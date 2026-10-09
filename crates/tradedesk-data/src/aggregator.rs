//! Aggregator — pure function (1m source bars, params) → `BarFrame` (D2-13 / D2-14 / D2-15).
//!
//! Transforms 1-minute [`RawBar`]s pulled from any [`Reader`] into a column-oriented
//! [`BarFrame`] at 15m / 1h / 1d resolution, with deterministic UTC bucketing and gap
//! omission (NEVER interpolation). Plan 04 owns the gap manifest side; this aggregator
//! purely emits a frame whose bars are byte-stable across re-runs on the same input.
//!
//! ## Determinism contract (CACHE-04 byte-identity, OUT-03)
//!
//! 1. **NO hash-randomised maps** anywhere in the aggregator or its inputs/outputs.
//!    `BTreeMap` only (no map types are needed here yet, but the convention is
//!    enforced by `grep` in CI).
//! 2. **NO `rayon::par_iter`** inside the per-symbol reduction. Single-threaded per quartet.
//! 3. **NO `Instant::now()` / `SystemTime::now()` / `Utc::now()`** inside this module.
//! 4. **f64 sums are sequential and ordered by `ts_open_utc`** — the [`Reader`] contract
//!    guarantees ascending order at the iterator level; the kernel preserves it.
//! 5. **Arrow IPC `Schema` constructed from a fixed `Vec<Field>`**, metadata keys collected
//!    from a `BTreeMap` to guarantee insertion order. (Plan 05 owns the Arrow side; the
//!    in-memory `BarFrame` here is the columnar source feeding it.)
//!
//! ## Bar boundary convention (D2-19)
//!
//! Bars are close-aligned to UTC midnight. A bar at `ts_open_utc = X` covers `[X, X + tf)`.
//! For 15m: `:00 / :15 / :30 / :45`; for 1h: `:00`; for 1d: `00:00:00 UTC`. Partial buckets
//! whose source minutes are entirely missing during open hours are OMITTED from the output;
//! buckets that have at least one source row are emitted as-is (Plan 04's gap manifest may
//! tag them with `affected_minutes` if more than 50% of the bucket is missing — that
//! decision is per Plan 04 scope and does not change this aggregator's emit/omit semantics).
//!
//! ## Module name
//!
//! Per revision pass 1: file is `aggregator.rs` (NOT `aggregate.rs`); `pub mod aggregator;`
//! in `lib.rs`; test paths are `aggregator::tests::*`. Matches VALIDATION.md verification
//! map and the Plan 06 public-surface audit.

use chrono::{DateTime, Duration, Timelike, Utc};
use schemars::{JsonSchema, Schema, SchemaGenerator};
use serde::{Deserialize, Deserializer, Serialize, Serializer};

use crate::reader::{ClosedRangeUtc, RawBar, Reader, Side};

/// Aggregator output-format version (D2-18).
///
/// Bump on ANY code change that alters output bytes for the same input. Stored in
/// both the Arrow file metadata AND the cache sidecar JSON; a mismatch triggers a
/// full rebuild (Plan 05 owns the cache side; this is the source-of-truth string).
pub const AGGREGATOR_VERSION: &str = "1.0.0";

/// Aggregation timeframe. The aggregator emits 5m / 10m / 15m / 1h / 1d bars from
/// 1-minute source.
///
/// Wire form (per PATTERNS lines 903-909): `"5m"` / `"10m"` / `"15m"` / `"1h"` / `"1d"`.
/// The variant name prefix `Tf` exists because Rust identifiers cannot start with a
/// digit; the manual `Serialize`/`Deserialize` impls map each variant to its
/// user-visible string form.
///
/// The hand-rolled `JsonSchema` impl pins the schema enum entries to
/// `"5m" / "10m" / "15m" / "1h" / "1d"`, matching the serde wire form.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum Timeframe {
    /// 5-minute bars.
    Tf5m,
    /// 10-minute bars.
    Tf10m,
    /// 15-minute bars.
    Tf15m,
    /// 1-hour bars.
    Tf1h,
    /// 1-day bars.
    Tf1d,
}

impl Timeframe {
    /// Bar duration. Used by [`aggregate`] to compute `ts_close_utc` for each emitted
    /// bar and by the misalignment validator at the kernel entry.
    #[must_use]
    pub fn duration(self) -> Duration {
        match self {
            Self::Tf5m => Duration::minutes(5),
            Self::Tf10m => Duration::minutes(10),
            Self::Tf15m => Duration::minutes(15),
            Self::Tf1h => Duration::hours(1),
            Self::Tf1d => Duration::hours(24),
        }
    }

    /// Filename-component string form (`"15m"` / `"1h"` / `"1d"`). Used by the cache
    /// path layout and by Arrow IPC metadata — centralised here so wire form and
    /// filesystem form cannot drift.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Tf5m => "5m",
            Self::Tf10m => "10m",
            Self::Tf15m => "15m",
            Self::Tf1h => "1h",
            Self::Tf1d => "1d",
        }
    }

    /// Inverse of [`Timeframe::as_str`] — parse the canonical CLI / wire form
    /// (`"5m"` / `"10m"` / `"15m"` / `"1h"` / `"1d"`) into a [`Timeframe`]. Used
    /// by Plan 03-05 to convert the clap-parsed `--timeframe` string into the
    /// typed enum at the CLI preflight boundary.
    ///
    /// # Errors
    /// Returns the input `&str` unchanged when it is not one of the five
    /// canonical forms; callers convert the error into a typed `WireError`
    /// with appropriate context.
    ///
    /// We do NOT implement `std::str::FromStr` because that trait's
    /// `Err: Display` requirement would force allocation; the borrowed `&str`
    /// is exactly what the preflight wrapper site needs.
    #[allow(clippy::should_implement_trait)]
    pub fn from_str(s: &str) -> Result<Self, &str> {
        match s {
            "5m" => Ok(Self::Tf5m),
            "10m" => Ok(Self::Tf10m),
            "15m" => Ok(Self::Tf15m),
            "1h" => Ok(Self::Tf1h),
            "1d" => Ok(Self::Tf1d),
            _ => Err(s),
        }
    }
}

impl Serialize for Timeframe {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(self.as_str())
    }
}

impl<'de> Deserialize<'de> for Timeframe {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        use serde::de::Error as _;
        let s = <&str as Deserialize>::deserialize(deserializer)?;
        match s {
            "5m" => Ok(Self::Tf5m),
            "10m" => Ok(Self::Tf10m),
            "15m" => Ok(Self::Tf15m),
            "1h" => Ok(Self::Tf1h),
            "1d" => Ok(Self::Tf1d),
            other => Err(D::Error::custom(format!(
                "unknown Timeframe: {other:?} (expected one of \"5m\", \"10m\", \"15m\", \"1h\", \"1d\")"
            ))),
        }
    }
}

impl JsonSchema for Timeframe {
    fn schema_name() -> std::borrow::Cow<'static, str> {
        "Timeframe".into()
    }
    fn schema_id() -> std::borrow::Cow<'static, str> {
        "tradedesk_data::aggregator::Timeframe".into()
    }
    fn json_schema(_: &mut SchemaGenerator) -> Schema {
        serde_json::json!({
            "type": "string",
            "enum": ["5m", "10m", "15m", "1h", "1d"],
            "description": "Bar aggregation timeframe — `5m`, `10m`, `15m`, `1h`, or `1d`."
        })
        .try_into()
        .expect("valid schema fragment")
    }
}

/// Aggregation parameters — pure-borrowed so the kernel call site is ergonomic.
#[derive(Debug, Clone, Copy)]
pub struct AggParams<'a> {
    /// Symbol (e.g., `"EURUSD"`). The reader resolves it to source files.
    pub symbol: &'a str,
    /// Bid or ask side.
    pub side: Side,
    /// Target timeframe.
    pub tf: Timeframe,
    /// UTC range `[start, end)`. `start` MUST be aligned to the timeframe boundary
    /// (e.g., for 15m, `start.minute() % 15 == 0` and seconds + nanos are zero).
    /// Misaligned ranges are caller bugs and surface as
    /// [`AggregateError::MisalignedRange`] rather than silent rounding.
    pub range: ClosedRangeUtc,
}

/// Column-oriented frame of aggregated bars (D2-14).
///
/// All column-vec lengths are equal; treat them as the columns of an implicit table.
/// This is the in-memory shape; Plan 05 maps each `Vec` to an Arrow column. **Not**
/// `Serialize` — Arrow IPC is the wire form, and a `Serialize` derive here would
/// force every consumer to pay the schema-derivation cost for no benefit.
#[derive(Debug, Clone)]
pub struct BarFrame {
    /// Stable source identifier (e.g., `"dukascopy"`); from [`Reader::source_id`].
    pub source_id: String,
    /// Symbol (e.g., `"EURUSD"`).
    pub symbol: String,
    /// Bid or ask side.
    pub side: Side,
    /// Bar timeframe.
    pub tf: Timeframe,
    /// Bar-open timestamps (UTC). Ascending; equal length to every other column.
    pub ts_open_utc: Vec<DateTime<Utc>>,
    /// Bar-close timestamps (UTC). `ts_close_utc[i] = ts_open_utc[i] + tf.duration()`.
    pub ts_close_utc: Vec<DateTime<Utc>>,
    /// First bar's open price within each bucket.
    pub open: Vec<f64>,
    /// Bucket-wise maximum of source `high`.
    pub high: Vec<f64>,
    /// Bucket-wise minimum of source `low`.
    pub low: Vec<f64>,
    /// Last bar's close price within each bucket.
    pub close: Vec<f64>,
    /// Bucket-wise SUM of per-bar `tick_volume`. Sequential f64 sum in ascending
    /// `ts_open_utc` order — the determinism guarantee for non-associative f64 addition.
    pub tick_volume: Vec<f64>,
}

impl BarFrame {
    /// Number of emitted bars. Equal to `self.ts_open_utc.len()` (all columns share
    /// this length as a structural invariant).
    #[must_use]
    pub fn len(&self) -> usize {
        self.ts_open_utc.len()
    }

    /// `true` if the frame contains no bars.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.ts_open_utc.is_empty()
    }

    /// RAD-2397 — append every column of `other` onto `self` in place.
    ///
    /// Used by the engine's `coalesce_subranges` path to fuse the
    /// per-sub-range frames emitted by the miner's `engine::gap_policy` into
    /// ONE logical frame before dispatching to a whole-sample scan kernel.
    ///
    /// Panics in debug builds if `other`'s `source_id` / `symbol` / `side`
    /// / `tf` do not match `self`; in release the mismatch is silent
    /// (the engine only ever calls this with frames produced from the
    /// same `(symbol, side, tf)` cache key, so the runtime check stays in
    /// `debug_assert!`).
    pub fn append_frame(&mut self, other: &BarFrame) {
        debug_assert_eq!(self.source_id, other.source_id, "append_frame: source_id");
        debug_assert_eq!(self.symbol, other.symbol, "append_frame: symbol");
        debug_assert_eq!(self.side, other.side, "append_frame: side");
        debug_assert_eq!(self.tf, other.tf, "append_frame: timeframe");
        self.ts_open_utc.extend_from_slice(&other.ts_open_utc);
        self.ts_close_utc.extend_from_slice(&other.ts_close_utc);
        self.open.extend_from_slice(&other.open);
        self.high.extend_from_slice(&other.high);
        self.low.extend_from_slice(&other.low);
        self.close.extend_from_slice(&other.close);
        self.tick_volume.extend_from_slice(&other.tick_volume);
    }
}

/// Aggregator error. `RE` is the reader's associated `Error` type.
///
/// **NO `Serialize` derive** — `RE` is unconstrained for ser and would force callers
/// to constrain `R::Error: Serialize` at every `aggregate` call site. The wire form
/// for `MisalignedRange` is a `tracing::warn!` log line + the typed error returned to
/// the caller; the engine boundary converts it to a `WireError` (Plan 05 owns that).
#[derive(Debug, thiserror::Error)]
pub enum AggregateError<RE>
where
    RE: std::error::Error + 'static,
{
    /// Reader returned an error during streaming (constructor or per-bar).
    #[error("reader error: {0}")]
    Reader(#[source] RE),

    /// The caller passed a `range.start` not aligned to the timeframe boundary —
    /// e.g., `Timeframe::Tf15m` with `start.minute() = 7`.
    #[error("range.start {start} is not aligned to {tf:?} boundary")]
    MisalignedRange {
        /// The offending start instant.
        start: DateTime<Utc>,
        /// The timeframe whose boundary `start` violated.
        tf: Timeframe,
    },
}

/// Internal helper: floor a UTC timestamp to the timeframe boundary. UTC-only —
/// chrono's UTC arithmetic has no DST math, so the component-wise truncation is
/// byte-stable across all timeframes and a single code path (single test surface).
///
/// For every timeframe, the sub-minute components (second, nanosecond) are zeroed.
/// Then:
/// - `Tf5m`: minute floored to a 5-minute boundary (`m / 5 * 5`).
/// - `Tf10m`: minute floored to a 10-minute boundary (`m / 10 * 10`).
/// - `Tf15m`: minute floored to a 15-minute boundary (`m / 15 * 15`).
/// - `Tf1h`: minute zeroed.
/// - `Tf1d`: minute AND hour zeroed.
///
/// All branches use chrono's `.with_*` setters in sequence; `Tf1d`
/// intentionally does NOT use the `date_naive` + `and_hms_opt` form — keeping a
/// single component-wise form removes the need to justify a per-tf branch in
/// code review, and a workspace grep gate enforces this discipline.
#[inline]
pub(crate) fn align_down(ts: DateTime<Utc>, tf: Timeframe) -> DateTime<Utc> {
    let t0 = ts
        .with_second(0)
        .and_then(|t| t.with_nanosecond(0))
        .expect("zeroing sub-minute fields is always valid");
    match tf {
        Timeframe::Tf5m => t0
            .with_minute((t0.minute() / 5) * 5)
            .expect("minute computed as (m / 5) * 5 is always in 0..=55"),
        Timeframe::Tf10m => t0
            .with_minute((t0.minute() / 10) * 10)
            .expect("minute computed as (m / 10) * 10 is always in 0..=50"),
        Timeframe::Tf15m => t0
            .with_minute((t0.minute() / 15) * 15)
            .expect("minute computed as (m / 15) * 15 is always in 0..=45"),
        Timeframe::Tf1h => t0
            .with_minute(0)
            .expect("minute = 0 is always a valid NaiveTime component"),
        Timeframe::Tf1d => t0
            .with_minute(0)
            .and_then(|t| t.with_hour(0))
            .expect("zeroing minute and hour is always valid"),
    }
}

/// Snap `ts` UP to the nearest timeframe boundary at or after `ts`. If `ts` is
/// already on a boundary, returns `ts` unchanged; otherwise returns
/// [`align_down`]`(ts, tf) + tf.duration()`.
///
/// Used by the miner's `engine::gap_policy::snap_subranges_to_timeframe` to round
/// `continuous_only` sub-range starts forward to the next bucket boundary
/// after the gap partitioner emits a minute-resolution start (RAD-2351).
#[inline]
#[must_use]
pub fn align_up(ts: DateTime<Utc>, tf: Timeframe) -> DateTime<Utc> {
    let floor = align_down(ts, tf);
    if floor == ts {
        ts
    } else {
        floor + tf.duration()
    }
}

/// Validate the caller's range against the timeframe boundary. See
/// [`AggParams::range`] for the contract.
#[inline]
fn validate_range_alignment(start: DateTime<Utc>, tf: Timeframe) -> bool {
    if start.second() != 0 || start.nanosecond() != 0 {
        return false;
    }
    match tf {
        Timeframe::Tf5m => start.minute() % 5 == 0,
        Timeframe::Tf10m => start.minute() % 10 == 0,
        Timeframe::Tf15m => start.minute() % 15 == 0,
        Timeframe::Tf1h => start.minute() == 0,
        Timeframe::Tf1d => start.minute() == 0 && start.hour() == 0,
    }
}

/// Aggregate 1-minute bars from `reader` into a column-oriented [`BarFrame`].
///
/// Pure function: no IO beyond the supplied reader, no clock reads, no env reads.
/// Determinism is the contract — byte-identical output across re-runs on the same
/// input (CACHE-04). See module docs for the 5 safeguards.
///
/// # Errors
///
/// - [`AggregateError::MisalignedRange`] when `params.range.start` is not aligned
///   to `params.tf.duration()` boundary (caller bug).
/// - [`AggregateError::Reader`] when the reader fails at construction time or for
///   any per-bar parse failure surfaced through the iterator.
pub fn aggregate<R: Reader>(
    reader: &R,
    params: AggParams<'_>,
) -> Result<BarFrame, AggregateError<R::Error>> {
    if !validate_range_alignment(params.range.start, params.tf) {
        return Err(AggregateError::MisalignedRange {
            start: params.range.start,
            tf: params.tf,
        });
    }

    let mut frame = BarFrame {
        source_id: reader.source_id().to_string(),
        symbol: params.symbol.to_string(),
        side: params.side,
        tf: params.tf,
        ts_open_utc: Vec::new(),
        ts_close_utc: Vec::new(),
        open: Vec::new(),
        high: Vec::new(),
        low: Vec::new(),
        close: Vec::new(),
        tick_volume: Vec::new(),
    };

    let tf_dur = params.tf.duration();
    let iter = reader
        .read_1m_bars(params.symbol, params.side, params.range)
        .map_err(AggregateError::Reader)?;

    // Open bucket state — `None` until the first bar arrives.
    let mut bucket_open: Option<DateTime<Utc>> = None;
    let mut acc_open: f64 = 0.0;
    let mut acc_high: f64 = 0.0;
    let mut acc_low: f64 = 0.0;
    let mut acc_close: f64 = 0.0;
    let mut acc_volume: f64 = 0.0;

    for bar_result in iter {
        let bar: RawBar = bar_result.map_err(AggregateError::Reader)?;
        let this_bucket = align_down(bar.ts_open_utc, params.tf);

        match bucket_open {
            Some(prev) if prev == this_bucket => {
                // Same bucket — fold the bar into the accumulators.
                if bar.high > acc_high {
                    acc_high = bar.high;
                }
                if bar.low < acc_low {
                    acc_low = bar.low;
                }
                acc_close = bar.close;
                // Sequential f64 sum in ascending `ts_open_utc` order — Reader
                // contract guarantees the order, so this addition is deterministic.
                acc_volume += bar.tick_volume;
            }
            Some(prev) => {
                // Bucket boundary — emit the previous bucket, then start a new one
                // with this bar's values.
                emit_bucket(
                    &mut frame, prev, tf_dur, acc_open, acc_high, acc_low, acc_close, acc_volume,
                );
                bucket_open = Some(this_bucket);
                acc_open = bar.open;
                acc_high = bar.high;
                acc_low = bar.low;
                acc_close = bar.close;
                acc_volume = bar.tick_volume;
            }
            None => {
                // First bar overall — initialise the bucket.
                bucket_open = Some(this_bucket);
                acc_open = bar.open;
                acc_high = bar.high;
                acc_low = bar.low;
                acc_close = bar.close;
                acc_volume = bar.tick_volume;
            }
        }
    }

    if let Some(prev) = bucket_open {
        emit_bucket(
            &mut frame, prev, tf_dur, acc_open, acc_high, acc_low, acc_close, acc_volume,
        );
    }

    Ok(frame)
}

/// Push the accumulated bucket onto the [`BarFrame`] column Vecs. Centralised so
/// the kernel's two emit sites stay in lockstep.
#[allow(clippy::too_many_arguments)]
#[inline]
fn emit_bucket(
    frame: &mut BarFrame,
    bucket_open: DateTime<Utc>,
    tf_dur: Duration,
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    tick_volume: f64,
) {
    frame.ts_open_utc.push(bucket_open);
    frame.ts_close_utc.push(bucket_open + tf_dur);
    frame.open.push(open);
    frame.high.push(high);
    frame.low.push(low);
    frame.close.push(close);
    frame.tick_volume.push(tick_volume);
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    use std::collections::BTreeMap;

    use chrono::{NaiveDate, TimeZone};
    use proptest::prelude::*;

    use crate::calendar::Calendar;
    use crate::reader::{Blake3Hex, RawBarIter};

    // -----------------------------------------------------------------------
    // Inline MockReader for unit-test use. Mirrors the integration MockReader
    // in `tests/aggregator_fixtures.rs` but lives inside the crate so unit
    // tests at `aggregator::tests::*` work without crossing the integration-
    // test boundary (PATTERNS Phase 1 precedent at sink.rs:399-409). Minor
    // duplication is preferred over making integration helpers part of the
    // public crate surface.
    // -----------------------------------------------------------------------

    /// Map key `(symbol, side, date)` so different days/sides are isolated.
    /// `BTreeMap` (never any hash-randomised map) for deterministic iteration
    /// order — OUT-03.
    type MockKey = (String, Side, NaiveDate);

    pub(super) struct MockReader {
        pub bars: BTreeMap<MockKey, Vec<RawBar>>,
        pub calendar: Calendar,
    }

    impl MockReader {
        pub fn new() -> Self {
            Self {
                bars: BTreeMap::new(),
                calendar: Calendar::fx_major(),
            }
        }

        pub fn insert_day(&mut self, symbol: &str, side: Side, date: NaiveDate, bars: Vec<RawBar>) {
            self.bars.insert((symbol.to_string(), side, date), bars);
        }
    }

    impl Reader for MockReader {
        type Error = std::io::Error;

        fn source_id(&self) -> &'static str {
            "mock"
        }

        fn trading_calendar(&self) -> Calendar {
            self.calendar.clone()
        }

        fn read_1m_bars<'a>(
            &'a self,
            symbol: &str,
            side: Side,
            range: ClosedRangeUtc,
        ) -> Result<RawBarIter<'a, Self::Error>, Self::Error> {
            let start_date = range.start.date_naive();
            let end_date = range.end.date_naive();
            // BTreeMap iteration order is the sort order of the keys; tuple-key
            // ordering is symbol → side → date, which produces ascending date
            // sequences for any fixed (symbol, side) pair.
            let mut all: Vec<RawBar> = Vec::new();
            for ((sym, sd, date), bars) in &self.bars {
                if sym != symbol || *sd != side {
                    continue;
                }
                if *date < start_date || *date > end_date {
                    continue;
                }
                for bar in bars {
                    if bar.ts_open_utc >= range.start && bar.ts_open_utc < range.end {
                        all.push(*bar);
                    }
                }
            }
            // BTreeMap already gives ascending (sym, side, date), and each day's
            // Vec is presumed-ascending by builder contract — no extra sort needed.
            Ok(Box::new(all.into_iter().map(Ok)))
        }

        fn fingerprint_day(
            &self,
            symbol: &str,
            side: Side,
            date: NaiveDate,
        ) -> Result<Option<Blake3Hex>, Self::Error> {
            let key = (symbol.to_string(), side, date);
            if self.bars.contains_key(&key) {
                Ok(Some(Blake3Hex::from_hex_bytes(&[b'0'; 64])))
            } else {
                Ok(None)
            }
        }

        fn enumerate_days(
            &self,
            symbol: &str,
            side: Side,
            range: ClosedRangeUtc,
        ) -> Result<Vec<NaiveDate>, Self::Error> {
            let start_date = range.start.date_naive();
            let end_date = range.end.date_naive();
            let mut out: Vec<NaiveDate> = self
                .bars
                .keys()
                .filter(|(s, sd, _)| s == symbol && *sd == side)
                .map(|(_, _, d)| *d)
                .filter(|d| *d >= start_date && *d <= end_date)
                .collect();
            out.sort_unstable();
            Ok(out)
        }
    }

    // -----------------------------------------------------------------------
    // Bar-builder helpers — bounded synthetic 1-minute data. Mirrors the
    // public helpers in `tests/aggregator_fixtures.rs`.
    // -----------------------------------------------------------------------

    /// Build 1440 `RawBar`s at 1-minute steps starting from `date 00:00:00 UTC`.
    /// OHLC ascends as `open_at_zero + i * 0.0001`; `high = close + 0.0001`,
    /// `low = open - 0.0001`, `tick_volume = 1.0`. Monotonic OHLC by construction.
    #[allow(clippy::cast_precision_loss)]
    fn build_24h_1m_bars(date: NaiveDate, open_at_zero: f64) -> Vec<RawBar> {
        let mut bars = Vec::with_capacity(1440);
        let day_start = date
            .and_hms_opt(0, 0, 0)
            .expect("00:00:00 is a valid wall-clock time")
            .and_utc();
        for i in 0..1440_i64 {
            let ts_open = day_start + Duration::minutes(i);
            let ts_close = ts_open + Duration::minutes(1);
            // i is bounded to 0..1440; precision loss for f64 is negligible.
            let base = open_at_zero + (i as f64) * 0.000_1;
            bars.push(RawBar {
                ts_open_utc: ts_open,
                ts_close_utc: ts_close,
                open: base,
                high: base + 0.000_1,
                low: base - 0.000_1,
                close: base + 0.000_05,
                tick_volume: 1.0,
            });
        }
        bars
    }

    fn whole_day_range(date: NaiveDate) -> ClosedRangeUtc {
        let start = date
            .and_hms_opt(0, 0, 0)
            .expect("00:00:00 is valid")
            .and_utc();
        let end = start + Duration::hours(24);
        ClosedRangeUtc { start, end }
    }

    // -----------------------------------------------------------------------
    // Const + helper sanity tests
    // -----------------------------------------------------------------------

    #[test]
    fn aggregator_version_is_one_zero_zero() {
        // The const is the cache-invalidation pivot. Bumping it forces every
        // cached BarFrame to rebuild (Plan 05); this test pins the initial value
        // so an accidental edit fails verification at the source.
        assert_eq!(AGGREGATOR_VERSION, "1.0.0");
    }

    #[test]
    fn timeframe_serde_round_trip() {
        let ser = serde_json::to_string(&Timeframe::Tf5m).unwrap();
        assert_eq!(ser, "\"5m\"");
        let de: Timeframe = serde_json::from_str("\"5m\"").unwrap();
        assert_eq!(de, Timeframe::Tf5m);

        let ser = serde_json::to_string(&Timeframe::Tf10m).unwrap();
        assert_eq!(ser, "\"10m\"");
        let de: Timeframe = serde_json::from_str("\"10m\"").unwrap();
        assert_eq!(de, Timeframe::Tf10m);

        let ser = serde_json::to_string(&Timeframe::Tf15m).unwrap();
        assert_eq!(ser, "\"15m\"");
        let de: Timeframe = serde_json::from_str("\"15m\"").unwrap();
        assert_eq!(de, Timeframe::Tf15m);

        let ser = serde_json::to_string(&Timeframe::Tf1h).unwrap();
        assert_eq!(ser, "\"1h\"");
        let de: Timeframe = serde_json::from_str("\"1h\"").unwrap();
        assert_eq!(de, Timeframe::Tf1h);

        let ser = serde_json::to_string(&Timeframe::Tf1d).unwrap();
        assert_eq!(ser, "\"1d\"");
        let de: Timeframe = serde_json::from_str("\"1d\"").unwrap();
        assert_eq!(de, Timeframe::Tf1d);
    }

    #[test]
    fn timeframe_from_str_round_trip() {
        for tf in [
            Timeframe::Tf5m,
            Timeframe::Tf10m,
            Timeframe::Tf15m,
            Timeframe::Tf1h,
            Timeframe::Tf1d,
        ] {
            assert_eq!(Timeframe::from_str(tf.as_str()).unwrap(), tf);
        }
    }

    #[test]
    fn timeframe_json_schema_enumerates_five_timeframes() {
        // RAD-3630: `miner scans` surfaces this schema; the enum must list the
        // five supported wire forms in canonical (ascending duration) order.
        let mut generator = SchemaGenerator::default();
        let schema = Timeframe::json_schema(&mut generator);
        let value = serde_json::to_value(&schema).expect("schema serialises");
        assert_eq!(
            value["enum"],
            serde_json::json!(["5m", "10m", "15m", "1h", "1d"]),
            "Timeframe JsonSchema enum must reflect the five supported timeframes"
        );
    }

    #[test]
    fn timeframe_from_str_rejects_unknown() {
        let err = Timeframe::from_str("2h").expect_err("must reject");
        assert_eq!(err, "2h");
    }

    #[test]
    fn align_down_15m() {
        let ts = Utc.with_ymd_and_hms(2024, 6, 12, 10, 7, 42).unwrap();
        // 10:07 floors to 10:00 (m / 15 * 15 = 0/15*15 = 0).
        assert_eq!(
            align_down(ts, Timeframe::Tf15m),
            Utc.with_ymd_and_hms(2024, 6, 12, 10, 0, 0).unwrap()
        );

        let ts = Utc.with_ymd_and_hms(2024, 6, 12, 10, 14, 59).unwrap();
        assert_eq!(
            align_down(ts, Timeframe::Tf15m),
            Utc.with_ymd_and_hms(2024, 6, 12, 10, 0, 0).unwrap()
        );

        let ts = Utc.with_ymd_and_hms(2024, 6, 12, 10, 15, 0).unwrap();
        assert_eq!(
            align_down(ts, Timeframe::Tf15m),
            Utc.with_ymd_and_hms(2024, 6, 12, 10, 15, 0).unwrap()
        );

        let ts = Utc.with_ymd_and_hms(2024, 6, 12, 10, 59, 59).unwrap();
        assert_eq!(
            align_down(ts, Timeframe::Tf15m),
            Utc.with_ymd_and_hms(2024, 6, 12, 10, 45, 0).unwrap()
        );
    }

    #[test]
    fn align_down_5m() {
        // 10:07:42 floors to 10:05 (m / 5 * 5 = 7/5*5 = 5).
        let ts = Utc.with_ymd_and_hms(2024, 6, 12, 10, 7, 42).unwrap();
        assert_eq!(
            align_down(ts, Timeframe::Tf5m),
            Utc.with_ymd_and_hms(2024, 6, 12, 10, 5, 0).unwrap()
        );
        // Already on a 5m boundary — unchanged (sub-minute fields zeroed).
        let ts = Utc.with_ymd_and_hms(2024, 6, 12, 10, 10, 0).unwrap();
        assert_eq!(
            align_down(ts, Timeframe::Tf5m),
            Utc.with_ymd_and_hms(2024, 6, 12, 10, 10, 0).unwrap()
        );
        let ts = Utc.with_ymd_and_hms(2024, 6, 12, 10, 59, 59).unwrap();
        assert_eq!(
            align_down(ts, Timeframe::Tf5m),
            Utc.with_ymd_and_hms(2024, 6, 12, 10, 55, 0).unwrap()
        );
    }

    #[test]
    fn align_down_10m() {
        // 10:07:42 floors to 10:00 (m / 10 * 10 = 7/10*10 = 0).
        let ts = Utc.with_ymd_and_hms(2024, 6, 12, 10, 7, 42).unwrap();
        assert_eq!(
            align_down(ts, Timeframe::Tf10m),
            Utc.with_ymd_and_hms(2024, 6, 12, 10, 0, 0).unwrap()
        );
        let ts = Utc.with_ymd_and_hms(2024, 6, 12, 10, 10, 0).unwrap();
        assert_eq!(
            align_down(ts, Timeframe::Tf10m),
            Utc.with_ymd_and_hms(2024, 6, 12, 10, 10, 0).unwrap()
        );
        let ts = Utc.with_ymd_and_hms(2024, 6, 12, 10, 59, 59).unwrap();
        assert_eq!(
            align_down(ts, Timeframe::Tf10m),
            Utc.with_ymd_and_hms(2024, 6, 12, 10, 50, 0).unwrap()
        );
    }

    #[test]
    fn align_down_1h() {
        let ts = Utc.with_ymd_and_hms(2024, 6, 12, 10, 7, 42).unwrap();
        assert_eq!(
            align_down(ts, Timeframe::Tf1h),
            Utc.with_ymd_and_hms(2024, 6, 12, 10, 0, 0).unwrap()
        );
        let ts = Utc.with_ymd_and_hms(2024, 6, 12, 10, 59, 59).unwrap();
        assert_eq!(
            align_down(ts, Timeframe::Tf1h),
            Utc.with_ymd_and_hms(2024, 6, 12, 10, 0, 0).unwrap()
        );
    }

    #[test]
    fn align_down_1d() {
        // Component-wise truncation: zero minute, then zero hour → midnight UTC
        // of the same calendar date. Verify on a few representative timestamps.
        let ts = Utc.with_ymd_and_hms(2024, 6, 12, 0, 0, 0).unwrap();
        assert_eq!(
            align_down(ts, Timeframe::Tf1d),
            Utc.with_ymd_and_hms(2024, 6, 12, 0, 0, 0).unwrap()
        );

        let ts = Utc.with_ymd_and_hms(2024, 6, 12, 23, 59, 59).unwrap();
        assert_eq!(
            align_down(ts, Timeframe::Tf1d),
            Utc.with_ymd_and_hms(2024, 6, 12, 0, 0, 0).unwrap()
        );

        let ts = Utc.with_ymd_and_hms(2024, 12, 31, 12, 34, 56).unwrap();
        assert_eq!(
            align_down(ts, Timeframe::Tf1d),
            Utc.with_ymd_and_hms(2024, 12, 31, 0, 0, 0).unwrap()
        );
    }

    // -----------------------------------------------------------------------
    // CACHE-03 — three_timeframes
    // -----------------------------------------------------------------------

    #[test]
    fn three_timeframes() {
        let date = NaiveDate::from_ymd_opt(2024, 6, 12).unwrap();
        let mut mock = MockReader::new();
        mock.insert_day("EURUSD", Side::Bid, date, build_24h_1m_bars(date, 1.0));

        let range = whole_day_range(date);
        for (tf, expected_len) in [
            (Timeframe::Tf5m, 288_usize),  // 1440 / 5
            (Timeframe::Tf10m, 144_usize), // 1440 / 10
            (Timeframe::Tf15m, 96_usize),  // 1440 / 15
            (Timeframe::Tf1h, 24_usize),
            (Timeframe::Tf1d, 1_usize),
        ] {
            let frame = aggregate(
                &mock,
                AggParams {
                    symbol: "EURUSD",
                    side: Side::Bid,
                    tf,
                    range,
                },
            )
            .expect("aggregation must succeed");
            assert_eq!(
                frame.len(),
                expected_len,
                "tf {:?} expected {} bars, got {}",
                tf,
                expected_len,
                frame.len()
            );
            // Structural invariant: every column has the same length.
            assert_eq!(frame.ts_open_utc.len(), expected_len);
            assert_eq!(frame.ts_close_utc.len(), expected_len);
            assert_eq!(frame.open.len(), expected_len);
            assert_eq!(frame.high.len(), expected_len);
            assert_eq!(frame.low.len(), expected_len);
            assert_eq!(frame.close.len(), expected_len);
            assert_eq!(frame.tick_volume.len(), expected_len);
        }
    }

    // -----------------------------------------------------------------------
    // CACHE-04 — ohlc_monotonicity_proptest
    // -----------------------------------------------------------------------

    /// `proptest` strategy for a single 1-minute `RawBar` with valid OHLC ordering
    /// by construction. The aggregator's invariant is that bucket-wise reductions
    /// preserve `high >= max(open, close)` and `low <= min(open, close)`.
    fn raw_bar_strategy(index: i64) -> impl Strategy<Value = RawBar> {
        // Generate a base price in a wide range, then derive OHLC such that the
        // bar's own OHLC monotonicity holds — the aggregator's job is to preserve
        // this property at the bucket level.
        (
            0.5_f64..2.0_f64,          // base / open price
            0.000_001_f64..0.001_f64,  // upward swing
            0.000_001_f64..0.001_f64,  // downward swing
            -0.000_5_f64..0.000_5_f64, // close offset from open
            0.0_f64..100.0_f64,        // tick_volume
        )
            .prop_map(move |(open, up, down, close_off, tick_volume)| {
                let high_candidate = open + up;
                let low_candidate = open - down;
                let close_raw = open + close_off;
                // Clamp `close` into `[low_candidate, high_candidate]` to preserve
                // the source-bar OHLC monotonicity contract.
                let close = close_raw.clamp(low_candidate, high_candidate);
                let high = high_candidate.max(close).max(open);
                let low = low_candidate.min(close).min(open);
                // 2024-06-12 00:00:00 UTC + `index` minutes.
                let ts_open =
                    Utc.with_ymd_and_hms(2024, 6, 12, 0, 0, 0).unwrap() + Duration::minutes(index);
                let ts_close = ts_open + Duration::minutes(1);
                RawBar {
                    ts_open_utc: ts_open,
                    ts_close_utc: ts_close,
                    open,
                    high,
                    low,
                    close,
                    tick_volume,
                }
            })
    }

    proptest! {
        #![proptest_config(ProptestConfig {
            cases: 64,
            ..ProptestConfig::default()
        })]
        #[test]
        #[allow(
            clippy::cast_possible_wrap,
            clippy::cast_precision_loss,
        )]
        fn ohlc_monotonicity_proptest(
            // Generate 60..=720 consecutive bars (1..=12 hours) to exercise multiple
            // 15m and 1h buckets per case.
            len in 60_i64..=720_i64,
        ) {
            // Build the bars via a closed-form constructor — proptest controls the
            // shape via the `len` parameter, the bars themselves are deterministic
            // per case. The aggregator invariant under test is that bucket-wise
            // reductions preserve OHLC monotonicity, which only needs the source
            // bars to be valid OHLC (they are by construction here).
            let date = NaiveDate::from_ymd_opt(2024, 6, 12).unwrap();
            let bars: Vec<RawBar> = (0..len)
                .map(|i| {
                    // `i` is bounded by `len <= 720`; precision loss negligible.
                    let open = 1.0 + (i as f64) * 0.000_001;
                    let close = open + 0.000_05;
                    let high = open.max(close) + 0.000_1;
                    let low = open.min(close) - 0.000_1;
                    let ts_open = Utc.with_ymd_and_hms(2024, 6, 12, 0, 0, 0).unwrap()
                        + Duration::minutes(i);
                    let ts_close = ts_open + Duration::minutes(1);
                    RawBar {
                        ts_open_utc: ts_open, ts_close_utc: ts_close,
                        open, high, low, close,
                        tick_volume: 1.0,
                    }
                })
                .collect();

            let mut mock = MockReader::new();
            mock.insert_day("EURUSD", Side::Bid, date, bars);

            let range = whole_day_range(date);
            for tf in [Timeframe::Tf15m, Timeframe::Tf1h] {
                let frame = aggregate(&mock, AggParams {
                    symbol: "EURUSD", side: Side::Bid, tf, range,
                }).expect("aggregate ok");
                for i in 0..frame.len() {
                    let oc_max = frame.open[i].max(frame.close[i]);
                    let oc_min = frame.open[i].min(frame.close[i]);
                    prop_assert!(
                        frame.high[i] >= oc_max,
                        "tf {:?} bar {}: high {} < max(open {}, close {})",
                        tf, i, frame.high[i], frame.open[i], frame.close[i]
                    );
                    prop_assert!(
                        frame.low[i] <= oc_min,
                        "tf {:?} bar {}: low {} > min(open {}, close {})",
                        tf, i, frame.low[i], frame.open[i], frame.close[i]
                    );
                }
            }
        }

        // Strategy-driven exploratory case (smaller `cases` count — the loop above
        // covers the bulk of the invariant; this case stresses single-bar strategy
        // output for sanity).
        #[test]
        fn raw_bar_strategy_emits_monotonic_ohlc(bar in raw_bar_strategy(0)) {
            prop_assert!(bar.high >= bar.open);
            prop_assert!(bar.high >= bar.close);
            prop_assert!(bar.low <= bar.open);
            prop_assert!(bar.low <= bar.close);
        }
    }

    // -----------------------------------------------------------------------
    // CACHE-04 — omits_gaps_never_interpolates
    // -----------------------------------------------------------------------

    #[test]
    fn omits_gaps_never_interpolates() {
        // Build a 1-day source dataset with bars 12:00..12:14 entirely removed —
        // an empty 15m bucket. The aggregator MUST omit that bucket (95 bars,
        // not 96), per D2-19 ("entirely missing → bar OMITTED").
        let date = NaiveDate::from_ymd_opt(2024, 6, 12).unwrap();
        let all_bars = build_24h_1m_bars(date, 1.0);
        let gap_start = Utc.with_ymd_and_hms(2024, 6, 12, 12, 0, 0).unwrap();
        let gap_end = Utc.with_ymd_and_hms(2024, 6, 12, 12, 15, 0).unwrap();
        let bars_with_gap: Vec<RawBar> = all_bars
            .into_iter()
            .filter(|b| b.ts_open_utc < gap_start || b.ts_open_utc >= gap_end)
            .collect();
        assert_eq!(
            bars_with_gap.len(),
            1440 - 15,
            "fixture sanity: 15 bars removed for the gap window"
        );

        let mut mock = MockReader::new();
        mock.insert_day("EURUSD", Side::Bid, date, bars_with_gap);

        let frame = aggregate(
            &mock,
            AggParams {
                symbol: "EURUSD",
                side: Side::Bid,
                tf: Timeframe::Tf15m,
                range: whole_day_range(date),
            },
        )
        .expect("aggregate ok");

        // 96 source 15m buckets minus the entirely-missing one = 95.
        assert_eq!(
            frame.len(),
            95,
            "fully-missing 15m bucket must be omitted (not interpolated)"
        );

        // Confirm the omitted bucket's open time is absent.
        let omitted = Utc.with_ymd_and_hms(2024, 6, 12, 12, 0, 0).unwrap();
        assert!(
            !frame.ts_open_utc.contains(&omitted),
            "omitted bucket ts_open_utc must NOT appear in the frame"
        );

        // Confirm the adjacent buckets are present (sanity).
        let before = Utc.with_ymd_and_hms(2024, 6, 12, 11, 45, 0).unwrap();
        let after = Utc.with_ymd_and_hms(2024, 6, 12, 12, 15, 0).unwrap();
        assert!(frame.ts_open_utc.contains(&before));
        assert!(frame.ts_open_utc.contains(&after));
    }

    // -----------------------------------------------------------------------
    // CACHE-04 — bid_ask_independent
    // -----------------------------------------------------------------------

    #[test]
    fn bid_ask_independent() {
        // Construct different OHLC for bid vs ask on the same symbol/date.
        // The aggregator must NEVER cross-contaminate the sides.
        let date = NaiveDate::from_ymd_opt(2024, 6, 12).unwrap();
        let bid_bars = build_24h_1m_bars(date, 1.0); // base 1.0
        let ask_bars = build_24h_1m_bars(date, 2.0); // base 2.0 — distinct

        let mut mock = MockReader::new();
        mock.insert_day("EURUSD", Side::Bid, date, bid_bars);
        mock.insert_day("EURUSD", Side::Ask, date, ask_bars);

        let range = whole_day_range(date);
        let bid_frame = aggregate(
            &mock,
            AggParams {
                symbol: "EURUSD",
                side: Side::Bid,
                tf: Timeframe::Tf15m,
                range,
            },
        )
        .expect("aggregate bid ok");
        let ask_frame = aggregate(
            &mock,
            AggParams {
                symbol: "EURUSD",
                side: Side::Ask,
                tf: Timeframe::Tf15m,
                range,
            },
        )
        .expect("aggregate ask ok");

        assert_eq!(bid_frame.len(), ask_frame.len());
        assert_eq!(bid_frame.side, Side::Bid);
        assert_eq!(ask_frame.side, Side::Ask);

        // The base offset is 1.0 vs 2.0 — every output bar must differ.
        for i in 0..bid_frame.len() {
            assert!(
                (bid_frame.open[i] - ask_frame.open[i]).abs() > 0.5,
                "bid/ask cross-contamination at bar {i}: bid.open {} ask.open {}",
                bid_frame.open[i],
                ask_frame.open[i]
            );
        }
    }

    // -----------------------------------------------------------------------
    // Misaligned-range error
    // -----------------------------------------------------------------------

    #[test]
    fn misaligned_range_errors_for_15m() {
        // 10:07 is not aligned to a 15-minute boundary.
        let mock = MockReader::new();
        let start = Utc.with_ymd_and_hms(2024, 6, 12, 10, 7, 0).unwrap();
        let end = start + Duration::hours(1);
        let err = aggregate(
            &mock,
            AggParams {
                symbol: "EURUSD",
                side: Side::Bid,
                tf: Timeframe::Tf15m,
                range: ClosedRangeUtc { start, end },
            },
        )
        .unwrap_err();
        match err {
            AggregateError::MisalignedRange { start: e_start, tf } => {
                assert_eq!(e_start, start);
                assert_eq!(tf, Timeframe::Tf15m);
            }
            AggregateError::Reader(_) => panic!("expected MisalignedRange, got Reader error"),
        }
    }

    #[test]
    fn misaligned_range_errors_for_5m_and_10m() {
        let mock = MockReader::new();

        // 10:07 is not aligned to a 5-minute boundary (7 % 5 != 0).
        let start = Utc.with_ymd_and_hms(2024, 6, 12, 10, 7, 0).unwrap();
        let err = aggregate(
            &mock,
            AggParams {
                symbol: "EURUSD",
                side: Side::Bid,
                tf: Timeframe::Tf5m,
                range: ClosedRangeUtc {
                    start,
                    end: start + Duration::hours(1),
                },
            },
        )
        .unwrap_err();
        assert!(matches!(
            err,
            AggregateError::MisalignedRange {
                tf: Timeframe::Tf5m,
                ..
            }
        ));

        // 10:05 IS aligned for 5m but NOT for 10m (5 % 10 != 0).
        let start = Utc.with_ymd_and_hms(2024, 6, 12, 10, 5, 0).unwrap();
        let err = aggregate(
            &mock,
            AggParams {
                symbol: "EURUSD",
                side: Side::Bid,
                tf: Timeframe::Tf10m,
                range: ClosedRangeUtc {
                    start,
                    end: start + Duration::hours(1),
                },
            },
        )
        .unwrap_err();
        assert!(matches!(
            err,
            AggregateError::MisalignedRange {
                tf: Timeframe::Tf10m,
                ..
            }
        ));

        // 10:05 must be ACCEPTED for 5m (no misalignment error).
        aggregate(
            &mock,
            AggParams {
                symbol: "EURUSD",
                side: Side::Bid,
                tf: Timeframe::Tf5m,
                range: ClosedRangeUtc {
                    start,
                    end: start + Duration::hours(1),
                },
            },
        )
        .expect("10:05 is aligned to a 5-minute boundary");
    }
}
