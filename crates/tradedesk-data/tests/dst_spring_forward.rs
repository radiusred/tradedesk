//! Plan 02-03 Task 1 — DST spring-forward fixture (T-02-09).
//!
//! Closes one half of CACHE-04 success criterion 5's DST coverage: the 2024
//! London spring-forward transition. At `2024-03-31T01:00:00Z` UTC, London wall
//! clocks jump forward from `01:00 GMT` to `02:00 BST` — the wall-clock hour
//! between `01:00` and `02:00` local DOES NOT EXIST on that date.
//!
//! ## What this test proves
//!
//! Dukascopy timestamps are UTC throughout (02-RESEARCH.md §"CSV Schema"); the
//! wall-clock hour-skip is INVISIBLE in the UTC source data. The aggregator
//! works in UTC throughout (D2-11) — no `chrono-tz`, no localtime conversion
//! anywhere on the kernel hot path. The aggregator's output bars across the
//! transition therefore stay EVENLY SPACED in UTC, regardless of what wall
//! clocks were doing.
//!
//! A localtime leak (someone reaching for `chrono::Local`, a `chrono-tz`
//! lookup, or any other reactive timezone helper inside the kernel) would
//! manifest as a 60-minute jump between two consecutive 15m bars — the
//! `frame.ts_open_utc[i] - frame.ts_open_utc[i-1] == Duration::minutes(15)`
//! assertion would fail at the transition. That is the regression-gate
//! purpose of this file.
//!
//! ## Why integration (not unit)
//!
//! The PLAN.md scope mandates tests against the FROZEN `tradedesk_data::*`
//! re-export surface — `use tradedesk_data::{aggregate, ...}`, not
//! `use tradedesk_data::aggregator::...`. The integration test target enforces
//! this by linking against the published crate surface only. The shared
//! `MockReader` substrate lives at `tests/aggregator_fixtures.rs` (owned by
//! Plan 02-02 Task 2) and is pulled in via `mod aggregator_fixtures;`.

mod aggregator_fixtures;

use chrono::{Duration, NaiveDate, TimeZone, Utc};

use tradedesk_data::{AggParams, ClosedRangeUtc, Side, Timeframe, aggregate};

use crate::aggregator_fixtures::{MockReader, build_24h_1m_bars, day_start_utc};

/// The 2024 London spring-forward transition UTC instant. London wall clocks
/// jumped from `01:00 GMT` to `02:00 BST` at this UTC moment; UTC time itself
/// is continuous through the transition. Held as a function (not a const) so
/// the chrono `Utc.with_ymd_and_hms` call returns a runtime value without
/// pulling `once_cell` into the test target.
fn spring_forward_transition_utc() -> chrono::DateTime<Utc> {
    Utc.with_ymd_and_hms(2024, 3, 31, 1, 0, 0)
        .single()
        .expect("2024-03-31T01:00:00Z is a valid UTC instant")
}

/// Build a `MockReader` with 1m bars for `EURUSD bid` on the three days that
/// straddle the 2024 London spring-forward transition (Sat 30 / Sun 31 / Mon 1).
/// 4320 bars total — every UTC minute present, no gaps.
fn build_three_day_mock() -> MockReader {
    let day0 = NaiveDate::from_ymd_opt(2024, 3, 30).expect("2024-03-30 is a valid date");
    let day1 = NaiveDate::from_ymd_opt(2024, 3, 31).expect("2024-03-31 is a valid date");
    let day2 = NaiveDate::from_ymd_opt(2024, 4, 1).expect("2024-04-01 is a valid date");

    let mut mock = MockReader::new();
    mock.insert_day("EURUSD", Side::Bid, day0, build_24h_1m_bars(day0, 1.0));
    mock.insert_day("EURUSD", Side::Bid, day1, build_24h_1m_bars(day1, 1.0));
    mock.insert_day("EURUSD", Side::Bid, day2, build_24h_1m_bars(day2, 1.0));
    mock
}

/// Half-open `[2024-03-30T00:00:00Z, 2024-04-02T00:00:00Z)` UTC range — three
/// whole days spanning the DST transition.
fn three_day_range() -> ClosedRangeUtc {
    let start = day_start_utc(NaiveDate::from_ymd_opt(2024, 3, 30).expect("valid"));
    let end = start + Duration::hours(72);
    ClosedRangeUtc { start, end }
}

#[test]
fn bars_evenly_spaced_across_spring_forward() {
    let mock = build_three_day_mock();
    let frame = aggregate(
        &mock,
        AggParams {
            symbol: "EURUSD",
            side: Side::Bid,
            tf: Timeframe::Tf15m,
            range: three_day_range(),
        },
    )
    .expect("aggregate ok");

    // 3 days × 96 buckets/day = 288.
    assert_eq!(
        frame.len(),
        288,
        "3-day spring-forward range at 15m must emit 288 bars (got {})",
        frame.len()
    );

    // Strict 15-minute UTC spacing — no DST gap, no DST duplicate. A localtime
    // leak would surface here as a 60-minute jump between the bar at
    // 01:00 UTC and the next bar.
    for i in 1..frame.len() {
        let delta = frame.ts_open_utc[i] - frame.ts_open_utc[i - 1];
        assert_eq!(
            delta,
            Duration::minutes(15),
            "spring-forward 15m: non-uniform spacing at bar {i}: {} -> {}",
            frame.ts_open_utc[i - 1],
            frame.ts_open_utc[i]
        );
    }

    // Pin the transition: the bar at the UTC transition instant (01:00 UTC on
    // 2024-03-31) must exist, and the very next bar must be exactly 15 minutes
    // later in UTC (NOT 1h15m, which would indicate a localtime leak).
    let transition = spring_forward_transition_utc();
    let idx = frame
        .ts_open_utc
        .iter()
        .position(|t| *t == transition)
        .expect("bar at 2024-03-31T01:00:00Z must be present");
    assert!(
        idx + 1 < frame.len(),
        "transition bar must not be the last bar"
    );
    assert_eq!(
        frame.ts_open_utc[idx + 1],
        transition + Duration::minutes(15),
        "next bar after spring-forward transition must be +15m UTC (localtime leak?)"
    );
}

#[test]
fn bars_evenly_spaced_across_spring_forward_1h() {
    let mock = build_three_day_mock();
    let frame = aggregate(
        &mock,
        AggParams {
            symbol: "EURUSD",
            side: Side::Bid,
            tf: Timeframe::Tf1h,
            range: three_day_range(),
        },
    )
    .expect("aggregate ok");

    // 3 × 24 = 72.
    assert_eq!(
        frame.len(),
        72,
        "3-day spring-forward range at 1h must emit 72 bars (got {})",
        frame.len()
    );

    for i in 1..frame.len() {
        let delta = frame.ts_open_utc[i] - frame.ts_open_utc[i - 1];
        assert_eq!(
            delta,
            Duration::hours(1),
            "spring-forward 1h: non-uniform spacing at bar {i}: {} -> {}",
            frame.ts_open_utc[i - 1],
            frame.ts_open_utc[i]
        );
    }

    let transition = spring_forward_transition_utc();
    let idx = frame
        .ts_open_utc
        .iter()
        .position(|t| *t == transition)
        .expect("1h bar at 2024-03-31T01:00:00Z must be present");
    assert!(
        idx + 1 < frame.len(),
        "transition bar must not be the last 1h bar"
    );
    assert_eq!(
        frame.ts_open_utc[idx + 1],
        transition + Duration::hours(1),
        "next 1h bar after spring-forward must be +1h UTC, not +2h (localtime leak?)"
    );
}

#[test]
fn bars_evenly_spaced_across_spring_forward_1d() {
    let mock = build_three_day_mock();
    let frame = aggregate(
        &mock,
        AggParams {
            symbol: "EURUSD",
            side: Side::Bid,
            tf: Timeframe::Tf1d,
            range: three_day_range(),
        },
    )
    .expect("aggregate ok");

    assert_eq!(frame.len(), 3, "3-day range at Tf1d must emit 3 bars");

    let day0_open = day_start_utc(NaiveDate::from_ymd_opt(2024, 3, 30).expect("valid"));
    let day1_open = day_start_utc(NaiveDate::from_ymd_opt(2024, 3, 31).expect("valid"));
    let day2_open = day_start_utc(NaiveDate::from_ymd_opt(2024, 4, 1).expect("valid"));
    assert_eq!(
        frame.ts_open_utc,
        vec![day0_open, day1_open, day2_open],
        "1d bar opens must be UTC-midnight of Mar 30, Mar 31, Apr 1"
    );

    // The Mar 31 bar covers a 24-UTC-hour window even though it spans the
    // DST transition — the aggregator emits `ts_close_utc = ts_open + 24h`
    // regardless. Pin it.
    assert_eq!(
        frame.ts_close_utc[1],
        day1_open + Duration::hours(24),
        "1d bar on DST day must still be exactly 24 UTC hours wide"
    );
}
