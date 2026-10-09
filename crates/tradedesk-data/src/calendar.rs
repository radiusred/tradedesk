//! Trading calendar — closed-form predicate (D2-06 / D2-07 / D2-08).
//!
//! FX-major default applies year-round; per-symbol overrides come via
//! [`crate::reader::Reader::trading_calendar`]. The shipped default is:
//!
//! - **Weekend closure:** Friday 22:00 UTC → Sunday 22:00 UTC closed every week.
//! - **Yearly holidays:** Dec 25 (Christmas Day) and Jan 1 (New Year's Day).
//! - **No other holidays in v1.** Country-specific bank holidays / Good Friday /
//!   Thanksgiving / etc. are per-symbol override concerns (Phase 3+).
//!
//! ## Performance budget (D2-08 / RESEARCH A4)
//!
//! [`Calendar::is_open_at`] is O(1), no allocation, inlineable. The gap detector
//! calls this once per minute over multi-year ranges (~3.2M calls per scan), so
//! the predicate must stay below ~100 ns/call. UTC-only — no `chrono-tz` lookup,
//! no localtime conversion. DST is invisible because Phase 2 buckets in UTC.
//!
//! ## Test surface
//!
//! Boundary tests below pin both ends of the weekly window (Friday 22:00 closes,
//! Sunday 22:00 opens) plus mid-week + Saturday + the two yearly holidays.

use chrono::{DateTime, Datelike, NaiveTime, Utc, Weekday};

/// Trading calendar. The FX-major default is constructed via [`Calendar::fx_major`].
///
/// `Clone` (NOT `Copy`) because the holiday list is a `Vec`. The `Weekday` fields in
/// `weekly_open_utc` / `weekly_close_utc` are stored for downstream introspection
/// (a Phase 3+ override calendar might use different days); the v1 predicate hardcodes
/// the Sun-open / Fri-close shape and ignores the `Weekday` field values.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Calendar {
    /// Weekly session open. v1 default: `(Sunday, 22:00 UTC)`.
    pub weekly_open_utc: (Weekday, NaiveTime),
    /// Weekly session close. v1 default: `(Friday, 22:00 UTC)`.
    pub weekly_close_utc: (Weekday, NaiveTime),
    /// `(month 1..=12, day 1..=31)` tuples applied every year. v1 default:
    /// `vec![(12, 25), (1, 1)]` (Christmas + New Year's Day).
    pub yearly_holidays: Vec<(u32, u32)>,
}

impl Calendar {
    /// The FX-major default — Friday 22:00 UTC → Sunday 22:00 UTC closed,
    /// plus Christmas Day (Dec 25) and New Year's Day (Jan 1) closed each year.
    ///
    /// # Panics
    /// Panics only if `NaiveTime::from_hms_opt(22, 0, 0)` returns `None`, which is
    /// statically impossible — `22:00:00` is a valid wall-clock time in every
    /// 24-hour day. The `.expect` is a compile-time guarantee in disguise.
    #[must_use]
    pub fn fx_major() -> Self {
        let twenty_two_hundred =
            NaiveTime::from_hms_opt(22, 0, 0).expect("22:00:00 is a valid NaiveTime");
        Self {
            weekly_open_utc: (Weekday::Sun, twenty_two_hundred),
            weekly_close_utc: (Weekday::Fri, twenty_two_hundred),
            yearly_holidays: vec![(12, 25), (1, 1)],
        }
    }

    /// Closed-form predicate. O(1), no allocation, inlineable.
    ///
    /// Returns `true` if the FX market is open at `ts` (UTC). The predicate:
    ///
    /// 1. Returns `false` if `(ts.month(), ts.day())` matches any
    ///    `yearly_holidays` entry.
    /// 2. Mon..=Thu: always open.
    /// 3. Friday: open while `ts.time() < weekly_close_utc.1` (closes AT 22:00 UTC).
    /// 4. Saturday: always closed.
    /// 5. Sunday: open while `ts.time() >= weekly_open_utc.1` (opens AT 22:00 UTC).
    ///
    /// The `Weekday` fields on `weekly_open_utc` / `weekly_close_utc` are stored for
    /// downstream introspection only — v1's predicate hardcodes Sun-open / Fri-close.
    #[inline]
    #[must_use]
    pub fn is_open_at(&self, ts: DateTime<Utc>) -> bool {
        let month = ts.month();
        let day = ts.day();
        for (m, d) in &self.yearly_holidays {
            if month == *m && day == *d {
                return false;
            }
        }
        // `DateTime<Utc>::time()` returns `NaiveTime` directly — no rebuild, no
        // panic surface, no allocation.
        let t = ts.time();
        match ts.weekday() {
            Weekday::Mon | Weekday::Tue | Weekday::Wed | Weekday::Thu => true,
            Weekday::Fri => t < self.weekly_close_utc.1,
            Weekday::Sat => false,
            Weekday::Sun => t >= self.weekly_open_utc.1,
        }
    }
}

impl Default for Calendar {
    /// `Calendar::default()` is [`Calendar::fx_major`] — so existing call sites that
    /// pre-date the real implementation (e.g., a Reader that previously instantiated
    /// the placeholder via `Calendar::new()`) get a sensible default value.
    fn default() -> Self {
        Self::fx_major()
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    fn at(year: i32, month: u32, day: u32, hour: u32, minute: u32) -> DateTime<Utc> {
        Utc.with_ymd_and_hms(year, month, day, hour, minute, 0)
            .single()
            .expect("valid UTC timestamp")
    }

    #[test]
    fn fx_major_shape_matches_d2_07() {
        let c = Calendar::fx_major();
        assert_eq!(c.weekly_open_utc.0, Weekday::Sun);
        assert_eq!(
            c.weekly_open_utc.1,
            NaiveTime::from_hms_opt(22, 0, 0).unwrap()
        );
        assert_eq!(c.weekly_close_utc.0, Weekday::Fri);
        assert_eq!(
            c.weekly_close_utc.1,
            NaiveTime::from_hms_opt(22, 0, 0).unwrap()
        );
        assert_eq!(c.yearly_holidays, vec![(12, 25), (1, 1)]);
    }

    #[test]
    fn default_is_fx_major() {
        assert_eq!(Calendar::default(), Calendar::fx_major());
    }

    #[test]
    fn christmas_day_is_closed() {
        // 2024-12-25 was a Wednesday — without the holiday rule the predicate would
        // return `true`. The holiday short-circuit overrides the weekday match.
        let c = Calendar::fx_major();
        assert!(!c.is_open_at(at(2024, 12, 25, 12, 0)));
    }

    #[test]
    fn new_years_day_is_closed() {
        // 2025-01-01 was a Wednesday.
        let c = Calendar::fx_major();
        assert!(!c.is_open_at(at(2025, 1, 1, 12, 0)));
    }

    #[test]
    fn friday_2200_utc_is_closed() {
        // 2024-06-14 was a Friday. At exactly 22:00:00 UTC the session is closed
        // (the predicate uses strict `<`).
        let c = Calendar::fx_major();
        assert!(!c.is_open_at(at(2024, 6, 14, 22, 0)));
    }

    #[test]
    fn friday_2159_utc_is_open() {
        // 2024-06-14 Friday 21:59 UTC — still inside the open window.
        let c = Calendar::fx_major();
        assert!(c.is_open_at(at(2024, 6, 14, 21, 59)));
    }

    #[test]
    fn sunday_2200_utc_is_open() {
        // 2024-06-16 was a Sunday. At exactly 22:00:00 UTC the session opens
        // (the predicate uses `>=`).
        let c = Calendar::fx_major();
        assert!(c.is_open_at(at(2024, 6, 16, 22, 0)));
    }

    #[test]
    fn sunday_2159_utc_is_closed() {
        // 2024-06-16 Sunday 21:59 UTC — still in the weekend closure window.
        let c = Calendar::fx_major();
        assert!(!c.is_open_at(at(2024, 6, 16, 21, 59)));
    }

    #[test]
    fn saturday_is_always_closed() {
        // 2024-06-15 was a Saturday — closed all day.
        let c = Calendar::fx_major();
        assert!(!c.is_open_at(at(2024, 6, 15, 12, 0)));
    }

    #[test]
    fn mid_week_is_open() {
        // 2024-06-12 was a Wednesday — open mid-day.
        let c = Calendar::fx_major();
        assert!(c.is_open_at(at(2024, 6, 12, 12, 0)));
    }
}
