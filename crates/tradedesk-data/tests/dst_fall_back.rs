//! Plan 02-03 Task 2 — DST fall-back fixture (T-02-09).
//!
//! Closes the other half of CACHE-04 success criterion 5's DST coverage: the
//! 2024 London fall-back transition. At `2024-10-27T01:00:00Z` UTC, London
//! wall clocks jump back from `02:00 BST` to `01:00 GMT` — the wall-clock
//! hour between `01:00 GMT` and `02:00 BST` is REPEATED on that date.
//!
//! ## What this test proves
//!
//! Same shape as `dst_spring_forward.rs`, opposite direction. Dukascopy
//! timestamps are UTC throughout, so the wall-clock hour-repeat is INVISIBLE
//! in the UTC source data. The aggregator works in UTC throughout (D2-11) —
//! the output bars across the transition stay evenly spaced in UTC, no
//! duplication, no double-counting of the "repeated" hour.
//!
//! A localtime leak that confused the wall-clock-repeated hour with two UTC
//! hours would manifest as duplicate `ts_open_utc` entries OR a zero-length
//! delta between consecutive bars. The
//! `frame.ts_open_utc[i] - frame.ts_open_utc[i-1] == tf.duration()`
//! assertion catches both classes.
//!
//! ## Why two files instead of one
//!
//! Spring-forward and fall-back exercise different mental-model failures:
//!   - spring-forward: off-by-one around "the missing wall-clock hour"
//!   - fall-back: duplicate-handling around "the repeated wall-clock hour"
//!
//! The aggregator is UTC-only so neither bug class applies — but separate
//! files let `cargo test` run them in parallel without serial deps, and a
//! future regression in either direction surfaces in the file named after
//! that direction.

mod aggregator_fixtures;

use chrono::{Duration, NaiveDate, TimeZone, Utc};

use tradedesk_data::{AggParams, ClosedRangeUtc, Side, Timeframe, aggregate};

use crate::aggregator_fixtures::{MockReader, build_24h_1m_bars, day_start_utc};

/// The 2024 London fall-back transition UTC instant. London wall clocks
/// jumped from `02:00 BST` back to `01:00 GMT` at this UTC moment; UTC time
/// is continuous through the transition.
fn fall_back_transition_utc() -> chrono::DateTime<Utc> {
    Utc.with_ymd_and_hms(2024, 10, 27, 1, 0, 0)
        .single()
        .expect("2024-10-27T01:00:00Z is a valid UTC instant")
}

/// Build a `MockReader` with 1m bars for `EURUSD bid` on the three days that
/// straddle the 2024 London fall-back transition (Sat 26 / Sun 27 / Mon 28).
/// 4320 bars total — every UTC minute present, no gaps.
fn build_three_day_mock() -> MockReader {
    let day0 = NaiveDate::from_ymd_opt(2024, 10, 26).expect("2024-10-26 is a valid date");
    let day1 = NaiveDate::from_ymd_opt(2024, 10, 27).expect("2024-10-27 is a valid date");
    let day2 = NaiveDate::from_ymd_opt(2024, 10, 28).expect("2024-10-28 is a valid date");

    let mut mock = MockReader::new();
    mock.insert_day("EURUSD", Side::Bid, day0, build_24h_1m_bars(day0, 1.0));
    mock.insert_day("EURUSD", Side::Bid, day1, build_24h_1m_bars(day1, 1.0));
    mock.insert_day("EURUSD", Side::Bid, day2, build_24h_1m_bars(day2, 1.0));
    mock
}

/// Half-open `[2024-10-26T00:00:00Z, 2024-10-29T00:00:00Z)` UTC range —
/// three whole days spanning the DST fall-back transition.
fn three_day_range() -> ClosedRangeUtc {
    let start = day_start_utc(NaiveDate::from_ymd_opt(2024, 10, 26).expect("valid"));
    let end = start + Duration::hours(72);
    ClosedRangeUtc { start, end }
}

#[test]
fn bars_evenly_spaced_across_fall_back() {
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
        "3-day fall-back range at 15m must emit 288 bars (got {})",
        frame.len()
    );

    // Strict 15-minute UTC spacing — no duplicate at the wall-clock repeat,
    // no zero-length delta. A localtime leak that double-counted the
    // repeated wall-clock hour would surface here as a `Duration::zero()`.
    for i in 1..frame.len() {
        let delta = frame.ts_open_utc[i] - frame.ts_open_utc[i - 1];
        assert_eq!(
            delta,
            Duration::minutes(15),
            "fall-back 15m: non-uniform spacing at bar {i}: {} -> {}",
            frame.ts_open_utc[i - 1],
            frame.ts_open_utc[i]
        );
    }

    // Pin the transition: the bar at the UTC transition instant (01:00 UTC on
    // 2024-10-27) must exist exactly once, and the very next bar must be
    // exactly 15 minutes later in UTC (NOT at the same instant, which would
    // indicate the repeated wall-clock hour leaked into UTC bucketing).
    let transition = fall_back_transition_utc();
    let count_at_transition = frame
        .ts_open_utc
        .iter()
        .filter(|t| **t == transition)
        .count();
    assert_eq!(
        count_at_transition, 1,
        "bar at 2024-10-27T01:00:00Z must appear exactly ONCE (got {count_at_transition} — repeated-hour leak?)"
    );
    let idx = frame
        .ts_open_utc
        .iter()
        .position(|t| *t == transition)
        .expect("bar at 2024-10-27T01:00:00Z must be present");
    assert!(
        idx + 1 < frame.len(),
        "transition bar must not be the last bar"
    );
    assert_eq!(
        frame.ts_open_utc[idx + 1],
        transition + Duration::minutes(15),
        "next bar after fall-back transition must be +15m UTC (localtime leak?)"
    );
}

#[test]
fn bars_evenly_spaced_across_fall_back_5m() {
    let mock = build_three_day_mock();
    let frame = aggregate(
        &mock,
        AggParams {
            symbol: "EURUSD",
            side: Side::Bid,
            tf: Timeframe::Tf5m,
            range: three_day_range(),
        },
    )
    .expect("aggregate ok");

    // 3 days × (1440 / 5) buckets/day = 3 × 288 = 864.
    assert_eq!(
        frame.len(),
        864,
        "3-day fall-back range at 5m must emit 864 bars (got {})",
        frame.len()
    );

    // Strict 5-minute UTC spacing — no duplicate at the wall-clock repeat.
    for i in 1..frame.len() {
        let delta = frame.ts_open_utc[i] - frame.ts_open_utc[i - 1];
        assert_eq!(
            delta,
            Duration::minutes(5),
            "fall-back 5m: non-uniform spacing at bar {i}: {} -> {}",
            frame.ts_open_utc[i - 1],
            frame.ts_open_utc[i]
        );
    }

    let transition = fall_back_transition_utc();
    let count_at_transition = frame
        .ts_open_utc
        .iter()
        .filter(|t| **t == transition)
        .count();
    assert_eq!(
        count_at_transition, 1,
        "5m bar at 2024-10-27T01:00:00Z must appear exactly ONCE"
    );
    let idx = frame
        .ts_open_utc
        .iter()
        .position(|t| *t == transition)
        .expect("5m bar at 2024-10-27T01:00:00Z must be present");
    assert_eq!(
        frame.ts_open_utc[idx + 1],
        transition + Duration::minutes(5),
        "next 5m bar after fall-back must be +5m UTC (localtime leak?)"
    );
}

#[test]
fn bars_evenly_spaced_across_fall_back_10m() {
    let mock = build_three_day_mock();
    let frame = aggregate(
        &mock,
        AggParams {
            symbol: "EURUSD",
            side: Side::Bid,
            tf: Timeframe::Tf10m,
            range: three_day_range(),
        },
    )
    .expect("aggregate ok");

    // 3 days × (1440 / 10) buckets/day = 3 × 144 = 432.
    assert_eq!(
        frame.len(),
        432,
        "3-day fall-back range at 10m must emit 432 bars (got {})",
        frame.len()
    );

    for i in 1..frame.len() {
        let delta = frame.ts_open_utc[i] - frame.ts_open_utc[i - 1];
        assert_eq!(
            delta,
            Duration::minutes(10),
            "fall-back 10m: non-uniform spacing at bar {i}: {} -> {}",
            frame.ts_open_utc[i - 1],
            frame.ts_open_utc[i]
        );
    }

    let transition = fall_back_transition_utc();
    let count_at_transition = frame
        .ts_open_utc
        .iter()
        .filter(|t| **t == transition)
        .count();
    assert_eq!(
        count_at_transition, 1,
        "10m bar at 2024-10-27T01:00:00Z must appear exactly ONCE"
    );
    let idx = frame
        .ts_open_utc
        .iter()
        .position(|t| *t == transition)
        .expect("10m bar at 2024-10-27T01:00:00Z must be present");
    assert_eq!(
        frame.ts_open_utc[idx + 1],
        transition + Duration::minutes(10),
        "next 10m bar after fall-back must be +10m UTC (localtime leak?)"
    );
}

#[test]
fn bars_evenly_spaced_across_fall_back_1h() {
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
        "3-day fall-back range at 1h must emit 72 bars (got {})",
        frame.len()
    );

    for i in 1..frame.len() {
        let delta = frame.ts_open_utc[i] - frame.ts_open_utc[i - 1];
        assert_eq!(
            delta,
            Duration::hours(1),
            "fall-back 1h: non-uniform spacing at bar {i}: {} -> {}",
            frame.ts_open_utc[i - 1],
            frame.ts_open_utc[i]
        );
    }

    let transition = fall_back_transition_utc();
    let count_at_transition = frame
        .ts_open_utc
        .iter()
        .filter(|t| **t == transition)
        .count();
    assert_eq!(
        count_at_transition, 1,
        "1h bar at 2024-10-27T01:00:00Z must appear exactly ONCE"
    );
    let idx = frame
        .ts_open_utc
        .iter()
        .position(|t| *t == transition)
        .expect("1h bar at 2024-10-27T01:00:00Z must be present");
    assert!(
        idx + 1 < frame.len(),
        "transition 1h bar must not be the last bar"
    );
    assert_eq!(
        frame.ts_open_utc[idx + 1],
        transition + Duration::hours(1),
        "next 1h bar after fall-back must be +1h UTC (localtime leak?)"
    );
}

#[test]
fn bars_evenly_spaced_across_fall_back_1d() {
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

    assert_eq!(
        frame.len(),
        3,
        "3-day fall-back range at Tf1d must emit 3 bars"
    );

    let day0_open = day_start_utc(NaiveDate::from_ymd_opt(2024, 10, 26).expect("valid"));
    let day1_open = day_start_utc(NaiveDate::from_ymd_opt(2024, 10, 27).expect("valid"));
    let day2_open = day_start_utc(NaiveDate::from_ymd_opt(2024, 10, 28).expect("valid"));
    assert_eq!(
        frame.ts_open_utc,
        vec![day0_open, day1_open, day2_open],
        "1d bar opens must be UTC-midnight of Oct 26, Oct 27, Oct 28"
    );

    // The Oct 27 bar covers a 24-UTC-hour window even though it spans the
    // DST fall-back — the aggregator emits `ts_close_utc = ts_open + 24h`
    // regardless of wall-clock behaviour.
    assert_eq!(
        frame.ts_close_utc[1],
        day1_open + Duration::hours(24),
        "1d bar on DST day must still be exactly 24 UTC hours wide"
    );
}
