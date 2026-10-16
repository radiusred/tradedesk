//! In-memory loader: read each instrument's window once per run, join, aggregate.
//!
//! [`load`] reads every requested `(symbol, side)` exactly once through
//! [`Reader::read_1m_bars`], joins the two sides, and builds every requested timeframe
//! up front. The result, [`MarketData`], is immutable and `Send + Sync`, so sweep cells
//! borrow one load from any number of rayon workers without copying or re-reading.
//!
//! Instruments load in parallel with rayon `par_iter` and an order-preserving collect
//! (the deterministic-drain pattern from miner-core's sweep runner). When several
//! instruments fail, the first failure in request order is reported.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use chrono::{DateTime, Datelike, Duration, NaiveDate, Utc, Weekday};
use rayon::prelude::*;
use tradedesk_data::{ClosedRangeUtc, Reader, Side};

use crate::cost::config::VenueInstrumentCost;
use crate::cost::minutes::{MinuteCosts, MinutePrices};
use crate::{
    BarTimeframe, JoinError, JoinedAggregateError, JoinedBar, JoinedSeries, Ohlcv, OneSidedMinute,
    SeriesError, aggregate_series, join_sides,
};

/// One instrument's slice of a run: which window to read and which timeframes to build.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LoadRequest {
    /// Instrument symbol as the reader knows it, e.g. `"EURUSD"`.
    pub symbol: String,
    /// Half-open UTC window `[start, end)` on bar opens.
    pub window: ClosedRangeUtc,
    /// Timeframes to build. The 1-minute series is always kept, listed or not.
    pub timeframes: Vec<BarTimeframe>,
    /// With `Some`, also build the instrument's [`MinuteCosts`] over the window and a
    /// look-back before it ([`LoadRequest::with_minute_costs`]).
    pub minute_costs: Option<MinuteLookback>,
}

/// How far before a window the one-minute cost data must reach: the volatility
/// slippage's floor and the spread excess's baseline.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MinuteLookback {
    /// The spread excess's `baseline_weekdays` (0 without a spread excess).
    pub baseline_weekdays: u32,
    /// The volatility slippage's `floor_minutes` (0 without one).
    pub floor_minutes: u32,
}

impl MinuteLookback {
    /// The look-back `cost` needs, or `None` when it uses neither the volatility
    /// slippage nor a spread excess ([`VenueInstrumentCost::needs_minute_costs`]).
    #[must_use]
    pub fn for_cost(cost: &VenueInstrumentCost) -> Option<Self> {
        cost.needs_minute_costs().then(|| Self {
            baseline_weekdays: cost
                .spread_excess
                .as_ref()
                .filter(|x| !x.excluded)
                .map_or(0, crate::cost::config::SpreadExcess::baseline_weekdays),
            floor_minutes: cost.slippage.floor_minutes(),
        })
    }

    /// The first instant to read for a window opening at `start`: `00:00Z` of the day
    /// that leaves `2 × baseline_weekdays` weekdays before `start`'s date, plus one
    /// weekday per whole day of `floor_minutes` (rounded up) and one more as a margin.
    #[must_use]
    pub fn read_start(&self, start: DateTime<Utc>) -> DateTime<Utc> {
        let weekdays = 2 * self.baseline_weekdays + self.floor_minutes.div_ceil(1440) + 1;
        let mut day = start.date_naive();
        let mut counted = 0;
        while counted < weekdays {
            let Some(previous) = day.pred_opt() else {
                break;
            };
            day = previous;
            if !matches!(day.weekday(), Weekday::Sat | Weekday::Sun) {
                counted += 1;
            }
        }
        day.and_time(chrono::NaiveTime::MIN).and_utc().min(start)
    }
}

impl LoadRequest {
    /// Request `window` of `symbol` at `timeframes`.
    #[must_use]
    pub fn new(
        symbol: impl Into<String>,
        window: ClosedRangeUtc,
        timeframes: impl IntoIterator<Item = BarTimeframe>,
    ) -> Self {
        Self {
            symbol: symbol.into(),
            window,
            timeframes: timeframes.into_iter().collect(),
            minute_costs: None,
        }
    }

    /// Also build the instrument's [`MinuteCosts`] ([`InstrumentSeries::minute_costs`])
    /// from the window and `lookback` before it. The look-back is read in the same pass
    /// as the window, and only the cost data sees it: the series, the one-sided policy
    /// check and every timeframe are built from the window alone.
    #[must_use]
    pub fn with_minute_costs(mut self, lookback: MinuteLookback) -> Self {
        self.minute_costs = Some(lookback);
        self
    }

    /// Request whole UTC days `first..=last` (the Python engine's inclusive
    /// `date_from` / `date_to`): the window is `[first 00:00Z, last + 1 day 00:00Z)`.
    #[must_use]
    pub fn utc_days(
        symbol: impl Into<String>,
        first: NaiveDate,
        last: NaiveDate,
        timeframes: impl IntoIterator<Item = BarTimeframe>,
    ) -> Self {
        let start = first.and_time(chrono::NaiveTime::MIN).and_utc();
        let end = last.and_time(chrono::NaiveTime::MIN).and_utc() + Duration::days(1);
        Self::new(symbol, ClosedRangeUtc { start, end }, timeframes)
    }
}

/// What the loader does with minutes present on one side only.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum OneSidedPolicy {
    /// Fail the load with [`LoadError::OneSided`], listing every such minute.
    #[default]
    Reject,
    /// Keep the two-sided bars and attach the list to the instrument
    /// ([`InstrumentSeries::one_sided`]); a warning is logged.
    Report,
}

/// Why a load failed. `E` is the reader's error type.
#[derive(Debug, thiserror::Error)]
pub enum LoadError<E>
where
    E: std::error::Error + 'static,
{
    /// The same symbol appeared twice in one load; each instrument is read once.
    #[error("{0} is requested more than once; one load reads each instrument once")]
    DuplicateSymbol(String),
    /// The window's start is not before its end.
    #[error("{symbol}: empty window, {start} is not before {end}")]
    EmptyWindow {
        /// Instrument symbol.
        symbol: String,
        /// Window start.
        start: DateTime<Utc>,
        /// Window end.
        end: DateTime<Utc>,
    },
    /// Reading or joining the two sides failed.
    #[error("{symbol}: {source}")]
    Read {
        /// Instrument symbol.
        symbol: String,
        /// The reader or ordering failure.
        #[source]
        source: JoinError<E>,
    },
    /// Neither side has a bar in the window (an unknown symbol or an empty cache slice).
    #[error("{symbol}: no bars on either side in [{start}, {end})")]
    NoData {
        /// Instrument symbol.
        symbol: String,
        /// Window start.
        start: DateTime<Utc>,
        /// Window end.
        end: DateTime<Utc>,
    },
    /// Some minutes exist on one side only, under [`OneSidedPolicy::Reject`].
    #[error(
        "{symbol}: {} minute(s) present on one side only, first at {}",
        .minutes.len(),
        .minutes.first().map_or_else(String::new, |m| m.ts_open_utc.to_string())
    )]
    OneSided {
        /// Instrument symbol.
        symbol: String,
        /// Every one-sided minute, ascending.
        minutes: Vec<OneSidedMinute>,
    },
    /// The joined 1-minute bars broke the series invariants (e.g. a source timestamp
    /// not on a whole minute).
    #[error("{symbol}: {source}")]
    Series {
        /// Instrument symbol.
        symbol: String,
        /// The broken invariant.
        #[source]
        source: SeriesError,
    },
    /// A joined minute carries a price that is not finite or not above zero: a corrupt
    /// cache minute. It fails the load rather than leave a hole in the P&L (a `NaN` mark
    /// books `NaN` financing and disables a stop).
    #[error(
        "{symbol}: {side:?} {field} of the minute at {ts} is {value}, not a finite price above zero"
    )]
    BadPrice {
        /// Instrument symbol.
        symbol: String,
        /// The minute's open.
        ts: DateTime<Utc>,
        /// The side carrying the bad price.
        side: Side,
        /// `open`, `high`, `low` or `close`.
        field: &'static str,
        /// The price found.
        value: f64,
    },
    /// Building an aggregated timeframe failed.
    #[error("{symbol} {timeframe}: {source}")]
    Aggregate {
        /// Instrument symbol.
        symbol: String,
        /// The timeframe being built.
        timeframe: BarTimeframe,
        /// The aggregation failure.
        #[source]
        source: JoinedAggregateError,
    },
}

/// Everything loaded for one instrument.
#[derive(Debug, Clone, PartialEq)]
pub struct InstrumentSeries {
    symbol: String,
    window: ClosedRangeUtc,
    frames: BTreeMap<BarTimeframe, JoinedSeries>,
    one_sided: Vec<OneSidedMinute>,
    minute_costs: Option<Arc<MinuteCosts>>,
}

impl InstrumentSeries {
    /// Instrument symbol.
    #[must_use]
    pub fn symbol(&self) -> &str {
        &self.symbol
    }

    /// The window that was read.
    #[must_use]
    pub fn window(&self) -> ClosedRangeUtc {
        self.window
    }

    /// The joined 1-minute series (always present).
    ///
    /// # Panics
    /// Never: the loader always inserts the 1-minute series.
    #[must_use]
    pub fn m1(&self) -> &JoinedSeries {
        self.frames
            .get(&BarTimeframe::M1)
            .expect("the loader always keeps the 1m series")
    }

    /// The series at `tf`, if it was requested.
    #[must_use]
    pub fn series(&self, tf: BarTimeframe) -> Option<&JoinedSeries> {
        self.frames.get(&tf)
    }

    /// Timeframes held, ascending (always starts with `M1`).
    pub fn timeframes(&self) -> impl Iterator<Item = BarTimeframe> + '_ {
        self.frames.keys().copied()
    }

    /// Minutes present on one side only (non-empty only under
    /// [`OneSidedPolicy::Report`]). They are absent from every series.
    #[must_use]
    pub fn one_sided(&self) -> &[OneSidedMinute] {
        &self.one_sided
    }

    /// The one-minute cost data, built when the request asked for it
    /// ([`LoadRequest::with_minute_costs`]): every two-sided and one-sided minute of the
    /// window and the look-back.
    #[must_use]
    pub fn minute_costs(&self) -> Option<&Arc<MinuteCosts>> {
        self.minute_costs.as_ref()
    }
}

/// One run's market data: every requested instrument, loaded once, read-only.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct MarketData {
    instruments: BTreeMap<String, InstrumentSeries>,
}

impl MarketData {
    /// The instrument named `symbol`.
    #[must_use]
    pub fn get(&self, symbol: &str) -> Option<&InstrumentSeries> {
        self.instruments.get(symbol)
    }

    /// The `tf` series of `symbol`, if both were loaded.
    #[must_use]
    pub fn series(&self, symbol: &str, tf: BarTimeframe) -> Option<&JoinedSeries> {
        self.get(symbol).and_then(|i| i.series(tf))
    }

    /// Loaded instruments, ascending by symbol.
    pub fn instruments(&self) -> impl Iterator<Item = &InstrumentSeries> {
        self.instruments.values()
    }

    /// Number of loaded instruments.
    #[must_use]
    pub fn len(&self) -> usize {
        self.instruments.len()
    }

    /// `true` when nothing was loaded.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.instruments.is_empty()
    }
}

/// Load every request into memory: each `(symbol, side)` is read exactly once.
///
/// # Errors
/// [`LoadError::DuplicateSymbol`] before any read when a symbol repeats; otherwise the
/// first failing instrument's error in request order (see [`LoadError`]).
pub fn load<R: Reader>(
    reader: &R,
    requests: &[LoadRequest],
    policy: OneSidedPolicy,
) -> Result<MarketData, LoadError<R::Error>> {
    let mut seen = BTreeSet::new();
    for request in requests {
        if !seen.insert(request.symbol.as_str()) {
            return Err(LoadError::DuplicateSymbol(request.symbol.clone()));
        }
    }
    let loaded: Vec<_> = requests
        .par_iter()
        .map(|request| load_one(reader, request, policy))
        .collect();
    let mut instruments = BTreeMap::new();
    for result in loaded {
        let series = result?;
        instruments.insert(series.symbol.clone(), series);
    }
    Ok(MarketData { instruments })
}

fn load_one<R: Reader>(
    reader: &R,
    request: &LoadRequest,
    policy: OneSidedPolicy,
) -> Result<InstrumentSeries, LoadError<R::Error>> {
    let symbol = request.symbol.as_str();
    let window = request.window;
    if window.start >= window.end {
        return Err(LoadError::EmptyWindow {
            symbol: symbol.to_owned(),
            start: window.start,
            end: window.end,
        });
    }
    let read_err = |source| LoadError::Read {
        symbol: symbol.to_owned(),
        source,
    };
    let read = ClosedRangeUtc {
        start: request
            .minute_costs
            .map_or(window.start, |l| l.read_start(window.start)),
        end: window.end,
    };
    let open = |side| {
        reader
            .read_1m_bars(symbol, side, read)
            .map_err(|source| read_err(JoinError::Reader { side, source }))
    };
    let mut joined = join_sides(open(Side::Bid)?, open(Side::Ask)?).map_err(read_err)?;
    // The look-back is the cost data's alone: the series start at `window.start`.
    let lookback_bars = joined
        .bars
        .partition_point(|b| b.ts_open_utc < window.start);
    let lookback_one_sided = joined
        .one_sided
        .partition_point(|m| m.ts_open_utc < window.start);
    let one_sided = &joined.one_sided[lookback_one_sided..];
    if joined.bars.len() == lookback_bars && one_sided.is_empty() {
        return Err(LoadError::NoData {
            symbol: symbol.to_owned(),
            start: window.start,
            end: window.end,
        });
    }
    if !one_sided.is_empty() {
        match policy {
            OneSidedPolicy::Reject => {
                return Err(LoadError::OneSided {
                    symbol: symbol.to_owned(),
                    minutes: one_sided.to_vec(),
                });
            }
            OneSidedPolicy::Report => tracing::warn!(
                symbol,
                count = one_sided.len(),
                first = %one_sided[0].ts_open_utc,
                "minutes present on one side only are excluded from the joined series"
            ),
        }
    }

    check_prices(symbol, &joined.bars[lookback_bars..])?;
    let minute_costs = match request.minute_costs {
        None => None,
        Some(_) => Some(Arc::new(minute_costs(symbol, &joined)?)),
    };
    joined.bars.drain(..lookback_bars);
    joined.one_sided.drain(..lookback_one_sided);
    joined.one_sided_prices.drain(..lookback_one_sided);
    let m1 = JoinedSeries::new(symbol, BarTimeframe::M1, joined.bars).map_err(|source| {
        LoadError::Series {
            symbol: symbol.to_owned(),
            source,
        }
    })?;
    let mut frames = BTreeMap::new();
    for &tf in &request.timeframes {
        if tf == BarTimeframe::M1 || frames.contains_key(&tf) {
            continue;
        }
        let series = aggregate_series(&m1, tf).map_err(|source| LoadError::Aggregate {
            symbol: symbol.to_owned(),
            timeframe: tf,
            source,
        })?;
        frames.insert(tf, series);
    }
    tracing::info!(
        symbol,
        minutes = m1.len(),
        one_sided = joined.one_sided.len(),
        "loaded instrument"
    );
    frames.insert(BarTimeframe::M1, m1);
    Ok(InstrumentSeries {
        symbol: symbol.to_owned(),
        window,
        frames,
        one_sided: joined.one_sided,
        minute_costs,
    })
}

/// Every minute read, two-sided and one-sided, as [`MinuteCosts`]. Every price is
/// checked as the window's joined minutes are.
fn minute_costs<E: std::error::Error + 'static>(
    symbol: &str,
    joined: &crate::Joined,
) -> Result<MinuteCosts, LoadError<E>> {
    check_prices(symbol, &joined.bars)?;
    let mut one_sided = Vec::with_capacity(joined.one_sided.len());
    for (m, prices) in joined.one_sided.iter().zip(&joined.one_sided_prices) {
        check_side(symbol, m.ts_open_utc, m.present, prices)?;
        let (bid, ask) = match m.present {
            Side::Bid => (Some(*prices), None),
            Side::Ask => (None, Some(*prices)),
        };
        one_sided.push(MinutePrices {
            ts_open_utc: m.ts_open_utc,
            bid,
            ask,
        });
    }
    let mut two_sided = joined.bars.iter().map(MinutePrices::from).peekable();
    let mut one_sided = one_sided.into_iter().peekable();
    let merged = std::iter::from_fn(|| match (two_sided.peek(), one_sided.peek()) {
        (Some(a), Some(b)) if b.ts_open_utc < a.ts_open_utc => one_sided.next(),
        (Some(_), _) => two_sided.next(),
        (None, _) => one_sided.next(),
    });
    // The join yields both lists ascending and disjoint, so the merge is ascending.
    Ok(MinuteCosts::new(merged).expect("the join yields ascending, disjoint minutes"))
}

/// Every price of every joined minute must be finite and above zero.
fn check_prices<E: std::error::Error + 'static>(
    symbol: &str,
    bars: &[JoinedBar],
) -> Result<(), LoadError<E>> {
    for bar in bars {
        for (side, ohlcv) in [(Side::Bid, &bar.bid), (Side::Ask, &bar.ask)] {
            check_side(symbol, bar.ts_open_utc, side, ohlcv)?;
        }
    }
    Ok(())
}

/// Every price of one side of one minute must be finite and above zero.
fn check_side<E: std::error::Error + 'static>(
    symbol: &str,
    ts: DateTime<Utc>,
    side: Side,
    ohlcv: &Ohlcv,
) -> Result<(), LoadError<E>> {
    let fields = [
        ("open", ohlcv.open),
        ("high", ohlcv.high),
        ("low", ohlcv.low),
        ("close", ohlcv.close),
    ];
    if let Some(&(field, value)) = fields.iter().find(|(_, v)| !(v.is_finite() && *v > 0.0)) {
        return Err(LoadError::BadPrice {
            symbol: symbol.to_owned(),
            ts,
            side,
            field,
            value,
        });
    }
    Ok(())
}

// One load is shared by reference across rayon workers; keep that a compile-time fact.
const _: fn() = || {
    fn shared<T: Send + Sync>() {}
    shared::<MarketData>();
};

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;
    use std::sync::Mutex;
    use tradedesk_data::reader::RawBarIter;
    use tradedesk_data::{Blake3Hex, Calendar, RawBar};

    type Key = (String, Side);

    /// Mock reader over in-memory bars that counts `read_1m_bars` calls per
    /// `(symbol, side)`.
    #[derive(Default)]
    struct CountingReader {
        bars: BTreeMap<Key, Vec<RawBar>>,
        reads: Mutex<BTreeMap<Key, usize>>,
    }

    impl CountingReader {
        fn with(mut self, symbol: &str, side: Side, minutes: &[i64], price: f64) -> Self {
            let bars = minutes
                .iter()
                .map(|&m| {
                    let ts = t0() + Duration::minutes(m);
                    RawBar {
                        ts_open_utc: ts,
                        ts_close_utc: ts + Duration::minutes(1),
                        open: price,
                        high: price + 1.0,
                        low: price - 1.0,
                        close: price,
                        tick_volume: 1.0,
                    }
                })
                .collect();
            self.bars.insert((symbol.to_owned(), side), bars);
            self
        }

        fn both(self, symbol: &str, minutes: &[i64]) -> Self {
            self.with(symbol, Side::Bid, minutes, 100.0)
                .with(symbol, Side::Ask, minutes, 101.0)
        }

        fn reads(&self) -> BTreeMap<Key, usize> {
            self.reads.lock().unwrap().clone()
        }
    }

    impl Reader for CountingReader {
        type Error = std::io::Error;

        fn source_id(&self) -> &'static str {
            "counting-mock"
        }

        fn trading_calendar(&self) -> Calendar {
            Calendar::fx_major()
        }

        fn read_1m_bars<'a>(
            &'a self,
            symbol: &str,
            side: Side,
            range: ClosedRangeUtc,
        ) -> Result<RawBarIter<'a, Self::Error>, Self::Error> {
            let key = (symbol.to_owned(), side);
            *self.reads.lock().unwrap().entry(key.clone()).or_default() += 1;
            let bars = self.bars.get(&key).map_or(&[][..], Vec::as_slice);
            Ok(Box::new(
                bars.iter()
                    .filter(move |b| b.ts_open_utc >= range.start && b.ts_open_utc < range.end)
                    .map(|b| Ok(*b)),
            ))
        }

        fn fingerprint_day(
            &self,
            _symbol: &str,
            _side: Side,
            _date: NaiveDate,
        ) -> Result<Option<Blake3Hex>, Self::Error> {
            Ok(None)
        }

        fn enumerate_days(
            &self,
            _symbol: &str,
            _side: Side,
            _range: ClosedRangeUtc,
        ) -> Result<Vec<NaiveDate>, Self::Error> {
            Ok(Vec::new())
        }
    }

    fn t0() -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2024, 1, 2, 0, 0, 0).unwrap()
    }

    fn two_days(symbol: &str, tfs: &[BarTimeframe]) -> LoadRequest {
        let d = t0().date_naive();
        LoadRequest::utc_days(symbol, d, d.succ_opt().unwrap(), tfs.iter().copied())
    }

    const ALL_AGG: [BarTimeframe; 4] = [
        BarTimeframe::M5,
        BarTimeframe::M15,
        BarTimeframe::H1,
        BarTimeframe::D1,
    ];

    #[test]
    fn utc_days_spans_whole_inclusive_days() {
        let r = two_days("EURUSD", &[]);
        assert_eq!(r.window.start, t0());
        assert_eq!(r.window.end, t0() + Duration::days(2));
    }

    #[test]
    fn reads_each_symbol_side_exactly_once() {
        let minutes: Vec<i64> = (0..3 * 1440).step_by(7).collect();
        let reader = CountingReader::default()
            .both("EURUSD", &minutes)
            .both("XAUUSD", &minutes)
            .both("GBPJPY", &minutes);
        let requests: Vec<_> = ["EURUSD", "XAUUSD", "GBPJPY"]
            .iter()
            .map(|s| two_days(s, &ALL_AGG))
            .collect();
        let data = load(&reader, &requests, OneSidedPolicy::Reject).unwrap();

        let reads = reader.reads();
        assert_eq!(reads.len(), 6);
        assert!(reads.values().all(|&n| n == 1), "{reads:?}");

        let eur = data.get("EURUSD").unwrap();
        assert_eq!(
            eur.timeframes().collect::<Vec<_>>(),
            BarTimeframe::ALL.to_vec()
        );
        // Only minutes inside the two-day window are kept.
        assert_eq!(
            eur.m1().len(),
            minutes.iter().filter(|&&m| m < 2 * 1440).count()
        );
        assert_eq!(data.series("EURUSD", BarTimeframe::D1).unwrap().len(), 2);
        assert_eq!(eur.m1().bars()[0].bid.open, 100.0);
        assert_eq!(eur.m1().bars()[0].ask.open, 101.0);
        assert_eq!(data.len(), 3);
    }

    #[test]
    fn only_requested_timeframes_are_built() {
        let reader = CountingReader::default().both("EURUSD", &[0, 1, 2]);
        let data = load(
            &reader,
            &[two_days("EURUSD", &[BarTimeframe::H1, BarTimeframe::H1])],
            OneSidedPolicy::Reject,
        )
        .unwrap();
        let tfs: Vec<_> = data.get("EURUSD").unwrap().timeframes().collect();
        assert_eq!(tfs, vec![BarTimeframe::M1, BarTimeframe::H1]);
        assert!(data.series("EURUSD", BarTimeframe::M15).is_none());
    }

    #[test]
    fn reject_policy_fails_with_every_one_sided_minute() {
        let reader = CountingReader::default()
            .with("EURUSD", Side::Bid, &[0, 1, 2], 100.0)
            .with("EURUSD", Side::Ask, &[0, 2, 3], 101.0);
        let err = load(&reader, &[two_days("EURUSD", &[])], OneSidedPolicy::Reject).unwrap_err();
        let LoadError::OneSided { symbol, minutes } = &err else {
            panic!("expected OneSided, got {err}");
        };
        assert_eq!(symbol, "EURUSD");
        assert_eq!(
            minutes,
            &vec![
                OneSidedMinute {
                    ts_open_utc: t0() + Duration::minutes(1),
                    present: Side::Bid
                },
                OneSidedMinute {
                    ts_open_utc: t0() + Duration::minutes(3),
                    present: Side::Ask
                },
            ]
        );
        assert!(err.to_string().contains("2 minute(s)"));
    }

    #[test]
    fn report_policy_keeps_two_sided_bars_and_attaches_the_report() {
        let reader = CountingReader::default()
            .with("EURUSD", Side::Bid, &[0, 1, 2], 100.0)
            .with("EURUSD", Side::Ask, &[0, 2, 3], 101.0);
        let data = load(
            &reader,
            &[two_days("EURUSD", &[BarTimeframe::M15])],
            OneSidedPolicy::Report,
        )
        .unwrap();
        let eur = data.get("EURUSD").unwrap();
        let opens: Vec<_> = eur.m1().bars().iter().map(|b| b.ts_open_utc).collect();
        assert_eq!(opens, vec![t0(), t0() + Duration::minutes(2)]);
        assert_eq!(eur.one_sided().len(), 2);
        // Aggregates are built from the two-sided minutes only.
        assert_eq!(
            eur.series(BarTimeframe::M15).unwrap().bars()[0]
                .bid
                .tick_volume,
            2.0
        );
    }

    #[test]
    fn non_finite_and_non_positive_prices_fail_the_load() {
        // QA's three edits on #48 (a NaN close, an infinite high, a negative open), plus
        // a zero low on the ask side.
        let cases: [(Side, &str, f64); 4] = [
            (Side::Bid, "close", f64::NAN),
            (Side::Bid, "high", f64::INFINITY),
            (Side::Bid, "open", -5.0),
            (Side::Ask, "low", 0.0),
        ];
        for (side, field, value) in cases {
            let mut reader = CountingReader::default().both("EURUSD", &[0, 1, 2]);
            let bar = &mut reader.bars.get_mut(&("EURUSD".to_owned(), side)).unwrap()[1];
            match field {
                "open" => bar.open = value,
                "high" => bar.high = value,
                "low" => bar.low = value,
                _ => bar.close = value,
            }
            let err = load(
                &reader,
                &[two_days("EURUSD", &[BarTimeframe::D1])],
                OneSidedPolicy::Reject,
            )
            .unwrap_err();
            let LoadError::BadPrice {
                symbol,
                ts,
                side: bad_side,
                field: bad_field,
                value: bad_value,
            } = &err
            else {
                panic!("expected BadPrice for {field} = {value}, got {err}");
            };
            assert_eq!(symbol, "EURUSD");
            assert_eq!(*ts, t0() + Duration::minutes(1));
            assert_eq!((*bad_side, *bad_field), (side, field));
            assert!(
                bad_value.to_bits() == value.to_bits(),
                "{bad_value} vs {value}"
            );
            assert!(err.to_string().contains(field), "{err}");
        }
    }

    #[test]
    fn duplicate_symbol_is_rejected_before_any_read() {
        let reader = CountingReader::default().both("EURUSD", &[0]);
        let req = two_days("EURUSD", &[]);
        let err = load(&reader, &[req.clone(), req], OneSidedPolicy::Reject).unwrap_err();
        assert!(matches!(err, LoadError::DuplicateSymbol(ref s) if s == "EURUSD"));
        assert!(reader.reads().is_empty());
    }

    #[test]
    fn unknown_symbol_is_no_data_not_an_empty_series() {
        let reader = CountingReader::default();
        let err = load(&reader, &[two_days("NOPE", &[])], OneSidedPolicy::Reject).unwrap_err();
        assert!(matches!(err, LoadError::NoData { ref symbol, .. } if symbol == "NOPE"));
    }

    #[test]
    fn empty_window_is_rejected() {
        let reader = CountingReader::default().both("EURUSD", &[0]);
        let req = LoadRequest::new(
            "EURUSD",
            ClosedRangeUtc {
                start: t0(),
                end: t0(),
            },
            [],
        );
        let err = load(&reader, &[req], OneSidedPolicy::Reject).unwrap_err();
        assert!(matches!(err, LoadError::EmptyWindow { .. }));
    }

    #[test]
    fn the_minute_cost_look_back_counts_weekdays_back_from_the_window() {
        // 2024-01-02 is a Tuesday. Two baseline weekdays (4 back), one floor day and one
        // margin: six weekdays back is Monday 2023-12-25.
        let l = MinuteLookback {
            baseline_weekdays: 2,
            floor_minutes: 30,
        };
        assert_eq!(
            l.read_start(t0()),
            Utc.with_ymd_and_hms(2023, 12, 25, 0, 0, 0).unwrap()
        );
        // The default 20 weekdays and 30 minutes: 42 weekdays, 8 weeks back to Tuesday
        // and two more to the Friday before.
        let l = MinuteLookback {
            baseline_weekdays: 20,
            floor_minutes: 30,
        };
        assert_eq!(l.read_start(t0()), t0() - Duration::days(60));
    }

    #[test]
    fn minute_costs_reach_back_without_changing_the_series_or_the_reads() {
        // Minutes from three days before the window to its end, and a one-sided minute in
        // the look-back that the Reject policy must not see.
        let minutes: Vec<i64> = (-3 * 1440..2 * 1440).step_by(5).collect();
        let mut ask = minutes.clone();
        ask.push(-3 * 1440 + 1);
        ask.sort_unstable();
        let reader = CountingReader::default()
            .with("EURUSD", Side::Bid, &minutes, 100.0)
            .with("EURUSD", Side::Ask, &ask, 101.0);
        let plain = load(
            &reader,
            &[two_days("EURUSD", &[BarTimeframe::H1])],
            OneSidedPolicy::Reject,
        )
        .unwrap();
        let lookback = MinuteLookback {
            baseline_weekdays: 2,
            floor_minutes: 30,
        };
        let with = load(
            &reader,
            &[two_days("EURUSD", &[BarTimeframe::H1]).with_minute_costs(lookback)],
            OneSidedPolicy::Reject,
        )
        .unwrap();
        let (a, b) = (plain.get("EURUSD").unwrap(), with.get("EURUSD").unwrap());
        // The window's series are those of a load without the look-back.
        assert_eq!(a.m1(), b.m1());
        assert_eq!(a.series(BarTimeframe::H1), b.series(BarTimeframe::H1));
        assert!(b.one_sided().is_empty());
        assert!(a.minute_costs().is_none());
        // The cost data holds every minute read, the look-back and the one-sided one too.
        let costs = b.minute_costs().unwrap();
        assert_eq!(costs.len(), minutes.len() + 1);
        assert_eq!(costs.ts(0), t0() - Duration::days(3));
        assert!(
            costs
                .find(t0() - Duration::days(3) + Duration::minutes(1))
                .is_some()
        );
        // Each side was read once per load, the look-back in the same read.
        assert!(
            reader.reads().values().all(|&n| n == 2),
            "{:?}",
            reader.reads()
        );
    }

    #[test]
    fn a_bad_price_in_the_look_back_fails_a_load_that_asks_for_minute_costs() {
        let minutes: Vec<i64> = (-1440..1440).step_by(60).collect();
        let mut reader = CountingReader::default().both("EURUSD", &minutes);
        reader
            .bars
            .get_mut(&("EURUSD".to_owned(), Side::Bid))
            .unwrap()[0]
            .low = f64::NAN;
        let request = two_days("EURUSD", &[]);
        assert!(load(&reader, &[request.clone()], OneSidedPolicy::Reject).is_ok());
        let lookback = MinuteLookback {
            baseline_weekdays: 2,
            floor_minutes: 30,
        };
        let err = load(
            &reader,
            &[request.with_minute_costs(lookback)],
            OneSidedPolicy::Reject,
        )
        .unwrap_err();
        assert!(
            matches!(err, LoadError::BadPrice { field: "low", .. }),
            "{err}"
        );
    }

    #[test]
    fn one_load_is_shared_by_parallel_cells_without_rereading() {
        let minutes: Vec<i64> = (0..1440).collect();
        let reader = CountingReader::default().both("EURUSD", &minutes);
        let data = load(
            &reader,
            &[two_days("EURUSD", &ALL_AGG)],
            OneSidedPolicy::Reject,
        )
        .unwrap();
        let sums: Vec<f64> = (0..64)
            .into_par_iter()
            .map(|cell| {
                let tf = BarTimeframe::ALL[cell % BarTimeframe::ALL.len()];
                let series = data.series("EURUSD", tf).unwrap();
                series.bars().iter().map(|b| b.bid.tick_volume).sum()
            })
            .collect();
        assert!(sums.iter().all(|&s| s == 1440.0), "{sums:?}");
        assert_eq!(reader.reads().values().sum::<usize>(), 2);
    }
}
