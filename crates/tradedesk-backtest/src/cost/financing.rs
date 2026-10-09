//! Overnight financing on the venue's real rollover calendar.
//!
//! A venue rolls positions once per trading day at a local wall-clock time (IG 22:00
//! `Europe/London`, Pepperstone 17:00 `America/New_York`), so the UTC instant moves with
//! that zone's daylight saving. A position is charged at a rollover when it is held
//! across it: opened strictly before the instant and still open strictly after it.
//!
//! Each calendar day is charged exactly once. There is no rollover on a local Saturday
//! or Sunday; instead, the asset class's triple day (Wednesday for spot FX, Friday for
//! indices; per venue for metals) charges `days_on_triple` days, so a full week charges
//! seven. This replaces the Python loop that charged every calendar day and then tripled
//! Friday on top (`client.py:348-382`).
//!
//! The charge for one rollover is `|size| × mark × rate / day_count × days`. The mark is
//! the configured side's quote price at the rollover instant, and `rate` is the long or
//! short rate.

use chrono::{
    DateTime, Datelike, Days, LocalResult, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Utc,
    Weekday,
};
use chrono_tz::Tz;
use serde::{Deserialize, Serialize};

use super::config::{AssetClass, Financing, RolloverSpec, WeekendRules};

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
        let local = NaiveDateTime::new(date, self.time);
        let resolved = match self.tz.from_local_datetime(&local) {
            LocalResult::Single(t) | LocalResult::Ambiguous(t, _) => t,
            LocalResult::None => {
                // Step forward a minute at a time to the end of the gap.
                let mut probe = local;
                loop {
                    probe += chrono::Duration::minutes(1);
                    if let Some(t) = self.tz.from_local_datetime(&probe).earliest() {
                        break t;
                    }
                }
            }
        };
        resolved.with_timezone(&Utc)
    }

    /// Every rollover strictly after `after` and strictly before `before`, oldest first,
    /// with the days each one charges for `class`. Local Saturdays and Sundays have no
    /// rollover.
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
        let rule = self.weekend.for_class(class);
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
        // Tuesday 2024-01-16 10:00Z to Wednesday 10:00Z: IG rollover Tue 22:00 GMT.
        let ig = calendar("ig_spread_bet");
        let rs = ig.rollovers_between(
            AssetClass::Index,
            utc(2024, 1, 16, 10, 0),
            utc(2024, 1, 17, 10, 0),
        );
        assert_eq!(rs.len(), 1);
        assert_eq!(rs[0].at_utc, utc(2024, 1, 16, 22, 0));
        assert_eq!(rs[0].days, 1);
        // Intraday: no rollover crossed.
        assert!(
            ig.rollovers_between(
                AssetClass::Index,
                utc(2024, 1, 16, 8, 0),
                utc(2024, 1, 16, 21, 59)
            )
            .is_empty()
        );
    }

    #[test]
    fn rollover_instant_itself_is_not_held_across() {
        let ig = calendar("ig_spread_bet");
        let r = utc(2024, 1, 16, 22, 0);
        assert!(
            ig.rollovers_between(AssetClass::Fx, r, utc(2024, 1, 17, 10, 0))
                .is_empty()
        );
        assert!(
            ig.rollovers_between(AssetClass::Fx, utc(2024, 1, 16, 10, 0), r)
                .is_empty()
        );
    }

    #[test]
    fn index_weekend_is_charged_on_friday_once() {
        // Friday 2024-01-19 12:00Z to Monday 2024-01-22 12:00Z.
        let ig = calendar("ig_spread_bet");
        let rs = ig.rollovers_between(
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
        let ig = calendar("ig_spread_bet");
        let wed = ig.rollovers_between(
            AssetClass::Fx,
            utc(2024, 1, 17, 12, 0),
            utc(2024, 1, 18, 12, 0),
        );
        assert_eq!(days(&wed), 3);
        let weekend = ig.rollovers_between(
            AssetClass::Fx,
            utc(2024, 1, 19, 12, 0),
            utc(2024, 1, 22, 12, 0),
        );
        assert_eq!(days(&weekend), 1, "Friday's FX rollover is a single day");
    }

    #[test]
    fn metals_follow_each_venues_own_triple_day() {
        let fri_to_mon = (utc(2024, 1, 19, 12, 0), utc(2024, 1, 22, 12, 0));
        let ig = calendar("ig_spread_bet");
        assert_eq!(
            days(&ig.rollovers_between(AssetClass::Metal, fri_to_mon.0, fri_to_mon.1)),
            3
        );
        let pep = calendar("pepperstone_razor");
        assert_eq!(
            days(&pep.rollovers_between(AssetClass::Metal, fri_to_mon.0, fri_to_mon.1)),
            1
        );
    }

    #[test]
    fn a_full_week_charges_seven_days_for_every_class_and_venue() {
        for venue in [
            "ig_spread_bet",
            "pepperstone_spread_bet",
            "pepperstone_razor",
        ] {
            let cal = calendar(venue);
            for class in [AssetClass::Fx, AssetClass::Metal, AssetClass::Index] {
                let rs =
                    cal.rollovers_between(class, utc(2024, 1, 15, 12, 0), utc(2024, 1, 22, 12, 0));
                assert_eq!(rs.len(), 5, "{venue} {class:?}");
                assert_eq!(days(&rs), 7, "{venue} {class:?}");
            }
        }
    }

    #[test]
    fn london_rollover_moves_with_bst() {
        let ig = calendar("ig_spread_bet");
        // UK clocks go forward Sunday 2024-03-31 and back Sunday 2024-10-27.
        assert_eq!(
            ig.instant_on(NaiveDate::from_ymd_opt(2024, 3, 29).unwrap()),
            utc(2024, 3, 29, 22, 0)
        );
        assert_eq!(
            ig.instant_on(NaiveDate::from_ymd_opt(2024, 4, 1).unwrap()),
            utc(2024, 4, 1, 21, 0)
        );
        assert_eq!(
            ig.instant_on(NaiveDate::from_ymd_opt(2024, 10, 25).unwrap()),
            utc(2024, 10, 25, 21, 0)
        );
        assert_eq!(
            ig.instant_on(NaiveDate::from_ymd_opt(2024, 10, 28).unwrap()),
            utc(2024, 10, 28, 22, 0)
        );
        // A position opened Monday 2024-04-01 21:30Z is past that day's 21:00Z rollover:
        // the GMT clock would have charged it, the BST clock does not.
        let rs = ig.rollovers_between(
            AssetClass::Index,
            utc(2024, 4, 1, 21, 30),
            utc(2024, 4, 1, 23, 0),
        );
        assert!(rs.is_empty());
        // Across the spring change weekend: Friday GMT rollover, then Monday BST rollover.
        let rs = ig.rollovers_between(
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
        let pep = calendar("pepperstone_razor");
        // US clocks go forward Sunday 2024-03-10 and back Sunday 2024-11-03.
        assert_eq!(
            pep.instant_on(NaiveDate::from_ymd_opt(2024, 3, 8).unwrap()),
            utc(2024, 3, 8, 22, 0)
        );
        assert_eq!(
            pep.instant_on(NaiveDate::from_ymd_opt(2024, 3, 11).unwrap()),
            utc(2024, 3, 11, 21, 0)
        );
        assert_eq!(
            pep.instant_on(NaiveDate::from_ymd_opt(2024, 11, 1).unwrap()),
            utc(2024, 11, 1, 21, 0)
        );
        assert_eq!(
            pep.instant_on(NaiveDate::from_ymd_opt(2024, 11, 4).unwrap()),
            utc(2024, 11, 4, 22, 0)
        );
        // 2024-03-11 to 2024-03-29 New York is on EDT and London on GMT: both venues roll
        // at 22:00 London on 2024-03-12, but Pepperstone rolls an hour earlier, 21:00Z.
        let ig = calendar("ig_spread_bet");
        let d = NaiveDate::from_ymd_opt(2024, 3, 12).unwrap();
        assert_eq!(pep.instant_on(d), utc(2024, 3, 12, 21, 0));
        assert_eq!(ig.instant_on(d), utc(2024, 3, 12, 22, 0));
        // A position opened at 21:30Z that day is charged by IG, not by Pepperstone.
        let (a, b) = (utc(2024, 3, 12, 21, 30), utc(2024, 3, 13, 9, 0));
        assert_eq!(ig.rollovers_between(AssetClass::Index, a, b).len(), 1);
        assert!(pep.rollovers_between(AssetClass::Index, a, b).is_empty());
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

    proptest! {
        #[test]
        fn splitting_the_interval_never_charges_a_rollover_twice(
            start in 0i64..(3 * 365 * 24 * 60),
            len in 1i64..(40 * 24 * 60),
            cut_permille in 0i64..=1000,
            venue in prop_oneof![Just("ig_spread_bet"), Just("pepperstone_razor")],
            class in prop_oneof![Just(AssetClass::Fx), Just(AssetClass::Metal), Just(AssetClass::Index)],
        ) {
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
            venue in prop_oneof![Just("ig_spread_bet"), Just("pepperstone_spread_bet")],
            class in prop_oneof![Just(AssetClass::Fx), Just(AssetClass::Metal), Just(AssetClass::Index)],
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
