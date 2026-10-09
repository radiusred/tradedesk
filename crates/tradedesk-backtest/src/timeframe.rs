//! Bar timeframes the backtester serves: 1-minute passthrough plus the aggregated
//! timeframes the M1 strategies need.
//!
//! miner-core's [`Timeframe`] has no 1-minute variant (the scan engine never reads 1m
//! bars directly), so this enum adds `M1` and maps the rest onto the core enum. Bucketing
//! for every aggregated timeframe is miner-core's: UTC-aligned, labelled with the bucket
//! open, daily buckets at `00:00Z` (see the daily-anchoring Decision on issue #29).

use chrono::{DateTime, Duration, Utc};
use tradedesk_data::Timeframe;

/// Timeframe of a [`crate::JoinedSeries`].
///
/// Ordered by duration, so it can key a `BTreeMap` with the 1-minute series first.
/// Serialised as its wire form ([`BarTimeframe::as_str`]).
#[derive(
    Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, serde::Serialize, serde::Deserialize,
)]
pub enum BarTimeframe {
    /// 1-minute bars, passed through from the source cache unchanged.
    #[serde(rename = "1m")]
    M1,
    /// 5-minute bars.
    #[serde(rename = "5m")]
    M5,
    /// 15-minute bars.
    #[serde(rename = "15m")]
    M15,
    /// 1-hour bars.
    #[serde(rename = "1h")]
    H1,
    /// 1-day bars, UTC midnight to UTC midnight.
    #[serde(rename = "1d")]
    D1,
}

impl BarTimeframe {
    /// Every timeframe, in ascending duration order.
    pub const ALL: [Self; 5] = [Self::M1, Self::M5, Self::M15, Self::H1, Self::D1];

    /// Wire form: `"1m"`, `"5m"`, `"15m"`, `"1h"`, `"1d"` (matches miner-core's
    /// `Timeframe::as_str` for the shared variants).
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::M1 => "1m",
            Self::M5 => "5m",
            Self::M15 => "15m",
            Self::H1 => "1h",
            Self::D1 => "1d",
        }
    }

    /// Bar length. A bar opening at `t` covers `[t, t + duration)`.
    #[must_use]
    pub fn duration(self) -> Duration {
        match self {
            Self::M1 => Duration::minutes(1),
            Self::M5 => Duration::minutes(5),
            Self::M15 => Duration::minutes(15),
            Self::H1 => Duration::hours(1),
            Self::D1 => Duration::days(1),
        }
    }

    /// Open of the bucket containing `ts`: `ts` floored to a multiple of this timeframe
    /// since the Unix epoch, so 1-day buckets open at `00:00Z`. Matches miner-core's
    /// aggregator bucketing (UTC has no leap seconds in chrono's arithmetic).
    ///
    /// # Panics
    /// Never in practice: the floored instant is no earlier than chrono's minimum
    /// representable time when `ts` itself is representable.
    #[must_use]
    pub fn bucket_open(self, ts: DateTime<Utc>) -> DateTime<Utc> {
        let step = self.duration().num_seconds();
        let floored = ts.timestamp().div_euclid(step) * step;
        DateTime::from_timestamp(floored, 0).expect("floored instant is representable")
    }

    /// `true` when `ts` is exactly a bucket open for this timeframe.
    #[must_use]
    pub fn is_bucket_open(self, ts: DateTime<Utc>) -> bool {
        self.bucket_open(ts) == ts
    }

    /// The miner-core aggregator timeframe that builds this one, or `None` for the
    /// 1-minute passthrough.
    #[must_use]
    pub fn to_core(self) -> Option<Timeframe> {
        match self {
            Self::M1 => None,
            Self::M5 => Some(Timeframe::Tf5m),
            Self::M15 => Some(Timeframe::Tf15m),
            Self::H1 => Some(Timeframe::Tf1h),
            Self::D1 => Some(Timeframe::Tf1d),
        }
    }
}

impl std::fmt::Display for BarTimeframe {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn wire_form_matches_core_for_shared_variants() {
        for tf in BarTimeframe::ALL {
            if let Some(core) = tf.to_core() {
                assert_eq!(tf.as_str(), core.as_str());
                assert_eq!(tf.duration(), core.duration());
            }
        }
        assert_eq!(BarTimeframe::M1.as_str(), "1m");
        assert_eq!(BarTimeframe::M1.to_core(), None);
    }

    #[test]
    fn bucket_open_floors_to_utc_boundaries() {
        let ts = Utc.with_ymd_and_hms(2024, 1, 2, 13, 47, 31).unwrap();
        let at = |h, m| Utc.with_ymd_and_hms(2024, 1, 2, h, m, 0).unwrap();
        assert_eq!(BarTimeframe::M1.bucket_open(ts), at(13, 47));
        assert_eq!(BarTimeframe::M5.bucket_open(ts), at(13, 45));
        assert_eq!(BarTimeframe::M15.bucket_open(ts), at(13, 45));
        assert_eq!(BarTimeframe::H1.bucket_open(ts), at(13, 0));
        assert_eq!(BarTimeframe::D1.bucket_open(ts), at(0, 0));
        assert!(BarTimeframe::D1.is_bucket_open(at(0, 0)));
        assert!(!BarTimeframe::H1.is_bucket_open(at(13, 1)));
    }

    #[test]
    fn all_is_strictly_ascending_by_duration() {
        for pair in BarTimeframe::ALL.windows(2) {
            assert!(pair[0] < pair[1]);
            assert!(pair[0].duration() < pair[1].duration());
        }
    }
}
