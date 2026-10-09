//! Peak-to-trough drawdown on a daily equity curve, in the account currency and as a
//! fraction of the running peak.

use chrono::NaiveDate;
use serde::{Deserialize, Serialize};

/// One drawdown episode: from the running peak to the trough, and back.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Drawdown {
    /// `trough_equity − peak_equity`, account currency (negative).
    pub amount: f64,
    /// `trough_equity / peak_equity − 1`, in `[−1, 0)`. It is clamped at −1 (−100%) only
    /// when equity falls to zero or below; `amount` carries the whole loss.
    pub fraction: f64,
    /// Date of the peak: the first point at the running maximum before the trough.
    pub peak: NaiveDate,
    /// Equity at the peak.
    pub peak_equity: f64,
    /// Date of the trough: the first point at this episode's minimum.
    pub trough: NaiveDate,
    /// Equity at the trough.
    pub trough_equity: f64,
    /// The first later point at or above `peak_equity`; `None` if the curve never gets
    /// back there.
    pub recovery: Option<NaiveDate>,
}

/// The maximum drawdown measured two ways. The deepest loss in pounds and the deepest
/// loss in percent can be different episodes (a 10% fall from £100k is a bigger loss than
/// a 20% fall from £25k), so each has its own peak, trough and recovery. Each is `None`
/// when the curve never falls below a previous peak.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct MaxDrawdown {
    /// The largest loss in the account currency. First-class: the pound figure is the
    /// one the ~£100k capital ceiling (#28) is set against.
    pub by_amount: Option<Drawdown>,
    /// The largest loss as a fraction of the running peak.
    pub by_fraction: Option<Drawdown>,
}

fn fraction(equity: f64, peak: f64) -> f64 {
    let loss = equity - peak;
    if loss >= 0.0 {
        0.0
    } else if peak > 0.0 {
        (loss / peak).max(-1.0)
    } else {
        -1.0
    }
}

/// The maximum drawdowns of `curve`, a dated equity series in time order whose first
/// element is the opening equity.
#[must_use]
pub fn max_drawdown(curve: &[(NaiveDate, f64)]) -> MaxDrawdown {
    // (peak index, trough index, value) of the deepest episode so far, per measure.
    let mut by_amount: Option<(usize, usize, f64)> = None;
    let mut by_fraction: Option<(usize, usize, f64)> = None;
    let mut peak = 0;
    for (i, &(_, equity)) in curve.iter().enumerate() {
        if equity > curve[peak].1 {
            peak = i;
        }
        let amount = equity - curve[peak].1;
        if amount < by_amount.map_or(0.0, |b| b.2) {
            by_amount = Some((peak, i, amount));
        }
        let frac = fraction(equity, curve[peak].1);
        if frac < by_fraction.map_or(0.0, |b| b.2) {
            by_fraction = Some((peak, i, frac));
        }
    }
    let episode = |(p, t, _): (usize, usize, f64)| {
        let (peak, peak_equity) = curve[p];
        let (trough, trough_equity) = curve[t];
        Drawdown {
            amount: trough_equity - peak_equity,
            fraction: fraction(trough_equity, peak_equity),
            peak,
            peak_equity,
            trough,
            trough_equity,
            recovery: curve[t + 1..]
                .iter()
                .find(|(_, e)| *e >= peak_equity)
                .map(|(d, _)| *d),
        }
    };
    MaxDrawdown {
        by_amount: by_amount.map(episode),
        by_fraction: by_fraction.map(episode),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    fn dated(values: &[f64]) -> Vec<(NaiveDate, f64)> {
        let d0 = NaiveDate::from_ymd_opt(2024, 1, 1).unwrap();
        values
            .iter()
            .enumerate()
            .map(|(i, v)| (d0 + chrono::Days::new(i as u64), *v))
            .collect()
    }

    fn day(i: u64) -> NaiveDate {
        NaiveDate::from_ymd_opt(2024, 1, 1).unwrap() + chrono::Days::new(i)
    }

    #[test]
    fn pounds_and_percent_can_be_different_episodes() {
        let m = max_drawdown(&dated(&[
            25_000.0, 20_000.0, 30_000.0, 100_000.0, 90_000.0, 95_000.0,
        ]));
        let a = m.by_amount.unwrap();
        assert_eq!(
            (a.amount, a.peak, a.trough, a.recovery),
            (-10_000.0, day(3), day(4), None)
        );
        assert!((a.fraction + 0.1).abs() < 1e-15);
        let f = m.by_fraction.unwrap();
        assert_eq!((f.amount, f.peak, f.trough), (-5_000.0, day(0), day(1)));
        assert_eq!(f.fraction, -0.2);
        assert_eq!(f.recovery, Some(day(2)));
    }

    #[test]
    fn a_rising_curve_has_no_drawdown_and_equal_peaks_recover() {
        let m = max_drawdown(&dated(&[100.0, 100.0, 101.0]));
        assert_eq!(
            m,
            MaxDrawdown {
                by_amount: None,
                by_fraction: None
            }
        );
        // Recovery is the first point at or above the peak, not strictly above it.
        let m = max_drawdown(&dated(&[100.0, 90.0, 100.0]));
        assert_eq!(m.by_amount.unwrap().recovery, Some(day(2)));
    }

    #[test]
    fn ruin_clamps_the_fraction_and_keeps_the_amount() {
        let m = max_drawdown(&dated(&[1_000.0, 1_200.0, -300.0, 50.0]));
        let a = m.by_amount.unwrap();
        assert_eq!(a.amount, -1_500.0);
        assert_eq!(a.fraction, -1.0);
        assert_eq!((a.peak, a.trough, a.recovery), (day(1), day(2), None));
    }

    proptest! {
        /// Drawdowns are never gains and never deeper than −100%.
        #[test]
        fn drawdown_is_between_minus_one_and_zero(
            values in prop::collection::vec(-1_000.0f64..200_000.0, 1..300),
        ) {
            let m = max_drawdown(&dated(&values));
            for d in [&m.by_amount, &m.by_fraction].into_iter().flatten() {
                prop_assert!(d.amount < 0.0);
                prop_assert!((-1.0..0.0).contains(&d.fraction));
                prop_assert!(d.peak < d.trough);
                if let Some(r) = d.recovery {
                    prop_assert!(r > d.trough);
                }
            }
            // No drawdown exactly when the curve never falls below its running peak.
            let mut peak = f64::NEG_INFINITY;
            let falls = values.iter().any(|v| { peak = peak.max(*v); *v < peak });
            prop_assert_eq!(m.by_amount.is_some(), falls);
            prop_assert_eq!(m.by_fraction.is_some(), falls);
        }
    }
}
