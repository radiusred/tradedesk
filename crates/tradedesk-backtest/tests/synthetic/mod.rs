//! An in-memory `Reader` over synthetic 1-minute bars, for the engine and sweep tests.
//!
//! Each synthetic **day** is four 1-minute bars at 10:00–10:03Z whose prices walk
//! open → high → low → close, so the day's aggregated D1 bar has exactly that OHLC and
//! every rollover (22:00 London) is marked at the day's close. Bid and ask are equal.
//! The reader counts `read_1m_bars` calls per `(symbol, side)`.
#![allow(dead_code)]

use std::collections::BTreeMap;
use std::sync::Mutex;

use chrono::{DateTime, Duration, NaiveDate, TimeZone, Utc};
use tradedesk_data::reader::RawBarIter;
use tradedesk_data::{Blake3Hex, Calendar, ClosedRangeUtc, RawBar, Reader, Side};

/// `(open, high, low, close)` of one day, in raw units.
pub type Day = (f64, f64, f64, f64);

/// In-memory bars per `(symbol, side)`, with a read counter.
#[derive(Default)]
pub struct MemReader {
    bars: BTreeMap<(String, Side), Vec<RawBar>>,
    reads: Mutex<BTreeMap<(String, Side), usize>>,
}

/// Monday 2024-01-01.
pub fn first_day() -> NaiveDate {
    NaiveDate::from_ymd_opt(2024, 1, 1).unwrap()
}

fn minute(ts: DateTime<Utc>, price: f64) -> RawBar {
    RawBar {
        ts_open_utc: ts,
        ts_close_utc: ts + Duration::minutes(1),
        open: price,
        high: price,
        low: price,
        close: price,
        tick_volume: 1.0,
    }
}

impl MemReader {
    /// Add `days` for `symbol`, one per calendar day from `start`.
    #[must_use]
    pub fn with_days(mut self, symbol: &str, start: NaiveDate, days: &[Day]) -> Self {
        let mut bars = Vec::with_capacity(days.len() * 4);
        for (i, &(o, h, l, c)) in days.iter().enumerate() {
            let date = start + Duration::days(i64::try_from(i).unwrap());
            let ten = Utc.from_utc_datetime(&date.and_hms_opt(10, 0, 0).unwrap());
            for (m, price) in [o, h, l, c].into_iter().enumerate() {
                bars.push(minute(
                    ten + Duration::minutes(i64::try_from(m).unwrap()),
                    price,
                ));
            }
        }
        for side in [Side::Bid, Side::Ask] {
            self.bars.insert((symbol.to_owned(), side), bars.clone());
        }
        self
    }

    /// `read_1m_bars` calls so far, per `(symbol, side)`.
    pub fn reads(&self) -> BTreeMap<(String, Side), usize> {
        self.reads.lock().unwrap().clone()
    }
}

impl Reader for MemReader {
    type Error = std::io::Error;

    fn source_id(&self) -> &'static str {
        "synthetic"
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

/// A gold path in cents (the conversion to quote units is not the identity): 60 quiet
/// days alternating 2000.0 / 2006.0 dollars, a breakout day closing at 2060.0 (the test
/// strategy's entry with its default $2030 level), then `after`, every figure in
/// dollars × 100.
pub fn breakout_path(after: &[Day]) -> Vec<Day> {
    let cents = |d: Day| (d.0 * 100.0, d.1 * 100.0, d.2 * 100.0, d.3 * 100.0);
    let mut days = Vec::new();
    let mut prev = 2000.0;
    for i in 0..60_u8 {
        let c = if i % 2 == 1 { 2006.0 } else { 2000.0 };
        days.push(cents((
            prev,
            f64::max(prev, c) + 10.0,
            f64::min(prev, c) - 10.0,
            c,
        )));
        prev = c;
    }
    days.push(cents((prev, 2070.0, prev - 10.0, 2060.0)));
    days.extend(after.iter().map(|&d| cents(d)));
    days
}

/// `n` days of a seeded walk in dollars that reverts towards $2060, so it crosses $2060
/// again and again: a strategy with its level there trades many times.
pub fn choppy_days(n: usize) -> Vec<Day> {
    let mut days = Vec::with_capacity(n);
    let mut c = 2060.0;
    let mut state: u64 = 0x2545_f491_4f6c_dd1d;
    for _ in 0..n {
        state = state
            .wrapping_mul(6_364_136_223_846_793_005)
            .wrapping_add(1);
        #[allow(clippy::cast_precision_loss)]
        let u = (state >> 11) as f64 / (1_u64 << 53) as f64; // [0, 1)
        let next = c + (u - 0.5) * 80.0 + 0.3 * (2060.0 - c);
        days.push((c, f64::max(c, next) + 12.0, f64::min(c, next) - 12.0, next));
        c = next;
    }
    days
}
