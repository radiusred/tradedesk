//! 1-minute passthrough and aggregation of joined series to 5m / 15m / 1h / 1d.
//!
//! Aggregation wraps miner-core's [`tradedesk_data::aggregator::aggregate`] instead of
//! reimplementing it. A private in-memory [`Reader`] serves one side of the joined
//! 1-minute series at a time, the core kernel folds it into a [`BarFrame`] per side, and
//! the two frames are zipped back into joined bars. Bucketing (UTC epoch boundaries,
//! daily at `00:00Z`, labelled with the bucket open), OHLC folding, empty-bucket
//! omission and the order of the f64 volume sums are therefore the scan engine's,
//! unchanged. Both sides are read from the same joined minute set, so their bucket sets
//! are identical by construction; the zip checks this anyway.

use std::convert::Infallible;

use chrono::{Duration, NaiveDate};
use tradedesk_data::aggregator::{AggParams, AggregateError, BarFrame};
use tradedesk_data::reader::RawBarIter;
use tradedesk_data::{Blake3Hex, Calendar, ClosedRangeUtc, RawBar, Reader, Side};

use crate::{BarTimeframe, JoinedBar, JoinedSeries, Ohlcv, SeriesError};

/// Why a joined series could not be aggregated.
#[derive(Debug, thiserror::Error)]
pub enum JoinedAggregateError {
    /// Aggregation reads 1-minute bars; the source series had another timeframe.
    #[error("aggregation needs a 1m source series, got {0}")]
    SourceNotOneMinute(BarTimeframe),
    /// miner-core's aggregator rejected the request.
    #[error(transparent)]
    Core(#[from] AggregateError<Infallible>),
    /// The bid and ask frames disagreed on their buckets (cannot happen for a series
    /// built by [`crate::join_sides`]; checked rather than assumed).
    #[error("bid and ask aggregates diverge at bucket {index}")]
    SideMismatch {
        /// First bucket index at which the frames differ.
        index: usize,
    },
    /// The aggregated bars failed the series invariants.
    #[error(transparent)]
    Series(#[from] SeriesError),
}

/// Build `tf` bars from a 1-minute joined series.
///
/// `tf == BarTimeframe::M1` returns the series unchanged (passthrough). Buckets with no
/// source minute are omitted, never interpolated, exactly as in miner-core.
///
/// # Errors
/// [`JoinedAggregateError::SourceNotOneMinute`] if `series` is not a 1-minute series.
/// The other variants indicate a broken invariant and are not expected in practice.
pub fn aggregate_series(
    series: &JoinedSeries,
    tf: BarTimeframe,
) -> Result<JoinedSeries, JoinedAggregateError> {
    if series.timeframe() != BarTimeframe::M1 {
        return Err(JoinedAggregateError::SourceNotOneMinute(series.timeframe()));
    }
    let Some(core_tf) = tf.to_core() else {
        return Ok(series.clone());
    };
    let bars = series.bars();
    let (Some(first), Some(last)) = (bars.first(), bars.last()) else {
        return Ok(JoinedSeries::new(series.symbol(), tf, Vec::new())?);
    };
    let range = ClosedRangeUtc {
        start: tf.bucket_open(first.ts_open_utc),
        end: last.ts_open_utc + Duration::minutes(1),
    };
    let view = JoinedView { bars };
    let frame = |side| {
        tradedesk_data::aggregator::aggregate(
            &view,
            AggParams {
                symbol: series.symbol(),
                side,
                tf: core_tf,
                range,
            },
        )
    };
    let bid = frame(Side::Bid)?;
    let ask = frame(Side::Ask)?;
    if bid.len() != ask.len() {
        return Err(JoinedAggregateError::SideMismatch {
            index: bid.len().min(ask.len()),
        });
    }
    let mut out = Vec::with_capacity(bid.len());
    for index in 0..bid.len() {
        if bid.ts_open_utc[index] != ask.ts_open_utc[index] {
            return Err(JoinedAggregateError::SideMismatch { index });
        }
        out.push(JoinedBar {
            ts_open_utc: bid.ts_open_utc[index],
            bid: row(&bid, index),
            ask: row(&ask, index),
        });
    }
    Ok(JoinedSeries::new(series.symbol(), tf, out)?)
}

fn row(frame: &BarFrame, i: usize) -> Ohlcv {
    Ohlcv {
        open: frame.open[i],
        high: frame.high[i],
        low: frame.low[i],
        close: frame.close[i],
        tick_volume: frame.tick_volume[i],
    }
}

/// Serves one side of an in-memory joined 1-minute series through the `Reader` trait,
/// so the unchanged miner-core aggregator can fold it. Private: it exists only to feed
/// `aggregate_series`.
struct JoinedView<'s> {
    bars: &'s [JoinedBar],
}

impl Reader for JoinedView<'_> {
    type Error = Infallible;

    fn source_id(&self) -> &'static str {
        "tradedesk-backtest-memory"
    }

    fn trading_calendar(&self) -> Calendar {
        // Not consulted by the aggregator; the default calendar keeps the trait honest.
        Calendar::fx_major()
    }

    fn read_1m_bars<'a>(
        &'a self,
        _symbol: &str,
        side: Side,
        range: ClosedRangeUtc,
    ) -> Result<RawBarIter<'a, Self::Error>, Self::Error> {
        let iter = self
            .bars
            .iter()
            .filter(move |b| b.ts_open_utc >= range.start && b.ts_open_utc < range.end)
            .map(move |b| {
                let s = b.side(side);
                Ok(RawBar {
                    ts_open_utc: b.ts_open_utc,
                    ts_close_utc: b.ts_open_utc + Duration::minutes(1),
                    open: s.open,
                    high: s.high,
                    low: s.low,
                    close: s.close,
                    tick_volume: s.tick_volume,
                })
            });
        Ok(Box::new(iter))
    }

    fn fingerprint_day(
        &self,
        _symbol: &str,
        _side: Side,
        _date: NaiveDate,
    ) -> Result<Option<Blake3Hex>, Self::Error> {
        // In-memory bars have no source file to fingerprint.
        Ok(None)
    }

    fn enumerate_days(
        &self,
        _symbol: &str,
        _side: Side,
        range: ClosedRangeUtc,
    ) -> Result<Vec<NaiveDate>, Self::Error> {
        let mut days: Vec<NaiveDate> = self
            .bars
            .iter()
            .filter(|b| b.ts_open_utc >= range.start && b.ts_open_utc < range.end)
            .map(|b| b.ts_open_utc.date_naive())
            .collect();
        days.dedup();
        Ok(days)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{DateTime, TimeZone, Utc};
    use proptest::prelude::*;
    use std::collections::BTreeMap;

    fn t0() -> DateTime<Utc> {
        // A Monday; the proptest window spans into the following days.
        Utc.with_ymd_and_hms(2024, 1, 1, 0, 0, 0).unwrap()
    }

    fn side(open: f64, close: f64, ext: f64, vol: f64) -> Ohlcv {
        Ohlcv {
            open,
            high: open.max(close) + ext,
            low: open.min(close) - ext,
            close,
            tick_volume: vol,
        }
    }

    fn bar(minute: i64, bid: Ohlcv, ask: Ohlcv) -> JoinedBar {
        JoinedBar {
            ts_open_utc: t0() + Duration::minutes(minute),
            bid,
            ask,
        }
    }

    fn m1(bars: Vec<JoinedBar>) -> JoinedSeries {
        JoinedSeries::new("EURUSD", BarTimeframe::M1, bars).unwrap()
    }

    #[test]
    fn m1_is_a_passthrough() {
        let s = m1(vec![
            bar(0, side(1.0, 2.0, 0.5, 1.0), side(1.5, 2.5, 0.5, 1.0)),
            bar(7, side(2.0, 1.0, 0.5, 1.0), side(2.5, 1.5, 0.5, 1.0)),
        ]);
        assert_eq!(aggregate_series(&s, BarTimeframe::M1).unwrap(), s);
    }

    #[test]
    fn rejects_a_non_1m_source() {
        let s = JoinedSeries::new("EURUSD", BarTimeframe::H1, Vec::new()).unwrap();
        let err = aggregate_series(&s, BarTimeframe::D1).unwrap_err();
        assert!(matches!(
            err,
            JoinedAggregateError::SourceNotOneMinute(BarTimeframe::H1)
        ));
    }

    #[test]
    fn empty_series_aggregates_to_empty() {
        let out = aggregate_series(&m1(Vec::new()), BarTimeframe::M15).unwrap();
        assert!(out.is_empty());
        assert_eq!(out.timeframe(), BarTimeframe::M15);
    }

    #[test]
    fn folds_each_side_independently_and_omits_empty_buckets() {
        // Minutes 3, 4, 14 fall in the 00:00 bucket; 31 in 00:30; 00:15 is empty.
        let s = m1(vec![
            bar(3, side(10.0, 11.0, 1.0, 1.0), side(20.0, 21.0, 1.0, 2.0)),
            bar(4, side(11.0, 9.0, 3.0, 1.5), side(21.0, 19.0, 0.5, 2.5)),
            bar(14, side(9.0, 9.5, 0.0, 0.5), side(19.0, 19.5, 4.0, 0.25)),
            bar(31, side(5.0, 6.0, 0.0, 7.0), side(15.0, 16.0, 0.0, 8.0)),
        ]);
        let out = aggregate_series(&s, BarTimeframe::M15).unwrap();
        let opens: Vec<_> = out.bars().iter().map(|b| b.ts_open_utc).collect();
        assert_eq!(opens, vec![t0(), t0() + Duration::minutes(30)]);
        let first = out.bars()[0];
        assert_eq!(
            first.bid,
            Ohlcv {
                open: 10.0,
                high: 14.0,
                low: 6.0,
                close: 9.5,
                tick_volume: 3.0
            }
        );
        assert_eq!(
            first.ask,
            Ohlcv {
                open: 20.0,
                high: 23.5,
                low: 15.0,
                close: 19.5,
                tick_volume: 4.75
            }
        );
        assert_eq!(out.bars()[1].bid.open, 5.0);
        assert_eq!(out.bars()[1].ask.close, 16.0);
    }

    #[test]
    fn daily_buckets_open_at_utc_midnight() {
        // Sunday-evening FX open (22:00Z) and the following Monday morning: two
        // UTC-midnight daily bars, the first a short Sunday stub, as in the Python
        // reference.
        let sunday_22 = -2 * 60;
        let s = m1(vec![
            bar(
                sunday_22,
                side(1.0, 2.0, 0.0, 1.0),
                side(1.1, 2.1, 0.0, 1.0),
            ),
            bar(9 * 60, side(3.0, 4.0, 0.0, 1.0), side(3.1, 4.1, 0.0, 1.0)),
        ]);
        let out = aggregate_series(&s, BarTimeframe::D1).unwrap();
        let opens: Vec<_> = out.bars().iter().map(|b| b.ts_open_utc).collect();
        assert_eq!(opens, vec![t0() - Duration::days(1), t0()]);
    }

    /// Naive per-side reference: group by bucket open in a `BTreeMap`, fold in order.
    fn reference(bars: &[JoinedBar], tf: BarTimeframe, pick: Side) -> Vec<(i64, Ohlcv)> {
        let mut buckets: BTreeMap<i64, Ohlcv> = BTreeMap::new();
        for b in bars {
            let key = tf.bucket_open(b.ts_open_utc).timestamp();
            let s = *b.side(pick);
            buckets
                .entry(key)
                .and_modify(|acc| {
                    acc.high = acc.high.max(s.high);
                    acc.low = acc.low.min(s.low);
                    acc.close = s.close;
                    acc.tick_volume += s.tick_volume;
                })
                .or_insert(s);
        }
        buckets.into_iter().collect()
    }

    fn side_strategy() -> impl Strategy<Value = Ohlcv> {
        (100_000_i32..120_000, -50_i32..50, 0_i32..20, 0_u32..1_000).prop_map(
            |(open, delta, ext, vol)| {
                side(
                    f64::from(open) / 10.0,
                    f64::from(open + delta) / 10.0,
                    f64::from(ext) / 10.0,
                    f64::from(vol) / 8.0,
                )
            },
        )
    }

    proptest! {
        /// Aggregation invariants on arbitrary sparse joined 1m data over ~3 days:
        /// bucket opens are aligned and ascending, one bar per non-empty bucket, each
        /// side equals an independent naive fold (bit-for-bit, volume included), and
        /// each side's high/low bound its open/close.
        #[test]
        fn aggregates_match_a_naive_fold_per_side(
            rows in proptest::collection::btree_map(
                0_i64..(3 * 1440),
                (side_strategy(), side_strategy()),
                0..400,
            )
        ) {
            let bars: Vec<JoinedBar> = rows
                .into_iter()
                .map(|(m, (b, a))| bar(m, b, a))
                .collect();
            let series = m1(bars.clone());
            for tf in [BarTimeframe::M5, BarTimeframe::M15, BarTimeframe::H1, BarTimeframe::D1] {
                let out = aggregate_series(&series, tf).unwrap();
                prop_assert_eq!(out.timeframe(), tf);
                let want_bid = reference(&bars, tf, Side::Bid);
                let want_ask = reference(&bars, tf, Side::Ask);
                prop_assert_eq!(out.len(), want_bid.len());
                for (i, got) in out.bars().iter().enumerate() {
                    prop_assert!(tf.is_bucket_open(got.ts_open_utc));
                    prop_assert_eq!(got.ts_open_utc.timestamp(), want_bid[i].0);
                    prop_assert_eq!(got.bid, want_bid[i].1);
                    prop_assert_eq!(got.ask, want_ask[i].1);
                    for s in [got.bid, got.ask] {
                        prop_assert!(s.high >= s.open.max(s.close));
                        prop_assert!(s.low <= s.open.min(s.close));
                    }
                }
            }
        }
    }
}
