//! Synthetic Dukascopy-format cache fixture builder.
//!
//! Writes a fresh `.csv.zst` hierarchy under a `tempfile::TempDir` for integration
//! tests — no checked-in binary data, the fixtures version with the code. Mirrors
//! the `tradedesk-dukascopy` CSV format (header `timestamp,open,high,low,close,
//! volume`; timestamps in `%Y-%m-%d %H:%M:%S%:z` form; zstd level 3 compression).
//!
//! This module is `pub`-but-test-only — see `tests/reader_smoke.rs` for usage.

use std::io::Write;
use std::path::PathBuf;

use chrono::{DateTime, NaiveDate, TimeZone, Utc};
use tradedesk_data::Side;
use tradedesk_data::dukascopy::day_csv_zst;

/// One synthetic cache rooted at a `TempDir`. Dropping the cache deletes the
/// underlying directory tree — keep it alive for the duration of any test that
/// reads from it.
pub struct SyntheticCache {
    pub root: tempfile::TempDir,
}

impl SyntheticCache {
    pub fn new() -> Self {
        Self {
            root: tempfile::TempDir::new().expect("tempdir"),
        }
    }

    /// Compose the path for a day file under this cache. Wraps `day_csv_zst`
    /// against the cache's `TempDir` root.
    pub fn day_path(&self, symbol: &str, date: NaiveDate, side: Side) -> PathBuf {
        day_csv_zst(self.root.path(), symbol, date, side)
    }

    /// Write a synthetic day file containing `csv_body` (header + data rows),
    /// zstd-compressed at level 3 (matches tradedesk-dukascopy's emit level).
    /// Returns the absolute path on disk.
    pub fn write_day(&self, symbol: &str, date: NaiveDate, side: Side, csv_body: &str) -> PathBuf {
        let path = self.day_path(symbol, date, side);
        std::fs::create_dir_all(path.parent().expect("absolute path")).expect("mkdir -p");
        let file = std::fs::File::create(&path).expect("create day file");
        let mut encoder = zstd::stream::write::Encoder::new(file, 3).expect("zstd encoder");
        encoder
            .write_all(csv_body.as_bytes())
            .expect("write csv body into zstd encoder");
        encoder.finish().expect("zstd finish");
        path
    }

    /// Write a zero-byte day file at the canonical path. Used to exercise the
    /// `CorruptSourceFile` branch of the reader.
    pub fn write_zero_byte_day(&self, symbol: &str, date: NaiveDate, side: Side) -> PathBuf {
        let path = self.day_path(symbol, date, side);
        std::fs::create_dir_all(path.parent().expect("absolute path")).expect("mkdir -p");
        std::fs::write(&path, b"").expect("write zero-byte file");
        path
    }
}

/// Build a CSV body of `count` 1-minute bars starting at `start` (UTC).
///
/// Each bar is `open = 1.0 + i*0.0001`, `high = open + 0.00005`, `low = open -
/// 0.00005`, `close = open`, `volume = (i+1) as f64`. The timestamp column uses
/// the Dukascopy format `%Y-%m-%d %H:%M:%S%:z` (space, `+00:00` offset).
///
/// `usize -> i64 / f64` casts are bounded by the synthetic-test domain (we never
/// build more than ~1440 bars in a single fixture call); precision-loss /
/// wraparound is not a concern here.
#[allow(clippy::cast_possible_wrap, clippy::cast_precision_loss)]
pub fn one_minute_csv_body(start: DateTime<Utc>, count: usize) -> String {
    let mut out = String::with_capacity(count.saturating_mul(64) + 32);
    out.push_str("timestamp,open,high,low,close,volume\n");
    for i in 0..count {
        let ts = start + chrono::Duration::minutes(i as i64);
        let open = 1.0 + (i as f64) * 0.0001;
        let high = open + 0.00005;
        let low = open - 0.00005;
        let close = open;
        let volume = (i + 1) as f64;
        // chrono's strftime `%:z` emits `+00:00` for UTC. We format the
        // timestamp explicitly to match the Dukascopy form (space-separated,
        // not RFC3339 `T`).
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

/// Convenience: the half-open UTC range covering a single calendar day in UTC.
pub fn whole_day_range(date: NaiveDate) -> tradedesk_data::ClosedRangeUtc {
    let start = Utc.from_utc_datetime(&date.and_hms_opt(0, 0, 0).expect("00:00:00 valid"));
    let end = start + chrono::Duration::days(1);
    tradedesk_data::ClosedRangeUtc { start, end }
}
