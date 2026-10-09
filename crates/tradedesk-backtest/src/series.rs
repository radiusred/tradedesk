//! An immutable, ordered series of joined bars at one timeframe.

use chrono::{DateTime, Utc};

use crate::{BarTimeframe, JoinedBar};

/// Why a bar vector cannot form a [`JoinedSeries`].
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum SeriesError {
    /// Bar timestamps must be strictly ascending (no repeats).
    #[error("bar {index} at {ts} does not follow the previous bar at {prev}")]
    NotAscending {
        /// Index of the offending bar.
        index: usize,
        /// Open of the previous bar.
        prev: DateTime<Utc>,
        /// Open of the offending bar.
        ts: DateTime<Utc>,
    },
    /// Every bar must open on a bucket boundary of the series' timeframe.
    #[error("bar {index} at {ts} is not aligned to a {timeframe} boundary")]
    Misaligned {
        /// Index of the offending bar.
        index: usize,
        /// Open of the offending bar.
        ts: DateTime<Utc>,
        /// The series timeframe.
        timeframe: BarTimeframe,
    },
}

/// Joined bid/ask bars for one symbol at one timeframe.
///
/// Invariants, checked at construction: bar opens are strictly ascending and each one
/// is a bucket open of `timeframe`. The series is read-only once built, so one load can
/// be borrowed by any number of sweep cells at once.
#[derive(Debug, Clone, PartialEq)]
pub struct JoinedSeries {
    symbol: String,
    timeframe: BarTimeframe,
    bars: Vec<JoinedBar>,
}

impl JoinedSeries {
    /// Build a series, checking its ordering and alignment invariants.
    ///
    /// # Errors
    /// [`SeriesError::NotAscending`] if two bars are out of order or share an open, and
    /// [`SeriesError::Misaligned`] if a bar does not open on a `timeframe` boundary.
    pub fn new(
        symbol: impl Into<String>,
        timeframe: BarTimeframe,
        bars: Vec<JoinedBar>,
    ) -> Result<Self, SeriesError> {
        for (index, bar) in bars.iter().enumerate() {
            if !timeframe.is_bucket_open(bar.ts_open_utc) {
                return Err(SeriesError::Misaligned {
                    index,
                    ts: bar.ts_open_utc,
                    timeframe,
                });
            }
            if index > 0 && bar.ts_open_utc <= bars[index - 1].ts_open_utc {
                return Err(SeriesError::NotAscending {
                    index,
                    prev: bars[index - 1].ts_open_utc,
                    ts: bar.ts_open_utc,
                });
            }
        }
        Ok(Self {
            symbol: symbol.into(),
            timeframe,
            bars,
        })
    }

    /// Instrument symbol, e.g. `"EURUSD"`.
    #[must_use]
    pub fn symbol(&self) -> &str {
        &self.symbol
    }

    /// Timeframe of every bar in the series.
    #[must_use]
    pub fn timeframe(&self) -> BarTimeframe {
        self.timeframe
    }

    /// The bars, ascending by open.
    #[must_use]
    pub fn bars(&self) -> &[JoinedBar] {
        &self.bars
    }

    /// Number of bars.
    #[must_use]
    pub fn len(&self) -> usize {
        self.bars.len()
    }

    /// `true` when the series holds no bars.
    #[must_use]
    pub fn is_empty(&self) -> bool {
        self.bars.is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Ohlcv;
    use chrono::TimeZone;

    fn bar_at(ts: DateTime<Utc>) -> JoinedBar {
        let side = Ohlcv {
            open: 1.0,
            high: 1.0,
            low: 1.0,
            close: 1.0,
            tick_volume: 1.0,
        };
        JoinedBar {
            ts_open_utc: ts,
            bid: side,
            ask: side,
        }
    }

    fn at(h: u32, m: u32) -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2024, 1, 2, h, m, 0).unwrap()
    }

    #[test]
    fn accepts_ascending_aligned_bars() {
        let s = JoinedSeries::new(
            "EURUSD",
            BarTimeframe::M15,
            vec![bar_at(at(0, 0)), bar_at(at(0, 30))],
        )
        .unwrap();
        assert_eq!(s.symbol(), "EURUSD");
        assert_eq!(s.timeframe(), BarTimeframe::M15);
        assert_eq!(s.len(), 2);
        assert!(!s.is_empty());
    }

    #[test]
    fn rejects_repeated_or_descending_opens() {
        for second in [at(0, 0), at(0, 0) - chrono::Duration::minutes(1)] {
            let err = JoinedSeries::new(
                "EURUSD",
                BarTimeframe::M1,
                vec![bar_at(at(0, 0)), bar_at(second)],
            )
            .unwrap_err();
            assert!(matches!(err, SeriesError::NotAscending { index: 1, .. }));
        }
    }

    #[test]
    fn rejects_misaligned_opens() {
        let err =
            JoinedSeries::new("EURUSD", BarTimeframe::H1, vec![bar_at(at(1, 15))]).unwrap_err();
        assert_eq!(
            err,
            SeriesError::Misaligned {
                index: 0,
                ts: at(1, 15),
                timeframe: BarTimeframe::H1
            }
        );
    }
}
