//! Plan 02-02 Task 3 — aggregator byte-identity gate (CACHE-04, T-02-05).
//!
//! Two-runs byte-identity test against the shared `MockReader` substrate. If a
//! future refactor introduces a hash-randomised map, a `rayon::par_iter` inside
//! the reduction, a clock read, or any other source of non-determinism, this
//! test breaks.
//!
//! Pattern: PATTERNS lines 794-823 (the analog `cli_streams.rs` Test 6 twice-run
//! byte-identity flow), with the byte-comparison shifted from filesystem bytes
//! to in-memory `Vec<f64>` / `Vec<DateTime<Utc>>` equality on the `BarFrame`
//! column vectors. Equality on `Vec<f64>` is byte-equal for finite, well-ordered
//! `f64` (the only NaN / infinity values would come from the source, and the
//! synthetic 1-day fixture builds only finite OHLC).
//!
//! Uses ONLY the public `tradedesk_data::*` re-export surface — extending the
//! FROZEN block in `lib.rs` is the way new types reach this test.
//!
//! `tests/aggregator_fixtures.rs` (Plan 02-02 Task 2) provides the shared
//! `MockReader`; we pull it in via `mod aggregator_fixtures;`.

#![allow(clippy::float_cmp)] // f64 byte-equality is the whole point of this test.

mod aggregator_fixtures;

use chrono::{Duration, NaiveDate, TimeZone, Utc};

use tradedesk_data::{AggParams, RawBar, Side, Timeframe, aggregate};

use crate::aggregator_fixtures::{MockReader, build_24h_1m_bars, whole_day_range};

/// Build a fresh `MockReader` carrying ONE day of 1440 synthetic 1-minute bars
/// for `EURUSD` bid on `2024-06-15`. Centralised so the two runs in
/// `byte_identical_two_runs` are guaranteed-identical fixtures.
fn fresh_eurusd_bid_one_day() -> (MockReader, NaiveDate) {
    let date = NaiveDate::from_ymd_opt(2024, 6, 15).expect("2024-06-15 is a valid date");
    let mut mock = MockReader::new();
    mock.insert_day("EURUSD", Side::Bid, date, build_24h_1m_bars(date, 1.0));
    (mock, date)
}

#[test]
fn byte_identical_two_runs() {
    // Run the aggregator twice against two equal-but-independent MockReaders.
    // If anything in the kernel is order-non-deterministic the equality fails.
    for tf in [Timeframe::Tf15m, Timeframe::Tf1h, Timeframe::Tf1d] {
        let (mock1, date) = fresh_eurusd_bid_one_day();
        let (mock2, _) = fresh_eurusd_bid_one_day();
        let range = whole_day_range(date);

        let frame1 = aggregate(
            &mock1,
            AggParams {
                symbol: "EURUSD",
                side: Side::Bid,
                tf,
                range,
            },
        )
        .expect("aggregate run 1 ok");
        let frame2 = aggregate(
            &mock2,
            AggParams {
                symbol: "EURUSD",
                side: Side::Bid,
                tf,
                range,
            },
        )
        .expect("aggregate run 2 ok");

        // Length first — if frame lengths diverge, column comparisons would
        // give noisy assertions.
        assert_eq!(
            frame1.len(),
            frame2.len(),
            "tf {tf:?}: frame lengths differ ({} vs {})",
            frame1.len(),
            frame2.len()
        );

        // Per-column byte-identity.
        assert_eq!(
            frame1.ts_open_utc, frame2.ts_open_utc,
            "tf {tf:?}: ts_open_utc differ"
        );
        assert_eq!(
            frame1.ts_close_utc, frame2.ts_close_utc,
            "tf {tf:?}: ts_close_utc differ"
        );
        assert_eq!(frame1.open, frame2.open, "tf {tf:?}: open differs");
        assert_eq!(frame1.high, frame2.high, "tf {tf:?}: high differs");
        assert_eq!(frame1.low, frame2.low, "tf {tf:?}: low differs");
        assert_eq!(frame1.close, frame2.close, "tf {tf:?}: close differs");
        // tick_volume is THE critical f64 sum test — non-associative addition
        // is reproducible iff the iteration order is reproducible.
        assert_eq!(
            frame1.tick_volume, frame2.tick_volume,
            "tf {tf:?}: tick_volume differs (sequential f64 sum not deterministic?)"
        );

        // Header fields.
        assert_eq!(frame1.source_id, frame2.source_id);
        assert_eq!(frame1.symbol, frame2.symbol);
        assert_eq!(frame1.side, frame2.side);
        assert_eq!(frame1.tf, frame2.tf);
    }
}

/// Second determinism gate: prove that the sequential f64 sum within a
/// bucket yields the documented `[1, 2, 3] → 6.0` result.
///
/// The test invariant is that the Reader returns bars in ascending `ts_open_utc`
/// order (Reader contract) and that the aggregator iterates them in that order
/// without re-ordering. We do NOT test what happens if a Reader returns bars
/// out-of-order — that's undefined behaviour per the Reader trait contract;
/// future hardening could detect it.
#[test]
fn tick_volume_sum_ordering_matters() {
    // Build three 1-minute bars inside a single 15m bucket (e.g., 10:00 / 10:01
    // / 10:02 — all map to the 10:00 15m bucket) with tick_volumes [1.0, 2.0, 3.0].
    let date = NaiveDate::from_ymd_opt(2024, 6, 12).expect("valid date");
    let bucket_open = Utc.with_ymd_and_hms(2024, 6, 12, 10, 0, 0).unwrap();
    let mk_bar = |minute_offset: i64, tick_volume: f64| -> RawBar {
        let ts_open = bucket_open + Duration::minutes(minute_offset);
        RawBar {
            ts_open_utc: ts_open,
            ts_close_utc: ts_open + Duration::minutes(1),
            // OHLC values are irrelevant for the volume-sum test; use 1.0 to
            // satisfy monotonicity invariants regardless of MockReader order.
            open: 1.0,
            high: 1.0,
            low: 1.0,
            close: 1.0,
            tick_volume,
        }
    };
    let bars = vec![mk_bar(0, 1.0), mk_bar(1, 2.0), mk_bar(2, 3.0)];

    let mut mock = MockReader::new();
    mock.insert_day("EURUSD", Side::Bid, date, bars);

    let start = bucket_open;
    let end = start + Duration::minutes(15);
    let frame = aggregate(
        &mock,
        AggParams {
            symbol: "EURUSD",
            side: Side::Bid,
            tf: Timeframe::Tf15m,
            range: tradedesk_data::ClosedRangeUtc { start, end },
        },
    )
    .expect("aggregate ok");

    assert_eq!(frame.len(), 1, "exactly one 15m bucket should be emitted");
    assert_eq!(
        frame.tick_volume[0], 6.0,
        "sequential sum of [1.0, 2.0, 3.0] in ts_open_utc order must equal 6.0"
    );
    // Also verify byte-stability across two runs of the same reader with the
    // same data — the f64 sum should reproduce exactly to 6.0 each time.
    let frame2 = aggregate(
        &mock,
        AggParams {
            symbol: "EURUSD",
            side: Side::Bid,
            tf: Timeframe::Tf15m,
            range: tradedesk_data::ClosedRangeUtc { start, end },
        },
    )
    .expect("aggregate ok");
    assert_eq!(
        frame.tick_volume, frame2.tick_volume,
        "two runs of the volume-sum reduction must be byte-identical"
    );
}
