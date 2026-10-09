//! Joined bid/ask bars.
//!
//! The Dukascopy cache stores bid and ask in separate day files, and
//! `tradedesk_data::Reader` yields one [`RawBar`] stream per [`Side`]. The backtester needs
//! both sides of the same minute together: strategies read one configured side and the
//! fill model (#30) checks the opposing side for presence and divergence. A
//! [`JoinedBar`] carries both sides of one bar, keyed by its open timestamp.
//!
//! Prices stay in the cache's raw units (EURUSD `11536.0` is 1.15360; XAUUSD is in
//! cents; indices are in points). Converting to quote units is instrument
//! configuration and belongs to the cost model, not to the bar.

use chrono::{DateTime, Utc};
use tradedesk_data::{RawBar, Side};

use crate::BarTimeframe;

/// One side's OHLC prices and tick volume for one bar.
///
/// `tick_volume` keeps miner-core's A1 meaning: the summed per-tick float volume, never
/// contract volume or a tick count. It aggregates by sum.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Ohlcv {
    /// First traded price in the bar.
    pub open: f64,
    /// Highest price in the bar.
    pub high: f64,
    /// Lowest price in the bar.
    pub low: f64,
    /// Last price in the bar.
    pub close: f64,
    /// Summed per-tick volume over the bar.
    pub tick_volume: f64,
}

impl Ohlcv {
    /// Copy the price and volume fields out of a reader bar.
    #[must_use]
    pub fn from_raw(bar: &RawBar) -> Self {
        Self {
            open: bar.open,
            high: bar.high,
            low: bar.low,
            close: bar.close,
            tick_volume: bar.tick_volume,
        }
    }
}

/// Bid and ask for the same bar.
///
/// Both sides are always present: a minute that exists on one side only never becomes
/// a `JoinedBar` (see [`crate::OneSidedMinute`]).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct JoinedBar {
    /// Bar open (UTC). The bar covers `[ts_open_utc, ts_open_utc + timeframe)`.
    pub ts_open_utc: DateTime<Utc>,
    /// Bid side.
    pub bid: Ohlcv,
    /// Ask side.
    pub ask: Ohlcv,
}

impl JoinedBar {
    /// The requested side.
    #[must_use]
    pub fn side(&self, side: Side) -> &Ohlcv {
        match side {
            Side::Bid => &self.bid,
            Side::Ask => &self.ask,
        }
    }

    /// Bar close (exclusive end) for a bar of timeframe `tf`.
    #[must_use]
    pub fn ts_close_utc(&self, tf: BarTimeframe) -> DateTime<Utc> {
        self.ts_open_utc + tf.duration()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{Duration, TimeZone};

    fn ohlcv(base: f64) -> Ohlcv {
        Ohlcv {
            open: base,
            high: base + 2.0,
            low: base - 1.0,
            close: base + 1.0,
            tick_volume: 3.5,
        }
    }

    #[test]
    fn side_selects_the_named_side() {
        let bar = JoinedBar {
            ts_open_utc: Utc.with_ymd_and_hms(2024, 1, 2, 0, 0, 0).unwrap(),
            bid: ohlcv(100.0),
            ask: ohlcv(101.0),
        };
        assert_eq!(*bar.side(Side::Bid), bar.bid);
        assert_eq!(*bar.side(Side::Ask), bar.ask);
        assert_eq!(
            bar.ts_close_utc(BarTimeframe::M15),
            bar.ts_open_utc + Duration::minutes(15)
        );
    }

    #[test]
    fn from_raw_copies_every_field() {
        let ts = Utc.with_ymd_and_hms(2024, 1, 2, 0, 0, 0).unwrap();
        let raw = RawBar {
            ts_open_utc: ts,
            ts_close_utc: ts + Duration::minutes(1),
            open: 1.0,
            high: 4.0,
            low: 0.5,
            close: 2.0,
            tick_volume: 7.25,
        };
        let o = Ohlcv::from_raw(&raw);
        assert_eq!(
            (o.open, o.high, o.low, o.close, o.tick_volume),
            (1.0, 4.0, 0.5, 2.0, 7.25)
        );
    }
}
