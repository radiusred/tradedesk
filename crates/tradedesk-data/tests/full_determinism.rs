//! Plan 02-06 / Task 1 — end-to-end pipeline determinism gate (CACHE-04 / CACHE-06 / T-02-19).
//!
//! This is the **headline** test of Phase 2. It proves the entire pipeline
//!
//! ```text
//! DukascopyReader (zstd-CSV)  →  aggregate (1m → Nm bars)  →  BarCache (Arrow IPC + sidecar)
//! ```
//!
//! produces **byte-identical** Arrow IPC bytes AND **byte-identical** sidecar JSON
//! bytes across two runs from the same synthetic source data. Unit tests prove the
//! parts; this test proves the seam.
//!
//! If this test fails, the likely culprits are (per 02-RESEARCH §"Determinism contract"
//! lines 526-534):
//!
//! 1. A `HashMap` in a `Serialize` path or in Arrow `Schema` metadata where iteration
//!    order leaks (use `BTreeMap` instead — though the Arrow IPC encoder internally
//!    sorts metadata keys before flatbuffer serialisation, so the in-memory `HashMap`
//!    is fine as long as it is *sourced* from a `BTreeMap`).
//! 2. `rayon::par_iter` inside an aggregator reduction (f64 sums become order-dependent).
//! 3. A clock read (`Instant::now()` / `SystemTime::now()` / `Utc::now()`) inside the
//!    aggregator, cache, or sidecar code path.
//! 4. `walkdir::WalkDir::new(...)` invocation missing `.sort_by_file_name()`.
//! 5. An `arrow::datatypes::Schema` constructed from a `HashMap<String, String>` whose
//!    *source* was a non-BTreeMap collection (the encoder's internal sort needs a
//!    deterministic *source* of keys).
//!
//! Uses the REAL [`DukascopyReader`] (NOT a `MockReader`) against a synthetic on-disk
//! `.csv.zst` cache so the test exercises the zstd decoder, CSV parser, blake3
//! fingerprinter, and path-layout logic in addition to the aggregator + cache.

#![allow(clippy::cast_possible_wrap)] // synthetic-test domain, bounded inputs.
#![allow(clippy::cast_precision_loss)] // synthetic-test domain, bounded inputs.

use std::io::Write;
use std::path::{Path, PathBuf};

use chrono::{DateTime, Duration, NaiveDate, TimeZone, Utc};
use tempfile::TempDir;

use tradedesk_data::dukascopy::{DukascopyReader, day_csv_zst};
use tradedesk_data::{AggParams, BarCache, ClosedRangeUtc, Side, Timeframe};

// ===========================================================================
// Synthetic Dukascopy cache builder (inlined from the patterns established by
// `miner-reader-dukascopy::tests::fixtures::SyntheticCache`; inlined here so
// the test does not reach into the sibling crate's #[path]-included test
// fixtures — we use the sibling crate's PUBLIC `day_csv_zst` API instead).
// ===========================================================================

/// Build a CSV body of `count` 1-minute bars starting at `start` (UTC).
/// Header: `timestamp,open,high,low,close,volume`. Synthetic OHLC values
/// (`open = 1.0 + i*0.0001`, narrow band ±0.00005, volume = i+1) are
/// deterministic in `i`, so byte-identity holds across runs.
fn one_minute_csv_body(start: DateTime<Utc>, count: usize) -> String {
    let mut out = String::with_capacity(count.saturating_mul(64) + 32);
    out.push_str("timestamp,open,high,low,close,volume\n");
    for i in 0..count {
        let ts = start + Duration::minutes(i as i64);
        let open = 1.0 + (i as f64) * 0.0001;
        let high = open + 0.00005;
        let low = open - 0.00005;
        let close = open;
        let volume = (i + 1) as f64;
        out.push_str(&format!(
            "{},{},{},{},{},{}\n",
            ts.format("%Y-%m-%d %H:%M:%S%:z"),
            open,
            high,
            low,
            close,
            volume,
        ));
    }
    out
}

/// Write one synthetic Dukascopy day file: zstd-encoded CSV bytes at the
/// canonical `<root>/<SYMBOL>/<YYYY>/<MM 00-indexed>/<DD>_<side>.csv.zst` path.
fn write_synthetic_day(root: &Path, symbol: &str, date: NaiveDate, side: Side, csv: &str) {
    let path = day_csv_zst(root, symbol, date, side);
    std::fs::create_dir_all(path.parent().expect("absolute path")).expect("mkdir -p");
    let file = std::fs::File::create(&path).expect("create day file");
    let mut encoder = zstd::stream::write::Encoder::new(file, 3).expect("zstd encoder");
    encoder
        .write_all(csv.as_bytes())
        .expect("write csv body into zstd encoder");
    encoder.finish().expect("zstd finish");
}

/// Compose a 3-day synthetic Dukascopy cache covering 2024-06-12/13/14 (Wed/Thu/Fri,
/// inside FX-major open hours) for symbol "EURUSD" on both bid and ask sides.
fn build_synthetic_dukascopy_cache(root: &Path) {
    let symbol = "EURUSD";
    let dates = [
        NaiveDate::from_ymd_opt(2024, 6, 12).expect("valid"),
        NaiveDate::from_ymd_opt(2024, 6, 13).expect("valid"),
        NaiveDate::from_ymd_opt(2024, 6, 14).expect("valid"),
    ];
    for date in dates {
        let start = date.and_hms_opt(0, 0, 0).expect("00:00:00 valid").and_utc();
        for side in [Side::Bid, Side::Ask] {
            let csv = one_minute_csv_body(start, 1440);
            write_synthetic_day(root, symbol, date, side, &csv);
        }
    }
}

// ===========================================================================
// Pipeline runner
// ===========================================================================

/// Drive the full pipeline once: build a `DukascopyReader` over `source_root`,
/// drive `BarCache::get_or_build` into `cache_root`, then read back the Arrow IPC
/// bytes and the sidecar JSON bytes for the (EURUSD, Bid, Tf15m, 2024-06-12..14)
/// quartet.
fn run_full_pipeline(source_root: &Path, cache_root: &Path) -> (Vec<u8>, Vec<u8>) {
    run_full_pipeline_for(source_root, cache_root, Timeframe::Tf15m, Side::Bid)
}

/// Generalised version of [`run_full_pipeline`] parameterised by timeframe + side.
fn run_full_pipeline_for(
    source_root: &Path,
    cache_root: &Path,
    tf: Timeframe,
    side: Side,
) -> (Vec<u8>, Vec<u8>) {
    let reader = DukascopyReader::new(source_root);
    let cache = BarCache::new(cache_root);

    // Range covers Wed 2024-06-12 00:00 UTC through Sat 2024-06-15 00:00 UTC
    // (exclusive end is the day-aligned boundary).
    let range = ClosedRangeUtc {
        start: Utc
            .with_ymd_and_hms(2024, 6, 12, 0, 0, 0)
            .single()
            .expect("valid"),
        end: Utc
            .with_ymd_and_hms(2024, 6, 15, 0, 0, 0)
            .single()
            .expect("valid"),
    };

    let _frame = cache
        .get_or_build(
            &reader,
            AggParams {
                symbol: "EURUSD",
                side,
                tf,
                range,
            },
        )
        .expect("cache get_or_build");

    // Path layout per D2-20 + tradedesk_data::cache::arrow_path:
    //   <cache_root>/<source_id>/<symbol>/<tf>_<side>.arrow
    // DukascopyReader's source_id is "dukascopy"; tf.as_str() is "15m"/"1h"/"1d";
    // side.as_str() is "bid"/"ask".
    let filename = format!("{}_{}.arrow", tf.as_str(), side.as_str());
    let arrow_path: PathBuf = cache_root.join("dukascopy").join("EURUSD").join(&filename);
    let sidecar_path: PathBuf = arrow_path.with_extension("fingerprints.json");

    let arrow_bytes = std::fs::read(&arrow_path)
        .unwrap_or_else(|e| panic!("read arrow file {arrow_path:?}: {e}"));
    let sidecar_bytes = std::fs::read(&sidecar_path)
        .unwrap_or_else(|e| panic!("read sidecar file {sidecar_path:?}: {e}"));
    (arrow_bytes, sidecar_bytes)
}

// ===========================================================================
// Tests
// ===========================================================================

/// **Phase 2's headline determinism gate.** Two runs of the full pipeline from
/// the same synthetic source must produce byte-identical Arrow IPC bytes AND
/// byte-identical sidecar JSON bytes. Any non-determinism in
/// reader / aggregator / cache / sidecar flows through to a diff here.
#[test]
fn two_runs_byte_identical() {
    let tmp = TempDir::new().expect("tempdir");
    let source = tmp.path().join("source");
    std::fs::create_dir_all(&source).expect("mkdir source");
    build_synthetic_dukascopy_cache(&source);

    let run1_cache = tmp.path().join("run1");
    let (run1_arrow, run1_sidecar) = run_full_pipeline(&source, &run1_cache);

    let run2_cache = tmp.path().join("run2");
    let (run2_arrow, run2_sidecar) = run_full_pipeline(&source, &run2_cache);

    assert_eq!(
        run1_arrow.len(),
        run2_arrow.len(),
        "Arrow IPC byte lengths must match across runs (run1: {} bytes, run2: {} bytes)",
        run1_arrow.len(),
        run2_arrow.len(),
    );
    assert_eq!(
        run1_arrow, run2_arrow,
        "Arrow IPC bytes must be byte-identical across runs",
    );
    assert_eq!(
        run1_sidecar.len(),
        run2_sidecar.len(),
        "sidecar JSON byte lengths must match across runs (run1: {} bytes, run2: {} bytes)",
        run1_sidecar.len(),
        run2_sidecar.len(),
    );
    assert_eq!(
        run1_sidecar, run2_sidecar,
        "sidecar JSON must be byte-identical across runs",
    );

    // Sanity: the sidecar must be non-empty (catches a future bug where the
    // sidecar is dropped or the path is wrong).
    assert!(
        !run1_sidecar.is_empty(),
        "sidecar JSON must be non-empty after a successful cache write",
    );
    // Sanity: the Arrow IPC bytes must be non-empty AND start with the IPC
    // magic header `ARROW1\0\0` (file format), proving we wrote a real Arrow
    // file (not e.g. an empty placeholder).
    assert!(
        run1_arrow.starts_with(b"ARROW1"),
        "Arrow IPC file must start with the `ARROW1` magic header; got first 16 bytes = {:?}",
        &run1_arrow[..run1_arrow.len().min(16)],
    );
}

/// The byte-identity invariant must hold across every timeframe × side
/// combination. Iterates all 6 combinations (3 timeframes × 2 sides) and
/// byte-compares two runs at each.
#[test]
fn two_runs_byte_identical_three_timeframes() {
    let tmp = TempDir::new().expect("tempdir");
    let source = tmp.path().join("source");
    std::fs::create_dir_all(&source).expect("mkdir source");
    build_synthetic_dukascopy_cache(&source);

    for tf in [Timeframe::Tf15m, Timeframe::Tf1h, Timeframe::Tf1d] {
        for side in [Side::Bid, Side::Ask] {
            let label = format!("{tf}/{}", side.as_str(), tf = tf.as_str());

            let run1_cache = tmp
                .path()
                .join(format!("r1_{}_{}", tf.as_str(), side.as_str()));
            let (run1_arrow, run1_sidecar) = run_full_pipeline_for(&source, &run1_cache, tf, side);

            let run2_cache = tmp
                .path()
                .join(format!("r2_{}_{}", tf.as_str(), side.as_str()));
            let (run2_arrow, run2_sidecar) = run_full_pipeline_for(&source, &run2_cache, tf, side);

            assert_eq!(
                run1_arrow, run2_arrow,
                "[{label}] Arrow IPC bytes must be byte-identical across runs",
            );
            assert_eq!(
                run1_sidecar, run2_sidecar,
                "[{label}] sidecar JSON must be byte-identical across runs",
            );
        }
    }
}
