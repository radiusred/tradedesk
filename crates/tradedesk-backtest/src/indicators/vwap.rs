//! Session VWAP (`tradedesk/marketdata/indicators/vwap.py`).

use chrono::{DateTime, Duration, NaiveDate, Utc};

use super::{Indicator, IndicatorError, check_bar};
use crate::Ohlcv;

/// Price each bar contributes to the VWAP.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VwapPrice {
    /// `(high + low + close) / 3` (the Python default).
    Typical,
    /// The close.
    Close,
}

/// When the VWAP accumulators reset.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VwapSession {
    /// Never: one VWAP over the whole stream (`reset_daily_utc=False`).
    None,
    /// On each new UTC date of the bar open (the Python default).
    UtcDay,
    /// At this UTC hour: a bar before the hour belongs to the previous day's session
    /// (`reset_hour_utc`). Must be `0..=23`.
    UtcHour(u32),
}

/// Volume-weighted average price over a session, weighted by tick volume.
///
/// Python parity: the session key is taken from the bar's open timestamp (the label
/// the Python candles carry); on a key change the accumulators restart before the bar
/// is added. A bar with zero volume adds nothing, and no value is reported while the
/// session's cumulative volume is 0. Volume must be finite and non-negative.
#[derive(Debug, Clone)]
pub struct Vwap {
    price: VwapPrice,
    session: VwapSession,
    key: Option<NaiveDate>,
    cum_pv: f64,
    cum_v: f64,
}

impl Vwap {
    /// A VWAP with the given price basis and session rule.
    ///
    /// # Errors
    /// [`IndicatorError::InvalidParameter`] for an hour outside `0..=23`.
    pub fn new(price: VwapPrice, session: VwapSession) -> Result<Self, IndicatorError> {
        if let VwapSession::UtcHour(h) = session {
            if h > 23 {
                return Err(IndicatorError::InvalidParameter {
                    name: "reset_hour_utc",
                    reason: "must be 0..=23",
                });
            }
        }
        Ok(Self {
            price,
            session,
            key: None,
            cum_pv: 0.0,
            cum_v: 0.0,
        })
    }

    fn session_key(&self, ts: DateTime<Utc>) -> Option<NaiveDate> {
        match self.session {
            VwapSession::None => None,
            VwapSession::UtcDay => Some(ts.date_naive()),
            VwapSession::UtcHour(h) => Some((ts - Duration::hours(i64::from(h))).date_naive()),
        }
    }
}

impl Indicator for Vwap {
    type Input = (DateTime<Utc>, Ohlcv);
    type Output = Option<f64>;

    fn update(&mut self, (ts, bar): (DateTime<Utc>, Ohlcv)) -> Result<Option<f64>, IndicatorError> {
        check_bar(&bar)?;
        let volume = bar.tick_volume;
        if !(volume.is_finite() && volume >= 0.0) {
            return Err(IndicatorError::InvalidVolume { value: volume });
        }
        let key = self.session_key(ts);
        if key.is_some() && self.key.is_some() && key != self.key {
            self.cum_pv = 0.0;
            self.cum_v = 0.0;
        }
        if key.is_some() {
            self.key = key;
        }

        let price = match self.price {
            VwapPrice::Typical => (bar.high + bar.low + bar.close) / 3.0,
            VwapPrice::Close => bar.close,
        };
        self.cum_pv += price * volume;
        self.cum_v += volume;
        Ok(self.is_ready().then(|| self.cum_pv / self.cum_v))
    }

    fn is_ready(&self) -> bool {
        self.cum_v > 0.0
    }

    fn reset(&mut self) {
        self.key = None;
        self.cum_pv = 0.0;
        self.cum_v = 0.0;
    }

    fn warmup_periods(&self) -> usize {
        1
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn bar(close: f64, volume: f64) -> Ohlcv {
        Ohlcv {
            open: close,
            high: close + 1.0,
            low: close - 1.0,
            close,
            tick_volume: volume,
        }
    }

    fn at(d: u32, h: u32) -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2024, 1, d, h, 0, 0).unwrap()
    }

    #[test]
    fn resets_on_the_utc_date_and_skips_zero_volume() {
        let mut v = Vwap::new(VwapPrice::Close, VwapSession::UtcDay).unwrap();
        assert_eq!(v.update((at(2, 22), bar(10.0, 0.0))).unwrap(), None);
        assert_eq!(v.update((at(2, 23), bar(10.0, 1.0))).unwrap(), Some(10.0));
        assert_eq!(v.update((at(2, 23), bar(20.0, 3.0))).unwrap(), Some(17.5));
        // New UTC date: the session restarts.
        assert_eq!(v.update((at(3, 0), bar(30.0, 1.0))).unwrap(), Some(30.0));
    }

    #[test]
    fn hour_reset_assigns_early_bars_to_the_previous_session() {
        let mut v = Vwap::new(VwapPrice::Close, VwapSession::UtcHour(7)).unwrap();
        v.update((at(2, 8), bar(10.0, 1.0))).unwrap();
        // 03:00 on the 3rd is still the 2nd's session (before 07:00).
        assert_eq!(v.update((at(3, 3), bar(20.0, 1.0))).unwrap(), Some(15.0));
        assert_eq!(v.update((at(3, 7), bar(40.0, 1.0))).unwrap(), Some(40.0));
        assert!(Vwap::new(VwapPrice::Close, VwapSession::UtcHour(24)).is_err());
    }

    #[test]
    fn typical_price_and_negative_volume() {
        let mut v = Vwap::new(VwapPrice::Typical, VwapSession::None).unwrap();
        assert_eq!(v.update((at(2, 0), bar(10.0, 2.0))).unwrap(), Some(10.0));
        assert!(matches!(
            v.update((at(2, 1), bar(10.0, -1.0))),
            Err(IndicatorError::InvalidVolume { .. })
        ));
        assert!(matches!(
            v.update((at(2, 1), bar(10.0, f64::NAN))),
            Err(IndicatorError::InvalidVolume { .. })
        ));
    }
}
