//! Plan 02-01 Task 4 — CACHE-01 / CACHE-05 reader smoke tests.
//!
//! Exercises the `tradedesk_data::Reader` contract end-to-end against a synthetic
//! `.csv.zst` fixture cache. Uses ONLY the public re-export surface of
//! `tradedesk_data::dukascopy::*` + `tradedesk_data::*` — no module-private access —
//! mirroring the Phase 1 precedent at `crates/miner-core/tests/schema_roundtrip.rs`.

mod fixtures;

use chrono::{NaiveDate, TimeZone, Utc};
use tradedesk_data::dukascopy::{DukascopyError, DukascopyReader};
use tradedesk_data::{Reader, Side};

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
