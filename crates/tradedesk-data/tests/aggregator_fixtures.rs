//! Shared aggregator test fixtures (Wave 0 substrate for Plans 02-02 .. 02-05).
//!
//! This is the integration-test fixture module — it lives under `tests/` so Plan
//! 02-03's `tests/dst_spring_forward.rs` etc. can pull it in via `mod
//! aggregator_fixtures;` and consume `MockReader`, `build_24h_1m_bars`, and
//! `build_partial_day_1m_bars` without having to redefine them.
//!
//! ## Why this duplicates the unit-test `MockReader` in `src/aggregator.rs`
//!
//! VALIDATION.md expects four CACHE-03 / CACHE-04 tests under the path
//! `aggregator::tests::*` — that's a UNIT-test path (`src/aggregator.rs`'s
//! `#[cfg(test)] mod tests`). Unit tests cannot share `#[test]` entries with
//! integration tests, and the rust toolchain forbids re-exporting `#[cfg(test)]`
//! code from a `tests/` integration target.
//!
//! Phase 1 sets the precedent for this duplication at `sink.rs:399-409`: a small
//! inline test-only fn is preferred over crossing the unit/integration boundary
//! in test fixtures. Plan 03's DST tests run as integration (`tests/dst_*.rs`)
//! and import this module via `mod aggregator_fixtures;`.

#![allow(dead_code)] // helpers are consumed by Plan 03's tests, not this plan's.
#![allow(clippy::cast_precision_loss)] // synthetic-test domain, bounded inputs.
#![allow(clippy::cast_possible_wrap)] // i is bounded by `count` (always <=1440 in practice).

use std::collections::BTreeMap;

use chrono::{DateTime, Duration, NaiveDate, Utc};

use tradedesk_data::reader::RawBarIter;
use tradedesk_data::{Blake3Hex, Calendar, ClosedRangeUtc, RawBar, Reader, Side};

/// `BTreeMap` key — `(symbol, side, date)`. `BTreeMap` only (never any
/// hash-randomised map) so iteration order is the sort order of the keys —
/// deterministic for byte-identity tests.
pub type MockKey = (String, Side, NaiveDate);

/// In-memory [`Reader`] impl backed by a `BTreeMap` of pre-built bars. Used by
/// every aggregator integration test (DST, edge-case, determinism).
pub struct MockReader {
    /// Pre-built bars keyed by `(symbol, side, date)`. `BTreeMap` iteration order
    /// is the sort order of the keys, so iteration is deterministic.
    pub bars: BTreeMap<MockKey, Vec<RawBar>>,
    /// Trading calendar to return from [`Reader::trading_calendar`]. Default:
    /// [`Calendar::fx_major`].
    pub calendar: Calendar,
}

impl Default for MockReader {
    fn default() -> Self {
        Self::new()
    }
}

impl MockReader {
    /// Construct an empty `MockReader` with the FX-major calendar.
    #[must_use]
    pub fn new() -> Self {
        Self {
            bars: BTreeMap::new(),
            calendar: Calendar::fx_major(),
        }
    }

    /// Builder: override the trading calendar (e.g., for tests that pass a
    /// custom open/close schedule).
    #[must_use]
    pub fn with_calendar(mut self, c: Calendar) -> Self {
        self.calendar = c;
        self
    }

    /// Insert one day's bars under `(symbol, side, date)`. Replaces any prior
    /// entry for the same key.
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

/// Build 1440 [`RawBar`]s at 1-minute steps starting from `date 00:00:00 UTC`.
/// OHLC ascends as `open_at_zero + i * 0.0001`; `high = base + 0.0001`,
/// `low = base - 0.0001`, `close = base + 0.00005`, `tick_volume = 1.0`. Monotonic
/// OHLC by construction — useful as a baseline 24-hour fixture for Plan 03's
/// edge-case tests.
#[must_use]
pub fn build_24h_1m_bars(date: NaiveDate, open_at_zero: f64) -> Vec<RawBar> {
    let day_start = day_start_utc(date);
    build_partial_day_1m_bars(day_start, 1440, open_at_zero)
}

/// Build `count` 1-minute [`RawBar`]s starting from `start`. Used by gap-window
/// and partial-day tests where a 1440-minute baseline is too coarse.
#[must_use]
pub fn build_partial_day_1m_bars(start: DateTime<Utc>, count: usize, open: f64) -> Vec<RawBar> {
    let mut bars = Vec::with_capacity(count);
    for i in 0..count {
        let ts_open = start + Duration::minutes(i as i64);
        let ts_close = ts_open + Duration::minutes(1);
        let base = open + (i as f64) * 0.000_1;
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

/// Convenience: the UTC midnight of `date`.
///
/// # Panics
/// Panics only if `NaiveDate::and_hms_opt(0, 0, 0)` returns `None`, which is
/// statically impossible — midnight is a valid wall-clock time on every date.
#[must_use]
pub fn day_start_utc(date: NaiveDate) -> DateTime<Utc> {
    date.and_hms_opt(0, 0, 0)
        .expect("00:00:00 is a valid wall-clock time")
        .and_utc()
}

/// Convenience: the half-open `[date 00:00, date+1 00:00)` UTC range used by
/// "aggregate one whole day" tests.
#[must_use]
pub fn whole_day_range(date: NaiveDate) -> ClosedRangeUtc {
    let start = day_start_utc(date);
    let end = start + Duration::hours(24);
    ClosedRangeUtc { start, end }
}
