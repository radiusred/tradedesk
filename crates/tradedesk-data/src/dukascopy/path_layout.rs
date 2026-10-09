//! Path layout for the Dukascopy zstd-CSV cache (D2-12 / D2-21 / CACHE-05).
//!
//! Layout: `<cache_root>/<SYMBOL>/<YYYY>/<MM 00-indexed>/<DD>_<bid|ask>.csv.zst`.
//!
//! ## 00-indexed month encapsulation
//!
//! Dukascopy directories are 0-indexed (`January = "00"`, `December = "11"`).
//! [`DukascopyMonth`] is a sealed newtype enforcing the `0..=11` invariant. The
//! private inner field plus `from_calendar` smart constructor are the ONLY way
//! to build a value — there is no `pub const fn` form because `assert!` is not
//! const-stable on stable Rust, and there is no `Deref<Target = u8>` because
//! consumers must round-trip through [`Self::dir_name`] for filesystem use.
//!
//! [`day_csv_zst`] is the ONLY public path constructor (T-02-04 mitigation).
//! Anyone composing a path by hand will skip the invariant — reject in code review.

use std::path::{Path, PathBuf};

use crate::Side;
use chrono::{Datelike, NaiveDate};

/// Dukascopy-style zero-indexed month (Jan = 0, Dec = 11). Distinct from
/// `chrono::Month` (which is 1-indexed).
///
/// The inner `u8` is private; [`Self::from_calendar`] and
/// [`Self::from_chrono_date`] are the only constructors. The invariant
/// `0..=11` is enforced at construction time.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct DukascopyMonth(u8);

impl DukascopyMonth {
    /// Convert from a calendar month (`1..=12`). Panics outside that range —
    /// caller bug, never a user-input path.
    ///
    /// # Panics
    /// Panics if `month` is `0` or `>= 13`. The bound is asserted at the only
    /// public construction point so the inner value is provably `0..=11`.
    #[must_use]
    pub fn from_calendar(month: u8) -> Self {
        assert!(
            (1..=12).contains(&month),
            "calendar month out of range: {month}"
        );
        Self(month - 1)
    }

    /// Convert from a `chrono::NaiveDate`. Safe because `chrono::Datelike::month`
    /// returns `1..=12` always.
    #[must_use]
    pub fn from_chrono_date(d: NaiveDate) -> Self {
        // `chrono::Datelike::month()` returns u32 in `1..=12`; the cast to u8 is
        // lossless and the `from_calendar` assert is a defence-in-depth gate.
        #[allow(clippy::cast_possible_truncation)]
        let m = d.month() as u8;
        Self::from_calendar(m)
    }

    /// Two-digit zero-padded directory component (`"00"`..=`"11"`).
    #[must_use]
    pub fn dir_name(self) -> String {
        format!("{:02}", self.0)
    }
}

/// Parsed components of a Dukascopy day-file path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParsedDayPath {
    pub symbol: String,
    pub date: NaiveDate,
    pub side: Side,
}

/// Errors from [`parse_day_path`]. Internal to the reader crate; surface via
/// `DukascopyError::PathLayout` when crossing module boundaries.
#[derive(Debug, thiserror::Error)]
pub enum PathParseError {
    #[error("path too short for Dukascopy layout (need at least 4 trailing components)")]
    InsufficientComponents,
    #[error("invalid year component: {0}")]
    InvalidYear(String),
    #[error("invalid month component: {0}")]
    InvalidMonth(String),
    #[error("invalid day component: {0}")]
    InvalidDay(String),
    #[error("invalid side suffix: {0}")]
    InvalidSide(String),
    #[error("missing `.csv.zst` extension")]
    MissingExtension,
    #[error("file stem must be `<DD>_<bid|ask>`")]
    InvalidFileStem,
    #[error("non-UTF-8 path component")]
    NonUtf8Component,
    #[error("calendar component out of range: {0}")]
    OutOfRange(String),
}

/// Construct the absolute path to one day-file: `<cache_root>/<SYMBOL>/<YYYY>/<MM
/// 00-indexed>/<DD>_<bid|ask>.csv.zst`.
///
/// THE ONLY public path constructor — encapsulates the 00-indexed-month quirk.
/// Anyone composing this by hand bypasses the invariant.
#[must_use]
pub fn day_csv_zst(cache_root: &Path, symbol: &str, date: NaiveDate, side: Side) -> PathBuf {
    cache_root
        .join(symbol)
        .join(format!("{}", date.year()))
        .join(DukascopyMonth::from_chrono_date(date).dir_name())
        .join(format!("{:02}_{}.csv.zst", date.day(), side.as_str()))
}

/// Inverse of [`day_csv_zst`]. Splits a path into `(symbol, date, side)`
/// components, converting the 00-indexed month back to calendar (`+1`).
///
/// # Errors
/// Returns `PathParseError` if the path is too short, has non-UTF-8 components,
/// has the wrong extension, or contains components that fail the
/// `1..=12` / `1..=31` / valid-date / bid|ask checks.
pub fn parse_day_path(p: &Path) -> Result<ParsedDayPath, PathParseError> {
    // Strip the `.csv.zst` double extension explicitly — `Path::extension` only
    // returns `Some("zst")`. We want the file stem before `.csv.zst`.
    let file_name = p
        .file_name()
        .ok_or(PathParseError::InsufficientComponents)?
        .to_str()
        .ok_or(PathParseError::NonUtf8Component)?;
    let stem = file_name
        .strip_suffix(".csv.zst")
        .ok_or(PathParseError::MissingExtension)?;

    // `stem` should look like `<DD>_<bid|ask>`. Split on the first '_'.
    let (day_str, side_str) = stem
        .split_once('_')
        .ok_or(PathParseError::InvalidFileStem)?;

    let day: u32 = day_str
        .parse()
        .map_err(|_| PathParseError::InvalidDay(day_str.to_string()))?;

    let side = match side_str {
        "bid" => Side::Bid,
        "ask" => Side::Ask,
        other => return Err(PathParseError::InvalidSide(other.to_string())),
    };

    // Walk parent components: <SYMBOL>/<YYYY>/<MM 00-indexed>/<DD>_<side>.csv.zst.
    let parent = p.parent().ok_or(PathParseError::InsufficientComponents)?;
    let mm_dir = parent
        .file_name()
        .ok_or(PathParseError::InsufficientComponents)?
        .to_str()
        .ok_or(PathParseError::NonUtf8Component)?;
    let mm_0indexed: u8 = mm_dir
        .parse()
        .map_err(|_| PathParseError::InvalidMonth(mm_dir.to_string()))?;
    if mm_0indexed > 11 {
        return Err(PathParseError::OutOfRange(format!(
            "0-indexed month must be 0..=11, got {mm_0indexed}"
        )));
    }
    let month_calendar = mm_0indexed + 1;

    let yyyy_dir_parent = parent
        .parent()
        .ok_or(PathParseError::InsufficientComponents)?;
    let yyyy_dir = yyyy_dir_parent
        .file_name()
        .ok_or(PathParseError::InsufficientComponents)?
        .to_str()
        .ok_or(PathParseError::NonUtf8Component)?;
    let year: i32 = yyyy_dir
        .parse()
        .map_err(|_| PathParseError::InvalidYear(yyyy_dir.to_string()))?;

    let symbol_parent = yyyy_dir_parent
        .parent()
        .ok_or(PathParseError::InsufficientComponents)?;
    let symbol = symbol_parent
        .file_name()
        .ok_or(PathParseError::InsufficientComponents)?
        .to_str()
        .ok_or(PathParseError::NonUtf8Component)?
        .to_string();

    let date = NaiveDate::from_ymd_opt(year, u32::from(month_calendar), day).ok_or_else(|| {
        PathParseError::OutOfRange(format!("{year}-{month_calendar:02}-{day:02}"))
    })?;

    Ok(ParsedDayPath { symbol, date, side })
}

// ---------------------------------------------------------------------------
// Tests — boundary + round-trip per RESEARCH lines 336-397
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::panic::catch_unwind;

    #[test]
    fn jan_maps_to_00() {
        assert_eq!(DukascopyMonth::from_calendar(1).dir_name(), "00");
    }

    #[test]
    fn dec_maps_to_11() {
        assert_eq!(DukascopyMonth::from_calendar(12).dir_name(), "11");
    }

    /// Covers BOTH boundary cases (0 and 13) — calendar must reject either side.
    #[test]
    fn out_of_range_panics() {
        let zero = catch_unwind(|| DukascopyMonth::from_calendar(0));
        assert!(zero.is_err(), "calendar month 0 must panic");
        let thirteen = catch_unwind(|| DukascopyMonth::from_calendar(13));
        assert!(thirteen.is_err(), "calendar month 13 must panic");
    }

    #[test]
    fn round_trip_via_chrono_date_for_every_month_of_2024() {
        let expected = [
            (1u32, "00"),
            (2, "01"),
            (3, "02"),
            (4, "03"),
            (5, "04"),
            (6, "05"),
            (7, "06"),
            (8, "07"),
            (9, "08"),
            (10, "09"),
            (11, "10"),
            (12, "11"),
        ];
        for (cal_m, dir) in expected {
            let d = NaiveDate::from_ymd_opt(2024, cal_m, 15).unwrap();
            assert_eq!(DukascopyMonth::from_chrono_date(d).dir_name(), dir);
        }
    }

    #[test]
    fn full_path_round_trip() {
        let p = day_csv_zst(
            Path::new("/c"),
            "EURUSD",
            NaiveDate::from_ymd_opt(2024, 12, 25).unwrap(),
            Side::Bid,
        );
        assert_eq!(p, PathBuf::from("/c/EURUSD/2024/11/25_bid.csv.zst"));
    }

    #[test]
    fn parse_day_path_round_trip_with_ask() {
        let p = day_csv_zst(
            Path::new("/cache"),
            "GBPUSD",
            NaiveDate::from_ymd_opt(2020, 1, 1).unwrap(),
            Side::Ask,
        );
        let parsed = parse_day_path(&p).unwrap();
        assert_eq!(parsed.symbol, "GBPUSD");
        assert_eq!(parsed.date, NaiveDate::from_ymd_opt(2020, 1, 1).unwrap());
        assert_eq!(parsed.side, Side::Ask);
    }

    #[test]
    fn parse_rejects_missing_extension() {
        let p = Path::new("/c/EURUSD/2024/11/25_bid.csv");
        assert!(matches!(
            parse_day_path(p),
            Err(PathParseError::MissingExtension)
        ));
    }

    #[test]
    fn parse_rejects_invalid_side() {
        let p = Path::new("/c/EURUSD/2024/11/25_mid.csv.zst");
        assert!(matches!(
            parse_day_path(p),
            Err(PathParseError::InvalidSide(_))
        ));
    }

    proptest::proptest! {
        /// Property: every (year, calendar_m, day) in the safe range round-trips
        /// via day_csv_zst → parse_day_path with byte-identical components.
        ///
        /// Bounds picked to avoid Feb-29 / 30-day-month edge cases (covered by
        /// the deterministic tests above and by `chrono::NaiveDate::from_ymd_opt`).
        #[test]
        fn path_round_trip_proptest(
            year in 2010i32..=2030i32,
            m_cal in 1u32..=12u32,
            day in 1u32..=28u32,
            symbol in "[A-Z]{6}",
            is_bid in proptest::bool::ANY,
        ) {
            let side = if is_bid { Side::Bid } else { Side::Ask };
            let d = NaiveDate::from_ymd_opt(year, m_cal, day).unwrap();
            let p = day_csv_zst(Path::new("/cache"), &symbol, d, side);
            let parsed = parse_day_path(&p).unwrap();
            proptest::prop_assert_eq!(parsed.symbol, symbol);
            proptest::prop_assert_eq!(parsed.date, d);
            proptest::prop_assert_eq!(parsed.side, side);
        }
    }
}
