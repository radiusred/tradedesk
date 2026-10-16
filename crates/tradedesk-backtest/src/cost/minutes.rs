//! One instrument's per-minute cost inputs: the high and low of each side and the
//! ask − bid at the open and the close, for every one-minute bar loaded.
//!
//! The volatility slippage and the spread excess ([`super::fill`]) read them. The
//! structure is separate from the stepping series: it may reach back before the run's
//! window (the loader's look-back, [`crate::LoadRequest::with_minute_costs`]) and it keeps
//! the minutes present on one side only, so it changes no series, no warm-up and no
//! result of a run that does not configure those models.
//!
//! Prices are raw cache units. A one-sided minute has the missing side's fields `NaN`
//! internally; the accessors never return them.

use chrono::{DateTime, Utc};

use crate::cost::config::PriceSide;
use crate::{JoinedBar, JoinedSeries, Ohlcv};

/// One one-minute bar with either side or both.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MinutePrices {
    /// The minute's open.
    pub ts_open_utc: DateTime<Utc>,
    /// The bid side, if the minute has one.
    pub bid: Option<Ohlcv>,
    /// The ask side, if the minute has one.
    pub ask: Option<Ohlcv>,
}

impl From<&JoinedBar> for MinutePrices {
    fn from(bar: &JoinedBar) -> Self {
        Self {
            ts_open_utc: bar.ts_open_utc,
            bid: Some(bar.bid),
            ask: Some(bar.ask),
        }
    }
}

/// Why minutes cannot form a [`MinuteCosts`].
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum MinuteCostsError {
    /// Minute opens must be strictly ascending.
    #[error("minute {index} at {ts} does not follow the previous minute")]
    NotAscending {
        /// Index of the offending minute.
        index: usize,
        /// Its open.
        ts: DateTime<Utc>,
    },
    /// A minute has neither side.
    #[error("minute {index} at {ts} has neither a bid nor an ask")]
    Empty {
        /// Index of the offending minute.
        index: usize,
        /// Its open.
        ts: DateTime<Utc>,
    },
}

/// Per-minute arrays for one instrument, ascending by minute open.
#[derive(Clone, PartialEq, Default)]
pub struct MinuteCosts {
    ts: Vec<DateTime<Utc>>,
    /// `[high, low]` of the bid, `NaN` when the minute has no bid.
    bid: Vec<[f64; 2]>,
    /// `[high, low]` of the ask, `NaN` when the minute has no ask.
    ask: Vec<[f64; 2]>,
    /// `max(ask.open − bid.open, ask.close − bid.close)`, `NaN` for a one-sided minute.
    spread: Vec<f64>,
}

/// A summary: the arrays hold one entry per minute loaded.
impl std::fmt::Debug for MinuteCosts {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("MinuteCosts")
            .field("minutes", &self.ts.len())
            .field("first", &self.ts.first())
            .field("last", &self.ts.last())
            .finish_non_exhaustive()
    }
}

impl MinuteCosts {
    /// Build from `minutes`, ascending by open.
    ///
    /// # Errors
    /// [`MinuteCostsError::NotAscending`] when two opens are out of order or repeat, and
    /// [`MinuteCostsError::Empty`] for a minute with neither side.
    pub fn new(minutes: impl IntoIterator<Item = MinutePrices>) -> Result<Self, MinuteCostsError> {
        let mut out = Self::default();
        for (index, m) in minutes.into_iter().enumerate() {
            let ts = m.ts_open_utc;
            if out.ts.last().is_some_and(|prev| *prev >= ts) {
                return Err(MinuteCostsError::NotAscending { index, ts });
            }
            if m.bid.is_none() && m.ask.is_none() {
                return Err(MinuteCostsError::Empty { index, ts });
            }
            let hl = |side: Option<Ohlcv>| side.map_or([f64::NAN; 2], |o| [o.high, o.low]);
            let spread = match (m.bid, m.ask) {
                (Some(b), Some(a)) => (a.open - b.open).max(a.close - b.close),
                _ => f64::NAN,
            };
            out.ts.push(ts);
            out.bid.push(hl(m.bid));
            out.ask.push(hl(m.ask));
            out.spread.push(spread);
        }
        Ok(out)
    }

    /// Build from a joined series (every minute two-sided).
    ///
    /// # Panics
    /// Never: a [`JoinedSeries`] is strictly ascending and every bar has both sides.
    #[must_use]
    pub fn from_series(series: &JoinedSeries) -> Self {
        Self::new(series.bars().iter().map(MinutePrices::from))
            .expect("a joined series is ascending and two-sided")
    }

    /// Minutes held.
    #[must_use]
    pub fn len(&self) -> usize {
        self.ts.len()
    }

    /// `true` when no minute is held.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.ts.is_empty()
    }

    /// The open of minute `i`.
    #[must_use]
    pub fn ts(&self, i: usize) -> DateTime<Utc> {
        self.ts[i]
    }

    /// Whether minute `i` has both sides.
    #[must_use]
    pub fn is_two_sided(&self, i: usize) -> bool {
        !self.spread[i].is_nan()
    }

    /// `R` of minute `i`, raw units: the range of the mid,
    /// `((ask.high + bid.high) − (ask.low + bid.low)) / 2`, or for a one-sided minute the
    /// range of the side that exists.
    #[must_use]
    pub fn range(&self, i: usize) -> f64 {
        let ([bh, bl], [ah, al]) = (self.bid[i], self.ask[i]);
        if bh.is_nan() {
            ah - al
        } else if ah.is_nan() {
            bh - bl
        } else {
            ((ah + bh) - (al + bl)) / 2.0
        }
    }

    /// `s` of minute `i`, raw units: `max(ask.open − bid.open, ask.close − bid.close)`;
    /// `None` for a one-sided minute.
    #[must_use]
    pub fn spread(&self, i: usize) -> Option<f64> {
        let s = self.spread[i];
        (!s.is_nan()).then_some(s)
    }

    /// `(high, low)` of minute `i` on `side`, raw units. `Mid` averages the two sides'
    /// highs and lows, as [`crate::FillModel::reference`] does. A one-sided minute gives
    /// the side that exists.
    #[must_use]
    pub fn high_low(&self, i: usize, side: PriceSide) -> (f64, f64) {
        let ([bh, bl], [ah, al]) = (self.bid[i], self.ask[i]);
        if bh.is_nan() {
            return (ah, al);
        }
        if ah.is_nan() {
            return (bh, bl);
        }
        match side {
            PriceSide::Bid => (bh, bl),
            PriceSide::Ask => (ah, al),
            PriceSide::Mid => (f64::midpoint(bh, ah), f64::midpoint(bl, al)),
        }
    }

    /// The number of minutes that open before `t`: the index of the first minute at or
    /// after `t`.
    #[must_use]
    pub fn before(&self, t: DateTime<Utc>) -> usize {
        self.ts.partition_point(|m| *m < t)
    }

    /// The index of the minute that opens exactly at `t`.
    #[must_use]
    pub fn find(&self, t: DateTime<Utc>) -> Option<usize> {
        self.ts.binary_search(&t).ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::{Duration, TimeZone};

    fn side(open: f64, high: f64, low: f64, close: f64) -> Ohlcv {
        Ohlcv {
            open,
            high,
            low,
            close,
            tick_volume: 1.0,
        }
    }

    fn t(m: i64) -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2024, 1, 2, 0, 0, 0).unwrap() + Duration::minutes(m)
    }

    #[test]
    fn range_spread_and_sides_of_two_and_one_sided_minutes() {
        let c = MinuteCosts::new([
            MinutePrices {
                ts_open_utc: t(0),
                bid: Some(side(100.0, 104.0, 98.0, 101.0)),
                ask: Some(side(101.0, 106.0, 99.0, 103.0)),
            },
            MinutePrices {
                ts_open_utc: t(2),
                bid: None,
                ask: Some(side(101.0, 105.0, 100.0, 102.0)),
            },
        ])
        .unwrap();
        assert_eq!(c.len(), 2);
        // Mid high (104 + 106) / 2 = 105, mid low (98 + 99) / 2 = 98.5.
        assert_eq!(c.range(0), 6.5);
        assert_eq!(c.spread(0), Some(2.0));
        assert_eq!(c.high_low(0, PriceSide::Mid), (105.0, 98.5));
        assert_eq!(c.high_low(0, PriceSide::Bid), (104.0, 98.0));
        assert_eq!(c.range(1), 5.0);
        assert_eq!(c.spread(1), None);
        assert!(!c.is_two_sided(1));
        assert_eq!(c.high_low(1, PriceSide::Bid), (105.0, 100.0));
        assert_eq!(c.before(t(1)), 1);
        assert_eq!(c.before(t(2)), 1);
        assert_eq!(c.find(t(2)), Some(1));
        assert_eq!(c.find(t(1)), None);
    }

    #[test]
    fn rejects_unordered_and_empty_minutes() {
        let two = |m| MinutePrices {
            ts_open_utc: t(m),
            bid: Some(side(1.0, 1.0, 1.0, 1.0)),
            ask: Some(side(1.0, 1.0, 1.0, 1.0)),
        };
        assert!(matches!(
            MinuteCosts::new([two(1), two(1)]),
            Err(MinuteCostsError::NotAscending { index: 1, .. })
        ));
        assert!(matches!(
            MinuteCosts::new([MinutePrices {
                ts_open_utc: t(0),
                bid: None,
                ask: None
            }]),
            Err(MinuteCostsError::Empty { index: 0, .. })
        ));
    }
}
