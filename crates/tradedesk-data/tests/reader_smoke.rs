//! Plan 02-01 Task 4 — CACHE-01 / CACHE-05 reader smoke tests, plus the
//! timestamp-only read path that gap detection uses (M33-R3).
//!
//! Exercises the `tradedesk_data::Reader` contract end-to-end against a synthetic
//! `.csv.zst` fixture cache. Uses ONLY the public re-export surface of
//! `tradedesk_data::dukascopy::*` + `tradedesk_data::*` — no module-private access —
//! mirroring the Phase 1 precedent at `crates/miner-core/tests/schema_roundtrip.rs`.

mod fixtures;

use chrono::{NaiveDate, TimeZone, Utc};
use tradedesk_data::dukascopy::{DukascopyError, DukascopyReader};
use tradedesk_data::{GapDetector, GapReason, Reader, Side};

use crate::fixtures::{SyntheticCache, one_minute_csv_body, whole_day_range};

#[test]
fn reads_one_day_in_order() {
    let cache = SyntheticCache::new();
    let date = NaiveDate::from_ymd_opt(2024, 6, 15).expect("date valid");
    let start = Utc.from_utc_datetime(&date.and_hms_opt(0, 0, 0).unwrap());
    let body = one_minute_csv_body(start, 1440);
    cache.write_day("EURUSD", date, Side::Bid, &body);

    let reader = DukascopyReader::new(cache.root.path());
    let range = whole_day_range(date);
    let iter = reader
        .read_1m_bars("EURUSD", Side::Bid, range)
        .expect("read_1m_bars opens");
    let bars: Vec<_> = iter.collect::<Result<Vec<_>, _>>().expect("all bars parse");

    assert_eq!(bars.len(), 1440, "1440 1m bars in one day");
    assert!(
        bars.windows(2).all(|w| w[0].ts_open_utc < w[1].ts_open_utc),
        "bars must be strictly ascending"
    );
    // The first bar's open is `1.0 + 0 * 0.0001 = 1.0` per the synthetic
    // generator (fixtures::one_minute_csv_body).
    assert!(
        (bars[0].open - 1.0).abs() < 1e-12,
        "first bar open: expected 1.0 (got {})",
        bars[0].open
    );
    // ts_close_utc = ts_open_utc + 60s for every bar.
    for b in &bars {
        assert_eq!(
            b.ts_close_utc - b.ts_open_utc,
            chrono::Duration::seconds(60)
        );
    }
}

#[test]
fn zero_byte_file() {
    let cache = SyntheticCache::new();
    let date = NaiveDate::from_ymd_opt(2024, 6, 15).unwrap();
    cache.write_zero_byte_day("EURUSD", date, Side::Bid);

    let reader = DukascopyReader::new(cache.root.path());
    let range = whole_day_range(date);
    let iter = reader
        .read_1m_bars("EURUSD", Side::Bid, range)
        .expect("read_1m_bars opens (zero-byte detection is per-bar)");
    let items: Vec<_> = iter.collect();
    assert!(
        matches!(
            items.first(),
            Some(Err(DukascopyError::CorruptSourceFile { .. })),
        ),
        "zero-byte file must yield CorruptSourceFile as first iterator item; got {items:?}",
    );
}

#[test]
fn tick_volume_from_csv_volume() {
    // CSV volume column = 1234.0 on the first bar. Confirms the boundary rename
    // CSV `volume` -> RawBar.tick_volume: f64 lands exactly (D2-13 / A1).
    let cache = SyntheticCache::new();
    let date = NaiveDate::from_ymd_opt(2024, 6, 15).unwrap();
    let body = "timestamp,open,high,low,close,volume\n\
                2024-06-15 00:00:00+00:00,1.10342,1.10361,1.10311,1.10355,1234.0\n";
    cache.write_day("EURUSD", date, Side::Bid, body);

    let reader = DukascopyReader::new(cache.root.path());
    let range = whole_day_range(date);
    let bars: Vec<_> = reader
        .read_1m_bars("EURUSD", Side::Bid, range)
        .expect("read_1m_bars opens")
        .collect::<Result<Vec<_>, _>>()
        .expect("bar parses");

    assert_eq!(bars.len(), 1, "one row in CSV -> one bar");
    assert!(
        (bars[0].tick_volume - 1234.0).abs() < 1e-12,
        "tick_volume must equal CSV volume column (got {})",
        bars[0].tick_volume,
    );
    // Belt-and-braces type check: `tick_volume` is `f64`, never `u32` — this
    // line fails to compile if anyone re-renames the field to an integer type.
    let _: f64 = bars[0].tick_volume;
}

#[test]
fn fingerprint_round_trip() {
    // Same bytes => same fingerprint. Different bytes => different fingerprint.
    // Hash is over the COMPRESSED .csv.zst bytes (D2-05).
    let cache = SyntheticCache::new();
    let date = NaiveDate::from_ymd_opt(2024, 6, 15).unwrap();
    let body = "timestamp,open,high,low,close,volume\n\
                2024-06-15 00:00:00+00:00,1.0,1.0,1.0,1.0,1.0\n";
    cache.write_day("EURUSD", date, Side::Bid, body);

    let reader = DukascopyReader::new(cache.root.path());
    let fp1 = reader
        .fingerprint_day("EURUSD", Side::Bid, date)
        .expect("fingerprint ok")
        .expect("file present");
    let fp2 = reader
        .fingerprint_day("EURUSD", Side::Bid, date)
        .expect("fingerprint ok")
        .expect("file present");
    assert_eq!(fp1, fp2, "deterministic fingerprint");

    // Missing day -> Ok(None).
    let absent_date = NaiveDate::from_ymd_opt(1999, 1, 1).unwrap();
    let absent = reader
        .fingerprint_day("EURUSD", Side::Bid, absent_date)
        .expect("fingerprint ok");
    assert!(absent.is_none(), "absent day must yield None");
}

#[test]
fn enumerate_days_sorted_ascending() {
    let cache = SyntheticCache::new();
    let d1 = NaiveDate::from_ymd_opt(2024, 6, 15).unwrap();
    let d2 = NaiveDate::from_ymd_opt(2024, 6, 16).unwrap();
    let d3 = NaiveDate::from_ymd_opt(2024, 6, 14).unwrap();
    let body = "timestamp,open,high,low,close,volume\n";
    // Insertion order intentionally non-sorted to confirm walkdir's
    // `sort_by_file_name` chain produces deterministic ascending output.
    cache.write_day("EURUSD", d2, Side::Bid, body);
    cache.write_day("EURUSD", d1, Side::Bid, body);
    cache.write_day("EURUSD", d3, Side::Bid, body);
    // Ask-side file in the same range must NOT pollute bid enumeration.
    cache.write_day("EURUSD", d1, Side::Ask, body);

    let reader = DukascopyReader::new(cache.root.path());
    let range = tradedesk_data::ClosedRangeUtc {
        start: Utc.from_utc_datetime(&d3.and_hms_opt(0, 0, 0).unwrap()),
        end: Utc.from_utc_datetime(&d2.and_hms_opt(0, 0, 0).unwrap()) + chrono::Duration::days(1),
    };
    let days = reader
        .enumerate_days("EURUSD", Side::Bid, range)
        .expect("enumerate ok");
    assert_eq!(days, vec![d3, d1, d2]);
}

// ---- Timestamp-only read path (M33-R3) ------------------------------------

/// The same CSV rows with every OHLC field set to `NOT_A_PRICE` and every
/// volume to `NOT_A_VOLUME`. The timestamp column is unchanged.
fn unparseable_fields(body: &str) -> String {
    let mut lines = body.lines();
    let mut out = format!("{}\n", lines.next().expect("header"));
    for line in lines {
        let ts = line.split(',').next().expect("timestamp column");
        out.push_str(&format!(
            "{ts},NOT_A_PRICE,NOT_A_PRICE,NOT_A_PRICE,NOT_A_PRICE,NOT_A_VOLUME\n"
        ));
    }
    out
}

/// A synthetic open-hours day (Monday 2024-01-08) with the minutes 10:00 to
/// 10:02 removed, so the manifest has intra-day gaps to compare.
fn monday_body_with_hole() -> (NaiveDate, String) {
    let date = NaiveDate::from_ymd_opt(2024, 1, 8).unwrap();
    let start = Utc.from_utc_datetime(&date.and_hms_opt(0, 0, 0).unwrap());
    let hole = ["2024-01-08 10:00", "2024-01-08 10:01", "2024-01-08 10:02"];
    let mut body = String::new();
    for line in one_minute_csv_body(start, 1440).lines() {
        if !hole.iter().any(|h| line.starts_with(h)) {
            body.push_str(line);
            body.push('\n');
        }
    }
    (date, body)
}

#[test]
fn timestamps_skip_unparseable_price_and_volume() {
    let cache = SyntheticCache::new();
    let date = NaiveDate::from_ymd_opt(2024, 6, 15).unwrap();
    let start = Utc.from_utc_datetime(&date.and_hms_opt(0, 0, 0).unwrap());
    let body = unparseable_fields(&one_minute_csv_body(start, 1440));
    cache.write_day("EURUSD", date, Side::Bid, &body);

    let reader = DukascopyReader::new(cache.root.path());
    let range = whole_day_range(date);

    // The fixture really is unparseable as bars.
    let first_bar = reader
        .read_1m_bars("EURUSD", Side::Bid, range)
        .expect("read_1m_bars opens")
        .next();
    assert!(
        matches!(first_bar, Some(Err(DukascopyError::Csv(_)))),
        "bars path must reject NOT_A_PRICE; got {first_bar:?}",
    );

    let ts: Vec<_> = reader
        .read_1m_timestamps("EURUSD", Side::Bid, range)
        .expect("read_1m_timestamps opens")
        .collect::<Result<Vec<_>, _>>()
        .expect("timestamps parse without the price fields");
    let expected: Vec<_> = (0..1440)
        .map(|i| start + chrono::Duration::minutes(i))
        .collect();
    assert_eq!(ts, expected, "all 1440 minute timestamps, ascending");
}

#[test]
fn timestamps_zero_byte_file_is_corrupt() {
    let cache = SyntheticCache::new();
    let date = NaiveDate::from_ymd_opt(2024, 6, 15).unwrap();
    cache.write_zero_byte_day("EURUSD", date, Side::Bid);

    let reader = DukascopyReader::new(cache.root.path());
    let first = reader
        .read_1m_timestamps("EURUSD", Side::Bid, whole_day_range(date))
        .expect("read_1m_timestamps opens")
        .next();
    assert!(
        matches!(first, Some(Err(DukascopyError::CorruptSourceFile { .. }))),
        "zero-byte file must yield CorruptSourceFile; got {first:?}",
    );
}

#[test]
fn gap_manifest_ignores_price_and_volume_fields() {
    let (date, numeric) = monday_body_with_hole();
    let numeric_cache = SyntheticCache::new();
    numeric_cache.write_day("AAA", date, Side::Bid, &numeric);
    let text_cache = SyntheticCache::new();
    text_cache.write_day("AAA", date, Side::Bid, &unparseable_fields(&numeric));

    let range = whole_day_range(date);
    let from_numeric = GapDetector::detect(
        &DukascopyReader::new(numeric_cache.root.path()),
        "AAA",
        Side::Bid,
        range,
    )
    .expect("detect numeric");
    let from_text = GapDetector::detect(
        &DukascopyReader::new(text_cache.root.path()),
        "AAA",
        Side::Bid,
        range,
    )
    .expect("detect unparseable");

    let hole_start = Utc.from_utc_datetime(&date.and_hms_opt(10, 0, 0).unwrap());
    let expected: Vec<_> = (0..3)
        .map(|i| hole_start + chrono::Duration::minutes(i))
        .collect();
    let starts: Vec<_> = from_numeric.gaps.iter().map(|g| g.start_utc).collect();
    assert_eq!(starts, expected, "the three removed minutes, nothing else");
    assert!(
        from_numeric
            .gaps
            .iter()
            .all(|g| matches!(g.reason, GapReason::IntraDayGap { .. })),
        "intra-day gaps only: {:?}",
        from_numeric.gaps,
    );
    assert_eq!(
        from_text, from_numeric,
        "changing only OHLC and volume must not change the manifest",
    );
}

#[test]
fn gap_manifest_bad_timestamp_is_corrupt() {
    let (date, numeric) = monday_body_with_hole();
    let body = unparseable_fields(&numeric).replacen("2024-01-08 12:00:00+00:00", "NOT_A_TIME", 1);
    let cache = SyntheticCache::new();
    cache.write_day("AAA", date, Side::Bid, &body);

    let manifest = GapDetector::detect(
        &DukascopyReader::new(cache.root.path()),
        "AAA",
        Side::Bid,
        whole_day_range(date),
    )
    .expect("detect");
    assert_eq!(
        manifest.gaps.len(),
        1,
        "one whole-day span: {:?}",
        manifest.gaps
    );
    assert!(
        matches!(manifest.gaps[0].reason, GapReason::CorruptSourceFile { .. }),
        "a bad timestamp still makes the day corrupt: {:?}",
        manifest.gaps[0],
    );
}
