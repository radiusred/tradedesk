//! Streaming technical indicators, ported from tradedesk's Python indicators.
//!
//! Each indicator is a small state machine with the Python `Indicator` contract:
//! [`Indicator::update`] takes one input and returns the latest value (or `None` while
//! warming up), [`Indicator::is_ready`] reports whether a value is available,
//! [`Indicator::reset`] returns the indicator to its empty state, and
//! [`Indicator::warmup_periods`] is the number of inputs before the first value.
//! [`Indicator::batch`] runs a whole slice.
//!
//! Seeding and warm-up follow `tradedesk/marketdata/indicators` (tradedesk 1.6.2)
//! exactly, including its quirks. Each type documents its choice. The arithmetic is
//! written in the same order as the Python: windows are re-summed on every update
//! rather than kept as running sums, and every Python `sum()` is reproduced with
//! `CPython`'s Neumaier-compensated float summation (`CPython` >= 3.12, on which the
//! goldens were computed). The golden fixtures in `tests/fixtures/indicators/` therefore
//! match to the last bit in practice; the tests hold them to a `1e-9` relative
//! tolerance.
//!
//! Inputs are validated at this boundary (RAD-1906: corrupt prices passed through the
//! Python indicators undetected): a NaN, infinite or non-positive price, an
//! inconsistent bar, or an invalid volume is an [`IndicatorError`], and the
//! indicator's state is left exactly as it was.
//!
//! Prices are in the cache's raw units. Every indicator here is scale-free in the
//! sense that it never compares a price with an absolute threshold.

mod adx;
mod atr;
mod bollinger;
mod ema;
mod macd;
mod rsi;
mod sma;
mod vwap;

pub use adx::{Adx, AdxOutput};
pub use atr::Atr;
pub use bollinger::{BollingerBands, BollingerOutput};
pub use ema::Ema;
pub use macd::{Macd, MacdOutput};
pub use rsi::Rsi;
pub use sma::Sma;
pub use vwap::{Vwap, VwapPrice, VwapSession};

use chrono::{DateTime, Utc};
use tradedesk_data::Side;

use crate::{JoinedSeries, Ohlcv};

/// Which price of a bar failed validation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PriceField {
    /// Bar open.
    Open,
    /// Bar high.
    High,
    /// Bar low.
    Low,
    /// Bar close.
    Close,
}

impl std::fmt::Display for PriceField {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Open => "open",
            Self::High => "high",
            Self::Low => "low",
            Self::Close => "close",
        })
    }
}

/// Why an indicator could not be built or could not accept an input.
#[derive(Debug, Clone, Copy, PartialEq, thiserror::Error)]
pub enum IndicatorError {
    /// A constructor parameter is out of range.
    #[error("invalid parameter {name}: {reason}")]
    InvalidParameter {
        /// Parameter name.
        name: &'static str,
        /// What is wrong with it.
        reason: &'static str,
    },
    /// A price is NaN, infinite, zero or negative.
    #[error("invalid {field} price {value}: prices must be finite and positive")]
    InvalidPrice {
        /// The offending field.
        field: PriceField,
        /// The value received.
        value: f64,
    },
    /// The bar's prices contradict each other (`low <= open, close <= high` fails).
    #[error("inconsistent bar: open {open}, high {high}, low {low}, close {close}")]
    InconsistentBar {
        /// Bar open.
        open: f64,
        /// Bar high.
        high: f64,
        /// Bar low.
        low: f64,
        /// Bar close.
        close: f64,
    },
    /// A volume is NaN, infinite or negative.
    #[error("invalid volume {value}: volume must be finite and non-negative")]
    InvalidVolume {
        /// The value received.
        value: f64,
    },
}

/// An [`IndicatorError`] raised part-way through [`Indicator::batch`].
#[derive(Debug, Clone, Copy, PartialEq, thiserror::Error)]
#[error("input {index}: {source}")]
pub struct BatchError {
    /// Index of the rejected input.
    pub index: usize,
    /// Why it was rejected.
    #[source]
    pub source: IndicatorError,
}

/// The streaming indicator contract shared by every indicator in this module.
pub trait Indicator {
    /// One input: a close (`f64`), a bar ([`Ohlcv`]), or a timestamped bar.
    type Input: Copy;
    /// What one update returns.
    type Output;

    /// Fold one input into the state and return the latest output.
    ///
    /// # Errors
    /// [`IndicatorError`] if the input fails validation; the state is then unchanged.
    fn update(&mut self, input: Self::Input) -> Result<Self::Output, IndicatorError>;

    /// `true` once the indicator produces values.
    fn is_ready(&self) -> bool;

    /// Return to the empty state the constructor produced.
    fn reset(&mut self);

    /// Inputs needed before the first value (the Python `warmup_periods`).
    fn warmup_periods(&self) -> usize;

    /// Run every input through [`Indicator::update`] in order.
    ///
    /// # Errors
    /// [`BatchError`] carrying the index of the first rejected input.
    fn batch(&mut self, inputs: &[Self::Input]) -> Result<Vec<Self::Output>, BatchError> {
        inputs
            .iter()
            .enumerate()
            .map(|(index, input)| {
                self.update(*input)
                    .map_err(|source| BatchError { index, source })
            })
            .collect()
    }
}

/// One side's closes from a series, oldest first.
#[must_use]
pub fn closes(series: &JoinedSeries, side: Side) -> Vec<f64> {
    series.bars().iter().map(|b| b.side(side).close).collect()
}

/// One side's bars from a series, oldest first.
#[must_use]
pub fn side_bars(series: &JoinedSeries, side: Side) -> Vec<Ohlcv> {
    series.bars().iter().map(|b| *b.side(side)).collect()
}

/// One side's bars with their open timestamps (the [`Vwap`] input), oldest first.
#[must_use]
pub fn timed_bars(series: &JoinedSeries, side: Side) -> Vec<(DateTime<Utc>, Ohlcv)> {
    series
        .bars()
        .iter()
        .map(|b| (b.ts_open_utc, *b.side(side)))
        .collect()
}

/// Validate one price: finite and strictly positive.
pub(crate) fn check_price(field: PriceField, value: f64) -> Result<f64, IndicatorError> {
    if value.is_finite() && value > 0.0 {
        Ok(value)
    } else {
        Err(IndicatorError::InvalidPrice { field, value })
    }
}

/// Validate a whole bar: every price finite and positive, and
/// `low <= open, close <= high`. Volume is not checked here (only VWAP reads it).
///
/// # Errors
/// [`IndicatorError::InvalidPrice`] or [`IndicatorError::InconsistentBar`].
pub fn check_bar(bar: &Ohlcv) -> Result<(), IndicatorError> {
    check_price(PriceField::Open, bar.open)?;
    check_price(PriceField::High, bar.high)?;
    check_price(PriceField::Low, bar.low)?;
    check_price(PriceField::Close, bar.close)?;
    let consistent = bar.low <= bar.high
        && bar.low <= bar.open
        && bar.open <= bar.high
        && bar.low <= bar.close
        && bar.close <= bar.high;
    if consistent {
        Ok(())
    } else {
        Err(IndicatorError::InconsistentBar {
            open: bar.open,
            high: bar.high,
            low: bar.low,
            close: bar.close,
        })
    }
}

/// A period as both `usize` (for buffers) and `f64` (for arithmetic), checked `> 0`.
pub(crate) fn period(name: &'static str, value: usize) -> Result<(usize, f64), IndicatorError> {
    let as_u32 = u32::try_from(value).map_err(|_| IndicatorError::InvalidParameter {
        name,
        reason: "must fit in 32 bits",
    })?;
    if as_u32 == 0 {
        return Err(IndicatorError::InvalidParameter {
            name,
            reason: "must be > 0",
        });
    }
    Ok((value, f64::from(as_u32)))
}

/// Python's built-in `sum()` over floats, bit for bit.
///
/// Since `CPython` 3.12, `sum()` of floats is Neumaier-compensated (an improved
/// Kahan–Babuška summation), and the goldens were computed on 3.12+, so every
/// `sum(window) / n` in the reference is compensated. This mirrors `CPython`'s
/// `builtin_sum_impl` float path: start from `0 + x0`, accumulate the compensation
/// term, and add it once at the end when it is non-zero and finite. A naive fold
/// drifts from the reference by a few ULPs on long windows of large prices (seen on
/// the MACD seeds over XAUUSD in cents).
pub(crate) fn sum(values: impl IntoIterator<Item = f64>) -> f64 {
    let mut values = values.into_iter();
    let Some(first) = values.next() else {
        return 0.0;
    };
    let mut total = 0.0 + first;
    let mut compensation = 0.0;
    for x in values {
        let t = total + x;
        if total.abs() >= x.abs() {
            compensation += (total - t) + x;
        } else {
            compensation += (x - t) + total;
        }
        total = t;
    }
    if compensation != 0.0 && compensation.is_finite() {
        total += compensation;
    }
    total
}

#[cfg(test)]
pub(crate) mod test_support {
    use crate::Ohlcv;

    /// A bar with the given high, low and close (open = close, no volume).
    pub(crate) fn hlc(high: f64, low: f64, close: f64) -> Ohlcv {
        Ohlcv {
            open: close,
            high,
            low,
            close,
            tick_volume: 0.0,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn check_price_rejects_nan_infinite_and_non_positive() {
        for bad in [f64::NAN, f64::INFINITY, f64::NEG_INFINITY, 0.0, -1.0, -0.0] {
            assert!(matches!(
                check_price(PriceField::Close, bad),
                Err(IndicatorError::InvalidPrice {
                    field: PriceField::Close,
                    ..
                })
            ));
        }
        assert_eq!(check_price(PriceField::Close, 1.5), Ok(1.5));
    }

    #[test]
    fn check_bar_rejects_inconsistent_bars() {
        let ok = Ohlcv {
            open: 10.0,
            high: 12.0,
            low: 9.0,
            close: 11.0,
            tick_volume: 1.0,
        };
        assert_eq!(check_bar(&ok), Ok(()));
        for bad in [
            Ohlcv { high: 8.0, ..ok },   // high below low
            Ohlcv { close: 13.0, ..ok }, // close above high
            Ohlcv { open: 8.5, ..ok },   // open below low
        ] {
            assert!(matches!(
                check_bar(&bad),
                Err(IndicatorError::InconsistentBar { .. })
            ));
        }
        assert!(matches!(
            check_bar(&Ohlcv {
                low: f64::NAN,
                ..ok
            }),
            Err(IndicatorError::InvalidPrice {
                field: PriceField::Low,
                ..
            })
        ));
    }

    #[test]
    fn period_rejects_zero() {
        assert!(matches!(
            period("period", 0),
            Err(IndicatorError::InvalidParameter { name: "period", .. })
        ));
        assert_eq!(period("period", 14), Ok((14, 14.0)));
    }

    #[test]
    fn sum_is_compensated_like_cpython() {
        // `CPython` >= 3.12: sum([0.1] * 10) == 1.0 exactly; a naive fold gives
        // 0.9999999999999999.
        let tenths = [0.1; 10];
        assert_eq!(sum(tenths), 1.0);
        assert_ne!(tenths.iter().fold(0.0, |a, x| a + x), 1.0);
        // sum([1e100, 1.0, -1e100, 1.0]) == 2.0 in `CPython` >= 3.12.
        assert_eq!(sum([1e100, 1.0, -1e100, 1.0]), 2.0);
        assert_eq!(sum([]), 0.0);
    }

    #[test]
    fn batch_reports_the_index_of_the_first_bad_input() {
        let mut sma = Sma::new(2).unwrap();
        let err = sma.batch(&[1.0, 2.0, f64::NAN, 3.0]).unwrap_err();
        assert_eq!(err.index, 2);
        assert!(matches!(err.source, IndicatorError::InvalidPrice { .. }));
    }
}
