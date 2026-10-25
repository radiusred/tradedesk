//! Overnight financing on the venue's real rollover calendar.
//!
//! A venue rolls positions once per trading day at a local wall-clock time (22:00
//! `Europe/London` at one example venue, 17:00 `America/New_York` at another), so the UTC instant moves with
//! that zone's daylight saving. A position is charged at a rollover when it is held
//! across it: opened strictly before the instant and still open strictly after it.
//!
//! Each calendar day is charged exactly once. There is no rollover on a local Saturday
//! or Sunday; instead, the asset class's triple day (Wednesday for spot FX, Friday for
//! indices; per venue for metals and energy) charges `days_on_triple` days, so a full
//! week charges seven. This replaces the Python loop that charged every calendar day and then tripled
//! Friday on top (`client.py:348-382`).
//!
//! The charge for one rollover is `|size| × mark × rate / day_count × days`. The mark is
//! the configured side's quote price at the rollover instant, and `rate` is the long or
//! short rate. A modelled carry ([`carry_charge`]) is charged at the same rollovers by the
//! same formula, with the rate of its table's row for the rollover's local date and its
//! own day count.
//!
//! A dividend ([`DividendClock`], [`dividend_charge`]) is charged at its own instant: a
//! local time on a weekday a configured number of weekdays before each ex-date, in its
//! own zone. A position held across that instant is credited (long) or debited (short)
//! the row's points times its side's share.

use chrono::{
    DateTime, Datelike, Days, LocalResult, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Utc,
    Weekday,
};
use chrono_tz::Tz;
use serde::{Deserialize, Serialize};

use super::config::{
    AssetClass, CarryRow, Dividend, DividendRow, Financing, RolloverSpec, WeekendRules,
};

/// Long or short.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Direction {
    /// Bought first; profits when the price rises.
    Long,
    /// Sold first; profits when the price falls.
    Short,
}

impl Direction {
    /// `+1.0` for long, `-1.0` for short.
    #[must_use]
    pub fn sign(self) -> f64 {
        match self {
            Self::Long => 1.0,
            Self::Short => -1.0,
        }
    }
}

/// One rollover a position was held across.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Rollover {
    /// The rollover instant in UTC.
    pub at_utc: DateTime<Utc>,
    /// The venue-local trading date it closes.
    pub local_date: NaiveDate,
    /// Calendar days it charges: `days_on_triple` on the triple day, otherwise 1.
    pub days: u32,
}

/// A venue's rollover clock and weekend rules.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RolloverCalendar {
    tz: Tz,
    time: NaiveTime,
    weekend: WeekendRules,
}

impl RolloverCalendar {
    /// A calendar from a venue's rollover spec and weekend rules.
    #[must_use]
    pub fn new(rollover: RolloverSpec, weekend: WeekendRules) -> Self {
        Self {
            tz: rollover.tz,
            time: rollover.time,
            weekend,
        }
    }

    /// The venue's time zone.
    #[must_use]
    pub fn tz(&self) -> Tz {
        self.tz
    }

    /// The UTC instant of the rollover that closes local trading date `date`.
    ///
    /// When the local time falls in a DST gap (never for 22:00 London or 17:00 New
    /// York), the first instant after the gap is used.
    #[must_use]
    pub fn instant_on(&self, date: NaiveDate) -> DateTime<Utc> {
        local_instant(self.tz, date, self.time)
    }

    /// Every rollover strictly after `after` and strictly before `before`, oldest first,
    /// with the days each one charges for `class`. Local Saturdays and Sundays have no
    /// rollover.
    ///
    /// # Panics
    /// If the calendar's weekend rules have no rule for `class` (an [`AssetClass::Energy`]
    /// instrument at a venue without `weekend.energy`). A loaded
    /// [`CostConfig`](super::config::CostConfig) rejects that venue, so a calendar built from
    /// one never panics for an instrument the venue costs.
    #[must_use]
    pub fn rollovers_between(
        &self,
        class: AssetClass,
        after: DateTime<Utc>,
        before: DateTime<Utc>,
    ) -> Vec<Rollover> {
        let mut out = Vec::new();
        if before <= after {
            return out;
        }
        let rule = self
            .weekend
            .for_class(class)
            .unwrap_or_else(|| panic!("the venue has no weekend rule for {class:?}"));
        let last = before.with_timezone(&self.tz).date_naive();
        let mut date = after.with_timezone(&self.tz).date_naive();
        while date <= last {
            let weekday = date.weekday();
            if !matches!(weekday, Weekday::Sat | Weekday::Sun) {
                let at_utc = self.instant_on(date);
                if at_utc > after && at_utc < before {
                    let days = if weekday == rule.triple_day.weekday() {
                        rule.days_on_triple
                    } else {
                        1
                    };
                    out.push(Rollover {
                        at_utc,
                        local_date: date,
                        days,
                    });
                }
            }
            match date.checked_add_days(Days::new(1)) {
                Some(next) => date = next,
                None => break,
            }
        }
        out
    }
}

/// The UTC instant of local `time` on `date` in `tz`. In a DST gap, the first instant
/// after the gap; in a repeated hour, the first of the two.
fn local_instant(tz: Tz, date: NaiveDate, time: NaiveTime) -> DateTime<Utc> {
    let local = NaiveDateTime::new(date, time);
    let resolved = match tz.from_local_datetime(&local) {
        LocalResult::Single(t) | LocalResult::Ambiguous(t, _) => t,
        LocalResult::None => {
            // Step forward a minute at a time to the end of the gap.
            let mut probe = local;
            loop {
                probe += chrono::Duration::minutes(1);
                if let Some(t) = tz.from_local_datetime(&probe).earliest() {
                    break t;
                }
            }
        }
    };
    resolved.with_timezone(&Utc)
}

/// One date whose dividend charge instant a position was held across.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DividendInstant {
    /// The ex-date (a candidate: the table says whether it has a dividend).
    pub ex_date: NaiveDate,
    /// The charge instant in UTC.
    pub at_utc: DateTime<Utc>,
}

/// When a dividend is charged: `time` in `tz` on the weekday `weekdays_before` weekdays
/// before each ex-date. Exchange holidays are not modelled.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DividendClock {
    tz: Tz,
    time: NaiveTime,
    weekdays_before: u32,
}

impl DividendClock {
    /// A clock from its zone, local time and weekdays before the ex-date.
    #[must_use]
    pub fn new(tz: Tz, time: NaiveTime, weekdays_before: u32) -> Self {
        Self {
            tz,
            time,
            weekdays_before,
        }
    }

    /// The clock of a dividend charged from a table; `None` when it charges nothing.
    #[must_use]
    pub fn of(dividend: &Dividend) -> Option<Self> {
        dividend.events.as_ref()?;
        Some(Self::new(
            dividend.tz?,
            dividend.time?,
            dividend.weekdays_before?,
        ))
    }

    /// The charge instant for ex-date `ex_date`.
    #[must_use]
    pub fn instant_for(&self, ex_date: NaiveDate) -> DateTime<Utc> {
        let mut date = ex_date;
        let mut left = self.weekdays_before;
        while left > 0 {
            date = date.pred_opt().unwrap_or(date);
            if !matches!(date.weekday(), Weekday::Sat | Weekday::Sun) {
                left -= 1;
            }
        }
        local_instant(self.tz, date, self.time)
    }

    /// Every date whose charge instant is strictly after `after` and strictly before
    /// `before`, oldest first. Every calendar date is a candidate; several can share one
    /// instant (with `weekdays_before = 1`, a Saturday, Sunday and Monday ex-date are all
    /// charged at Friday's).
    #[must_use]
    pub fn instants_between(
        &self,
        after: DateTime<Utc>,
        before: DateTime<Utc>,
    ) -> Vec<DividendInstant> {
        let mut out = Vec::new();
        if before <= after {
            return out;
        }
        // An instant is on or before its ex-date, and at most `weekdays_before` weekdays
        // (so at most `3 × weekdays_before + 3` calendar days, with the weekends) earlier.
        let first = after.with_timezone(&self.tz).date_naive().pred_opt();
        let last = before
            .with_timezone(&self.tz)
            .date_naive()
            .checked_add_days(Days::new(u64::from(3 * self.weekdays_before + 3)));
        let (Some(mut date), Some(last)) = (first, last) else {
            return out;
        };
        while date <= last {
            let at_utc = self.instant_for(date);
            if at_utc > after && at_utc < before {
                out.push(DividendInstant {
                    ex_date: date,
                    at_utc,
                });
            }
            match date.succ_opt() {
                Some(next) => date = next,
                None => break,
            }
        }
        out
    }
}

/// The dividend charged on a position held over the instant of `row`'s ex-date, in quote
/// currency: a long is credited `points × pip_size × long_share × |size|` (negative), a
/// short is debited `points × pip_size × short_share × |size|` (positive). Returns the
/// share applied and the charge.
#[must_use]
pub fn dividend_charge(
    row: &DividendRow,
    pip_size: f64,
    direction: Direction,
    size: f64,
) -> (f64, f64) {
    let (share, sign) = match direction {
        Direction::Long => (row.long_share, -1.0),
        Direction::Short => (row.short_share, 1.0),
    };
    let charge = sign * row.points * pip_size * share * size.abs();
    // A zero credit is 0, not -0.
    (share, if charge == 0.0 { 0.0 } else { charge })
}

/// The financing charged at one rollover, in quote currency: `|size| × mark × rate /
/// day_count × days`. Positive is a charge to the trader; negative is a credit.
#[must_use]
pub fn rollover_charge(
    financing: &Financing,
    direction: Direction,
    size: f64,
    mark: f64,
    days: u32,
) -> f64 {
    let rate = match direction {
        Direction::Long => financing.long_rate,
        Direction::Short => financing.short_rate,
    };
    size.abs() * mark * rate / f64::from(financing.day_count) * f64::from(days)
}

/// The carry charged at one rollover from its table row, in quote currency: the long or
/// short rate of `row` and `|size| × mark × rate / day_count × days`. Returns the rate
/// used and the charge; positive is a charge to the trader, negative a credit.
#[must_use]
pub fn carry_charge(
    row: &CarryRow,
    day_count: u32,
    direction: Direction,
    size: f64,
    mark: f64,
    days: u32,
) -> (f64, f64) {
    let rate = match direction {
        Direction::Long => row.long_rate,
        Direction::Short => row.short_rate,
    };
    (
        rate,
        size.abs() * mark * rate / f64::from(day_count) * f64::from(days),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cost::config::CostConfig;
    use proptest::prelude::*;

    fn calendar(venue: &str) -> RolloverCalendar {
        let v = CostConfig::builtin().venue(venue).unwrap().clone();
        RolloverCalendar::new(v.rollover, v.weekend)
    }

    fn utc(y: i32, mo: u32, d: u32, h: u32, mi: u32) -> DateTime<Utc> {
        Utc.with_ymd_and_hms(y, mo, d, h, mi, 0).unwrap()
    }

    fn days(rs: &[Rollover]) -> u32 {
        rs.iter().map(|r| r.days).sum()
    }

    #[test]
    fn plain_weekday_overnight_charges_one_day() {
        // Tuesday 2024-01-16 10:00Z to Wednesday 10:00Z: London rollover Tue 22:00 GMT.
        let london = calendar("example_spread_bet");
        let rs = london.rollovers_between(
            AssetClass::Index,
            utc(2024, 1, 16, 10, 0),
            utc(2024, 1, 17, 10, 0),
        );
        assert_eq!(rs.len(), 1);
        assert_eq!(rs[0].at_utc, utc(2024, 1, 16, 22, 0));
        assert_eq!(rs[0].days, 1);
        // Intraday: no rollover crossed.
        assert!(
            london
                .rollovers_between(
                    AssetClass::Index,
                    utc(2024, 1, 16, 8, 0),
                    utc(2024, 1, 16, 21, 59)
                )
                .is_empty()
        );
    }

    #[test]
    fn rollover_instant_itself_is_not_held_across() {
        let london = calendar("example_spread_bet");
        let r = utc(2024, 1, 16, 22, 0);
        assert!(
            london
                .rollovers_between(AssetClass::Fx, r, utc(2024, 1, 17, 10, 0))
                .is_empty()
        );
        assert!(
            london
                .rollovers_between(AssetClass::Fx, utc(2024, 1, 16, 10, 0), r)
                .is_empty()
        );
    }

    #[test]
    fn index_weekend_is_charged_on_friday_once() {
        // Friday 2024-01-19 12:00Z to Monday 2024-01-22 12:00Z.
        let london = calendar("example_spread_bet");
        let rs = london.rollovers_between(
            AssetClass::Index,
            utc(2024, 1, 19, 12, 0),
            utc(2024, 1, 22, 12, 0),
        );
        assert_eq!(rs.len(), 1);
        assert_eq!(
            rs[0].local_date,
            NaiveDate::from_ymd_opt(2024, 1, 19).unwrap()
        );
        assert_eq!(rs[0].days, 3);
        // Python charged 1 (Fri x3) + 1 (Sat) + 1 (Sun) = 5 here.
        assert_eq!(days(&rs), 3);
    }

    #[test]
    fn fx_triple_is_wednesday_and_its_weekend_is_free() {
        let london = calendar("example_spread_bet");
        let wed = london.rollovers_between(
            AssetClass::Fx,
            utc(2024, 1, 17, 12, 0),
            utc(2024, 1, 18, 12, 0),
        );
        assert_eq!(days(&wed), 3);
        let weekend = london.rollovers_between(
            AssetClass::Fx,
            utc(2024, 1, 19, 12, 0),
            utc(2024, 1, 22, 12, 0),
        );
        assert_eq!(days(&weekend), 1, "Friday's FX rollover is a single day");
    }

    #[test]
    fn metals_follow_each_venues_own_triple_day() {
        let fri_to_mon = (utc(2024, 1, 19, 12, 0), utc(2024, 1, 22, 12, 0));
        let london = calendar("example_spread_bet");
        assert_eq!(
            days(&london.rollovers_between(AssetClass::Metal, fri_to_mon.0, fri_to_mon.1)),
            3
        );
        let ny = calendar("example_ny_cfd");
        assert_eq!(
            days(&ny.rollovers_between(AssetClass::Metal, fri_to_mon.0, fri_to_mon.1)),
            1
        );
    }

    #[test]
    fn energy_follows_each_venues_own_triple_day() {
        let fri_to_mon = (utc(2024, 1, 19, 12, 0), utc(2024, 1, 22, 12, 0));
        let wed_to_thu = (utc(2024, 1, 17, 12, 0), utc(2024, 1, 18, 12, 0));
        let london = calendar("example_spread_bet");
        let ny = calendar("example_ny_spread_bet");
        for (cal, (a, b), expect) in [
            (&london, fri_to_mon, 3),
            (&london, wed_to_thu, 1),
            (&ny, fri_to_mon, 1),
            (&ny, wed_to_thu, 3),
        ] {
            assert_eq!(
                days(&cal.rollovers_between(AssetClass::Energy, a, b)),
                expect
            );
        }
    }

    #[test]
    #[should_panic(expected = "no weekend rule for Energy")]
    fn a_calendar_without_an_energy_rule_refuses_an_energy_instrument() {
        // The example New York CFD venue defines no `weekend.energy`.
        let ny = calendar("example_ny_cfd");
        let _ = ny.rollovers_between(
            AssetClass::Energy,
            utc(2024, 1, 19, 12, 0),
            utc(2024, 1, 22, 12, 0),
        );
    }

    /// Every class `venue` has a weekend rule for.
    fn classes(venue: &str) -> Vec<AssetClass> {
        let weekend = CostConfig::builtin().venue(venue).unwrap().weekend;
        [
            AssetClass::Fx,
            AssetClass::Metal,
            AssetClass::Index,
            AssetClass::Energy,
        ]
        .into_iter()
        .filter(|c| weekend.for_class(*c).is_some())
        .collect()
    }

    #[test]
    fn a_full_week_charges_seven_days_for_every_class_and_venue() {
        for venue in [
            "example_spread_bet",
            "example_ny_spread_bet",
            "example_ny_cfd",
        ] {
            let cal = calendar(venue);
            for class in classes(venue) {
                let rs =
                    cal.rollovers_between(class, utc(2024, 1, 15, 12, 0), utc(2024, 1, 22, 12, 0));
                assert_eq!(rs.len(), 5, "{venue} {class:?}");
                assert_eq!(days(&rs), 7, "{venue} {class:?}");
            }
        }
    }

    #[test]
    fn london_rollover_moves_with_bst() {
        let london = calendar("example_spread_bet");
        // UK clocks go forward Sunday 2024-03-31 and back Sunday 2024-10-27.
        assert_eq!(
            london.instant_on(NaiveDate::from_ymd_opt(2024, 3, 29).unwrap()),
            utc(2024, 3, 29, 22, 0)
        );
        assert_eq!(
            london.instant_on(NaiveDate::from_ymd_opt(2024, 4, 1).unwrap()),
            utc(2024, 4, 1, 21, 0)
        );
        assert_eq!(
            london.instant_on(NaiveDate::from_ymd_opt(2024, 10, 25).unwrap()),
            utc(2024, 10, 25, 21, 0)
        );
        assert_eq!(
            london.instant_on(NaiveDate::from_ymd_opt(2024, 10, 28).unwrap()),
            utc(2024, 10, 28, 22, 0)
        );
        // A position opened Monday 2024-04-01 21:30Z is past that day's 21:00Z rollover:
        // the GMT clock would have charged it, the BST clock does not.
        let rs = london.rollovers_between(
            AssetClass::Index,
            utc(2024, 4, 1, 21, 30),
            utc(2024, 4, 1, 23, 0),
        );
        assert!(rs.is_empty());
        // Across the spring change weekend: Friday GMT rollover, then Monday BST rollover.
        let rs = london.rollovers_between(
            AssetClass::Index,
            utc(2024, 3, 29, 12, 0),
            utc(2024, 4, 2, 12, 0),
        );
        assert_eq!(
            rs.iter().map(|r| r.at_utc).collect::<Vec<_>>(),
            vec![utc(2024, 3, 29, 22, 0), utc(2024, 4, 1, 21, 0)]
        );
        assert_eq!(days(&rs), 4);
    }

    #[test]
    fn new_york_rollover_moves_with_edt_including_the_weeks_london_disagrees() {
        let ny = calendar("example_ny_cfd");
        // US clocks go forward Sunday 2024-03-10 and back Sunday 2024-11-03.
        assert_eq!(
            ny.instant_on(NaiveDate::from_ymd_opt(2024, 3, 8).unwrap()),
            utc(2024, 3, 8, 22, 0)
        );
        assert_eq!(
            ny.instant_on(NaiveDate::from_ymd_opt(2024, 3, 11).unwrap()),
            utc(2024, 3, 11, 21, 0)
        );
        assert_eq!(
            ny.instant_on(NaiveDate::from_ymd_opt(2024, 11, 1).unwrap()),
            utc(2024, 11, 1, 21, 0)
        );
        assert_eq!(
            ny.instant_on(NaiveDate::from_ymd_opt(2024, 11, 4).unwrap()),
            utc(2024, 11, 4, 22, 0)
        );
        // 2024-03-11 to 2024-03-29 New York is on EDT and London on GMT: both venues roll
        // at 22:00 London on 2024-03-12, but the New York venues roll an hour earlier, 21:00Z.
        let london = calendar("example_spread_bet");
        let d = NaiveDate::from_ymd_opt(2024, 3, 12).unwrap();
        assert_eq!(ny.instant_on(d), utc(2024, 3, 12, 21, 0));
        assert_eq!(london.instant_on(d), utc(2024, 3, 12, 22, 0));
        // A position opened at 21:30Z that day is charged in London, not in New York.
        let (a, b) = (utc(2024, 3, 12, 21, 30), utc(2024, 3, 13, 9, 0));
        assert_eq!(london.rollovers_between(AssetClass::Index, a, b).len(), 1);
        assert!(ny.rollovers_between(AssetClass::Index, a, b).is_empty());
    }

    #[test]
    fn charge_uses_separate_long_short_rates_and_the_day_count() {
        let f360 = Financing {
            long_rate: 0.036,
            short_rate: -0.018,
            day_count: 360,
            source: String::new(),
            placeholder: false,
            excluded: false,
            reason: None,
        };
        // 2 units at 5000: notional 10,000.
        assert!((rollover_charge(&f360, Direction::Long, 2.0, 5000.0, 1) - 1.0).abs() < 1e-12);
        assert!((rollover_charge(&f360, Direction::Short, 2.0, 5000.0, 3) + 1.5).abs() < 1e-12);
        let f365 = Financing {
            day_count: 365,
            long_rate: 0.0365,
            ..f360
        };
        assert!((rollover_charge(&f365, Direction::Long, 2.0, 5000.0, 1) - 1.0).abs() < 1e-12);
    }

    fn new_york_close(weekdays_before: u32) -> DividendClock {
        DividendClock::new(
            chrono_tz::America::New_York,
            NaiveTime::from_hms_opt(16, 0, 0).unwrap(),
            weekdays_before,
        )
    }

    fn date(y: i32, m: u32, d: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, d).unwrap()
    }

    #[test]
    fn the_dividend_instant_counts_weekdays_back_and_moves_with_dst() {
        let before = new_york_close(1);
        // Wednesday 2024-01-17: Tuesday 16:00 EST is 21:00Z.
        assert_eq!(
            before.instant_for(date(2024, 1, 17)),
            utc(2024, 1, 16, 21, 0)
        );
        // Monday 2024-01-22: back over the weekend to Friday.
        assert_eq!(
            before.instant_for(date(2024, 1, 22)),
            utc(2024, 1, 19, 21, 0)
        );
        // Tuesday 2024-03-12: Monday 16:00 EDT is 20:00Z.
        assert_eq!(
            before.instant_for(date(2024, 3, 12)),
            utc(2024, 3, 11, 20, 0)
        );
        // 0: the ex-date itself; 3 from a Tuesday: the Thursday before.
        assert_eq!(
            new_york_close(0).instant_for(date(2024, 1, 17)),
            utc(2024, 1, 17, 21, 0)
        );
        assert_eq!(
            new_york_close(3).instant_for(date(2024, 1, 23)),
            utc(2024, 1, 18, 21, 0)
        );
    }

    #[test]
    fn the_instants_held_across_are_strict_and_share_the_friday_over_a_weekend() {
        let clock = new_york_close(1);
        let held = |a, b| {
            clock
                .instants_between(a, b)
                .into_iter()
                .map(|i| (i.ex_date, i.at_utc))
                .collect::<Vec<_>>()
        };
        // Friday 12:00Z to Monday 12:00Z: Friday 21:00Z is the instant of the Saturday,
        // Sunday and Monday ex-dates.
        let friday = utc(2024, 1, 19, 21, 0);
        assert_eq!(
            held(utc(2024, 1, 19, 12, 0), utc(2024, 1, 22, 12, 0)),
            vec![
                (date(2024, 1, 20), friday),
                (date(2024, 1, 21), friday),
                (date(2024, 1, 22), friday),
            ]
        );
        // Opened or closed at the instant itself: not held across it.
        let tuesday = utc(2024, 1, 16, 21, 0);
        assert!(held(tuesday, utc(2024, 1, 17, 12, 0)).is_empty());
        assert!(held(utc(2024, 1, 16, 12, 0), tuesday).is_empty());
        assert_eq!(
            held(
                utc(2024, 1, 16, 12, 0),
                tuesday + chrono::Duration::minutes(1)
            ),
            vec![(date(2024, 1, 17), tuesday)]
        );
    }

    #[test]
    fn a_long_is_credited_and_a_short_debited_its_share_of_the_points() {
        let row = DividendRow {
            date: date(2024, 1, 17),
            points: 1.5,
            long_share: 0.8,
            short_share: 1.1,
            bound: false,
        };
        let (share, long) = dividend_charge(&row, 1.0, Direction::Long, 2.0);
        assert_eq!(share, 0.8);
        assert!((long + 2.4).abs() < 1e-12);
        let (share, short) = dividend_charge(&row, 0.01, Direction::Short, -2.0);
        assert_eq!(share, 1.1);
        assert!((short - 0.033).abs() < 1e-12);
        // A zero share credits 0, not -0.
        let zero = DividendRow {
            long_share: 0.0,
            ..row
        };
        let (_, credit) = dividend_charge(&zero, 1.0, Direction::Long, 2.0);
        assert!(credit == 0.0 && credit.is_sign_positive());
    }

    proptest! {
        #[test]
        fn splitting_the_interval_never_charges_a_rollover_twice(
            start in 0i64..(3 * 365 * 24 * 60),
            len in 1i64..(40 * 24 * 60),
            cut_permille in 0i64..=1000,
            venue in prop_oneof![Just("example_spread_bet"), Just("example_ny_cfd")],
            class in prop_oneof![
                Just(AssetClass::Fx),
                Just(AssetClass::Metal),
                Just(AssetClass::Index),
                Just(AssetClass::Energy),
            ],
        ) {
            prop_assume!(classes(venue).contains(&class));
            let cal = calendar(venue);
            let base = utc(2022, 1, 1, 0, 0);
            let a = base + chrono::Duration::minutes(start);
            let b = a + chrono::Duration::minutes(len);
            let m = a + chrono::Duration::minutes(len * cut_permille / 1000);
            let whole = cal.rollovers_between(class, a, b);
            // The ledger advances `financed_through` to the last charged rollover, so a
            // split charges (a, m) and then (last charged or a, b).
            let first = cal.rollovers_between(class, a, m);
            let resume = first.last().map_or(a, |r| r.at_utc);
            let second = cal.rollovers_between(class, resume, b);
            let mut joined = first.clone();
            joined.extend(second);
            prop_assert_eq!(&joined, &whole);
            let mut instants: Vec<_> = joined.iter().map(|r| r.local_date).collect();
            instants.dedup();
            prop_assert_eq!(instants.len(), joined.len());
        }

        #[test]
        fn whole_weeks_charge_seven_days_per_week(
            start in 0i64..(3 * 365 * 24 * 60),
            weeks in 1i64..20,
            venue in prop_oneof![Just("example_spread_bet"), Just("example_ny_spread_bet")],
            class in prop_oneof![
                Just(AssetClass::Fx),
                Just(AssetClass::Metal),
                Just(AssetClass::Index),
                Just(AssetClass::Energy),
            ],
        ) {
            let cal = calendar(venue);
            let a = utc(2022, 1, 1, 0, 0) + chrono::Duration::minutes(start);
            let b = a + chrono::Duration::weeks(weeks);
            // A DST change inside the window moves the rollover by an hour, so an edge
            // within an hour of a rollover can gain or lose one; those edges are tested
            // explicitly above.
            let hour = chrono::Duration::hours(1);
            for edge in [a, b] {
                prop_assume!(cal.rollovers_between(class, edge - hour, edge + hour).is_empty());
            }
            let rs = cal.rollovers_between(class, a, b);
            #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
            let expect = (weeks * 7) as u32;
            prop_assert_eq!(days(&rs), expect);
        }
    }
}
