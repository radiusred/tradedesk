//! Plan 02-03 Task 3 — aggregator edge-case fixtures (T-02-10).
//!
//! Closes parts 3..7/6 of CACHE-04 success criterion 5: weekend gap,
//! Christmas holiday, instrument first cache day, instrument last cache day,
//! and partial-bar session open. The DST halves (parts 1+2) live in
//! `tests/dst_spring_forward.rs` + `tests/dst_fall_back.rs`.
//!
//! ## Aggregator-side vs gap-manifest-side
//!
//! These tests verify the aggregator's **bar-emission** behaviour: if a
//! bucket has zero source 1m bars, the aggregator OMITS it (never zero-fills,
//! never interpolates). If a bucket has ≥1 source 1m bar, the aggregator
//! emits it with whatever OHLCV reduction the present bars produce.
//!
//! The aggregator itself does NOT consult [`tradedesk_data::Calendar`] to decide
//! which buckets to emit. The closed-hours / partial-bar tagging is Plan
//! 02-04's gap-manifest responsibility (a separate snapshot test). The
//! aggregator's omission rule is purely "no source bars → no output bar".
//!
//! That separation of concerns is what lets these edge-case fixtures be
//! local to this file: the test sets up the source data such that the
//! closed-hours buckets contain zero source bars, then asserts the
//! aggregator's output matches the open-hours buckets. Calendar predicate
//! invariants are tested separately in `src/calendar.rs::tests::*`.
//!
//! ## Integration test target
//!
//! All five tests run against the FROZEN `tradedesk_data::*` re-export surface
//! (no `tradedesk_data::aggregator::*` direct paths). The shared `MockReader`
//! substrate is owned by Plan 02-02 Task 2 at `tests/aggregator_fixtures.rs`.

// `clippy::float_cmp` — the partial-bar test asserts exact passthrough of
// f64 source-bar fields (open / high / low / close) into the aggregator's
// output columns. The aggregator performs no arithmetic on these values
// (only `max` / `min` / first / last selection), so byte-equality is the
// correct semantic — matches the existing pattern in
// `tests/aggregator_determinism.rs:21`.
#![allow(clippy::float_cmp)]

mod aggregator_fixtures;

use chrono::{Duration, NaiveDate, TimeZone, Utc};

use tradedesk_data::{AggParams, ClosedRangeUtc, RawBar, Side, Timeframe, aggregate};

use crate::aggregator_fixtures::{
    MockReader, build_24h_1m_bars, build_partial_day_1m_bars, day_start_utc,
};

// =============================================================================
// Weekend gap (T-02-10)
// =============================================================================

/// Source data has bars Mon..Fri 21:59 UTC, then NOTHING until Sun 22:00 UTC.
/// The aggregator emits 22 hourly bars Fri 00:00..21:00 plus 2 hourly bars Sun
/// 22:00..23:00 — 24 bars total, no bars in the closed-hours window. This is
/// the aggregator-side behaviour; Plan 02-04 owns the gap-manifest entry.
#[test]
fn weekend_gap_emits_no_bars() {
    let friday = NaiveDate::from_ymd_opt(2024, 6, 14).expect("2024-06-14 is a Friday");
    let sunday = NaiveDate::from_ymd_opt(2024, 6, 16).expect("2024-06-16 is a Sunday");

    // Friday 00:00..21:59 UTC = 22 hours × 60 minutes = 1320 1m bars.
    let friday_start = day_start_utc(friday);
    let friday_bars = build_partial_day_1m_bars(friday_start, 22 * 60, 1.0);

    // Sunday 22:00..23:59 UTC = 2 hours × 60 = 120 1m bars (markets re-open at
    // Sun 22:00 UTC per Calendar::fx_major).
    let sunday_2200_utc = Utc
        .with_ymd_and_hms(2024, 6, 16, 22, 0, 0)
        .single()
        .expect("2024-06-16T22:00:00Z is a valid UTC instant");
    let sunday_bars = build_partial_day_1m_bars(sunday_2200_utc, 2 * 60, 1.0);

    let mut mock = MockReader::new();
    mock.insert_day("EURUSD", Side::Bid, friday, friday_bars);
    mock.insert_day("EURUSD", Side::Bid, sunday, sunday_bars);

    let range = ClosedRangeUtc {
        start: day_start_utc(friday),
        end: day_start_utc(NaiveDate::from_ymd_opt(2024, 6, 17).expect("valid")),
    };

    let frame = aggregate(
        &mock,
        AggParams {
            symbol: "EURUSD",
            side: Side::Bid,
            tf: Timeframe::Tf1h,
            range,
        },
    )
    .expect("aggregate ok");

    // 22 Friday hours (00:00..21:00) + 2 Sunday hours (22:00, 23:00) = 24 bars.
    assert_eq!(
        frame.len(),
        24,
        "weekend gap: expected 24 hourly bars (22 Fri + 2 Sun), got {}",
        frame.len()
    );

    // NO bar exists with `ts_open_utc` inside the closed-hours window
    // `[Fri 22:00 UTC, Sun 22:00 UTC)`.
    let friday_22 = Utc
        .with_ymd_and_hms(2024, 6, 14, 22, 0, 0)
        .single()
        .expect("valid");
    let sunday_22 = sunday_2200_utc;
    let closed_hits: Vec<_> = frame
        .ts_open_utc
        .iter()
        .filter(|ts| **ts >= friday_22 && **ts < sunday_22)
        .collect();
    assert!(
        closed_hits.is_empty(),
        "weekend gap: aggregator emitted {} bar(s) inside the closed-hours window — first was {:?}",
        closed_hits.len(),
        closed_hits.first()
    );

    // Sanity: the last Friday bar (21:00 UTC) and the first Sunday bar (22:00 UTC) are present.
    let fri_2100 = Utc
        .with_ymd_and_hms(2024, 6, 14, 21, 0, 0)
        .single()
        .expect("valid");
    assert!(
        frame.ts_open_utc.contains(&fri_2100),
        "weekend gap: Friday 21:00 UTC bar must be present (last pre-close bar)"
    );
    assert!(
        frame.ts_open_utc.contains(&sunday_22),
        "weekend gap: Sunday 22:00 UTC bar must be present (re-open bar)"
    );
}

// =============================================================================
// Christmas Day holiday (T-02-10)
// =============================================================================

/// Dec 24 + Dec 26 have full days of 1m bars; Dec 25 has NONE (Christmas Day
/// closed per `Calendar::fx_major`). The aggregator emits the two daily bars
/// for the days that have data and OMITS Dec 25 — no zero-fill bar, no error.
#[test]
fn christmas_day_emits_no_bars() {
    let dec_24 = NaiveDate::from_ymd_opt(2024, 12, 24).expect("2024-12-24 is a valid date");
    let dec_26 = NaiveDate::from_ymd_opt(2024, 12, 26).expect("2024-12-26 is a valid date");

    let mut mock = MockReader::new();
    mock.insert_day("EURUSD", Side::Bid, dec_24, build_24h_1m_bars(dec_24, 1.0));
    mock.insert_day("EURUSD", Side::Bid, dec_26, build_24h_1m_bars(dec_26, 1.0));

    let range = ClosedRangeUtc {
        start: day_start_utc(dec_24),
        end: day_start_utc(NaiveDate::from_ymd_opt(2024, 12, 27).expect("valid")),
    };

    let frame = aggregate(
        &mock,
        AggParams {
            symbol: "EURUSD",
            side: Side::Bid,
            tf: Timeframe::Tf1d,
            range,
        },
    )
    .expect("aggregate ok");

    assert_eq!(
        frame.len(),
        2,
        "christmas: expected 2 daily bars (Dec 24 + Dec 26), got {}",
        frame.len()
    );

    let dec_25_open = day_start_utc(NaiveDate::from_ymd_opt(2024, 12, 25).expect("valid"));
    assert!(
        !frame.ts_open_utc.contains(&dec_25_open),
        "christmas: Dec 25 daily bar must NOT be present (aggregator omits empty buckets)"
    );

    // Sanity: the present bars are exactly Dec 24 + Dec 26.
    assert_eq!(frame.ts_open_utc[0], day_start_utc(dec_24));
    assert_eq!(frame.ts_open_utc[1], day_start_utc(dec_26));
}

// =============================================================================
// Instrument first cache day (T-02-10)
// =============================================================================

/// `MockReader` has bars for Jun 15 onwards; the requested range starts on
/// Jun 13. The aggregator must NOT error or panic — it simply emits no bars
/// for the days that pre-date the instrument's history.
#[test]
fn instrument_first_cache_day() {
    let jun_15 = NaiveDate::from_ymd_opt(2024, 6, 15).expect("valid");
    let jun_16 = NaiveDate::from_ymd_opt(2024, 6, 16).expect("valid");

    let mut mock = MockReader::new();
    mock.insert_day("EURUSD", Side::Bid, jun_15, build_24h_1m_bars(jun_15, 1.0));
    mock.insert_day("EURUSD", Side::Bid, jun_16, build_24h_1m_bars(jun_16, 1.0));

    // Range starts BEFORE the instrument's first available data (Jun 13).
    let range = ClosedRangeUtc {
        start: day_start_utc(NaiveDate::from_ymd_opt(2024, 6, 13).expect("valid")),
        end: day_start_utc(NaiveDate::from_ymd_opt(2024, 6, 17).expect("valid")),
    };

    let frame = aggregate(
        &mock,
        AggParams {
            symbol: "EURUSD",
            side: Side::Bid,
            tf: Timeframe::Tf1d,
            range,
        },
    )
    .expect("aggregate ok");

    assert_eq!(
        frame.len(),
        2,
        "first-cache-day: expected 2 daily bars (Jun 15 + Jun 16), got {}",
        frame.len()
    );

    // No bar for Jun 13 or Jun 14 — pre-history days are silently omitted.
    let jun_13_open = day_start_utc(NaiveDate::from_ymd_opt(2024, 6, 13).expect("valid"));
    let jun_14_open = day_start_utc(NaiveDate::from_ymd_opt(2024, 6, 14).expect("valid"));
    assert!(
        !frame.ts_open_utc.contains(&jun_13_open),
        "first-cache-day: Jun 13 must NOT have a bar (pre-history)"
    );
    assert!(
        !frame.ts_open_utc.contains(&jun_14_open),
        "first-cache-day: Jun 14 must NOT have a bar (pre-history)"
    );

    // The first present bar is at the first-available-day boundary.
    assert_eq!(frame.ts_open_utc[0], day_start_utc(jun_15));
    assert_eq!(frame.ts_open_utc[1], day_start_utc(jun_16));
}

// =============================================================================
// Instrument last cache day (T-02-10)
// =============================================================================

/// `MockReader` has bars only up to Jun 16; the requested range extends to
/// Jun 21. The aggregator emits bars for Jun 15 + Jun 16 only — no error, no
/// padding for the days after the instrument's history ends.
#[test]
fn instrument_last_cache_day() {
    let jun_15 = NaiveDate::from_ymd_opt(2024, 6, 15).expect("valid");
    let jun_16 = NaiveDate::from_ymd_opt(2024, 6, 16).expect("valid");

    let mut mock = MockReader::new();
    mock.insert_day("EURUSD", Side::Bid, jun_15, build_24h_1m_bars(jun_15, 1.0));
    mock.insert_day("EURUSD", Side::Bid, jun_16, build_24h_1m_bars(jun_16, 1.0));

    // Range extends well past the last-available day (Jun 21).
    let range = ClosedRangeUtc {
        start: day_start_utc(jun_15),
        end: day_start_utc(NaiveDate::from_ymd_opt(2024, 6, 21).expect("valid")),
    };

    let frame = aggregate(
        &mock,
        AggParams {
            symbol: "EURUSD",
            side: Side::Bid,
            tf: Timeframe::Tf1d,
            range,
        },
    )
    .expect("aggregate ok");

    assert_eq!(
        frame.len(),
        2,
        "last-cache-day: expected 2 daily bars (Jun 15 + Jun 16), got {}",
        frame.len()
    );

    // No bars for Jun 17..Jun 20 — post-history days are silently omitted.
    for d in 17..=20_u32 {
        let day_open = day_start_utc(NaiveDate::from_ymd_opt(2024, 6, d).expect("valid June date"));
        assert!(
            !frame.ts_open_utc.contains(&day_open),
            "last-cache-day: Jun {d} must NOT have a bar (post-history)"
        );
    }

    assert_eq!(frame.ts_open_utc[0], day_start_utc(jun_15));
    assert_eq!(frame.ts_open_utc[1], day_start_utc(jun_16));
}

// =============================================================================
// Partial-bar session open (T-02-10, D2-19)
// =============================================================================

/// Sunday 2024-06-16 market re-opens at 22:00 UTC, but the source data
/// available to the aggregator only starts at 22:08 — the first 8 minutes of
/// the 15m bucket `[22:00, 22:15)` have no source bars; 7 bars (22:08..22:14)
/// ARE present.
///
/// Per D2-19 the gap manifest (Plan 04) will flag this bucket as having
/// `>50%` of its sub-minutes missing (8/15 ≈ 53%). That's NOT this test's
/// concern. The aggregator's emit/omit rule is simpler: ≥1 source bar in
/// the bucket → emit. So the aggregator emits the 22:00 bar with OHLCV
/// reduced over the 7 present 1m bars.
#[test]
fn partial_bar_session_open() {
    let sunday = NaiveDate::from_ymd_opt(2024, 6, 16).expect("2024-06-16 is a Sunday");

    // 1m bars at 22:08, 22:09, ..., 22:14 — 7 bars, all inside the 15m bucket
    // `[22:00, 22:15)`. Open = 1.0 + 0 * 0.0001 = 1.0 (first bar's open).
    let start_2208 = Utc
        .with_ymd_and_hms(2024, 6, 16, 22, 8, 0)
        .single()
        .expect("valid");
    let partial_bars: Vec<RawBar> = build_partial_day_1m_bars(start_2208, 7, 1.0);
    assert_eq!(
        partial_bars.len(),
        7,
        "fixture sanity: 7 1m bars at 22:08..22:14"
    );

    let mut mock = MockReader::new();
    mock.insert_day("EURUSD", Side::Bid, sunday, partial_bars.clone());

    // Half-open range covers exactly the 22:00..22:30 window — two 15m buckets,
    // but only the first contains any source bars.
    let range = ClosedRangeUtc {
        start: Utc
            .with_ymd_and_hms(2024, 6, 16, 22, 0, 0)
            .single()
            .expect("valid"),
        end: Utc
            .with_ymd_and_hms(2024, 6, 16, 22, 30, 0)
            .single()
            .expect("valid"),
    };

    let frame = aggregate(
        &mock,
        AggParams {
            symbol: "EURUSD",
            side: Side::Bid,
            tf: Timeframe::Tf15m,
            range,
        },
    )
    .expect("aggregate ok");

    // Exactly ONE bucket emitted — the 22:00 one (partial). The 22:15 bucket
    // has zero source bars and is omitted (aggregator's gap-omission rule).
    assert_eq!(
        frame.len(),
        1,
        "partial-bar: expected 1 emitted bar (22:00 bucket); got {}",
        frame.len()
    );

    let bucket_open = Utc
        .with_ymd_and_hms(2024, 6, 16, 22, 0, 0)
        .single()
        .expect("valid");
    assert_eq!(frame.ts_open_utc[0], bucket_open);
    assert_eq!(frame.ts_close_utc[0], bucket_open + Duration::minutes(15));

    // open = first present bar's open (the 22:08 bar) per the OHLC reduction.
    // `build_partial_day_1m_bars` sets `bar[0].open = open + 0 * 0.0001 = 1.0`.
    assert_eq!(
        frame.open[0], partial_bars[0].open,
        "partial-bar: open must equal the first present 1m bar's open"
    );

    // tick_volume = sequential sum of the 7 present bars' tick_volume (each 1.0
    // per `build_partial_day_1m_bars`).
    let expected_volume: f64 = partial_bars.iter().map(|b| b.tick_volume).sum();
    assert!(
        (frame.tick_volume[0] - expected_volume).abs() < 1e-9,
        "partial-bar: tick_volume {} must equal sum of 7 source bars ({})",
        frame.tick_volume[0],
        expected_volume
    );

    // high / low / close are within the source-bar range — guards against an
    // accidental zero-fill or interpolation in the reduction.
    let src_high = partial_bars
        .iter()
        .map(|b| b.high)
        .fold(f64::NEG_INFINITY, f64::max);
    let src_low = partial_bars
        .iter()
        .map(|b| b.low)
        .fold(f64::INFINITY, f64::min);
    assert_eq!(frame.high[0], src_high);
    assert_eq!(frame.low[0], src_low);
    assert_eq!(
        frame.close[0],
        partial_bars.last().expect("non-empty").close
    );
}
