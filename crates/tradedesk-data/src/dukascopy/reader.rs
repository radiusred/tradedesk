//! `DukascopyReader` — concrete `crate::Reader` implementation for the
//! Dukascopy zstd-CSV cache layout (CACHE-01 / CACHE-02 / CACHE-05).
//!
//! Pipeline (per RESEARCH §"Reading a Dukascopy day file end-to-end"):
//! `File` → `BufReader::with_capacity(1MB)` → `zstd::stream::read::Decoder` →
//! `csv::ReaderBuilder::has_headers(true).from_reader`.
//!
//! ## Invariants enforced here
//!
//! - **No `unsafe`** (workspace `unsafe_code = "forbid"`). Only `BufReader<File>`,
//!   no `memmap2`.
//! - **No `println!` / `eprintln!` / `dbg!`** (clippy gate). Use `tracing::*!`.
//! - **`walkdir::WalkDir::new` always chains `.sort_by_file_name()`** for byte-
//!   identity across runs (PATTERNS §"walkdir MUST sort").
//! - **CSV `volume` column is renamed to `tick_volume: f64` at the boundary**
//!   per D2-13 (A1 invariant).
//! - **blake3 fingerprint is over the FULL `.csv.zst` bytes** (D2-05).

use std::io::{BufReader, Read};
use std::path::PathBuf;

use crate::{Blake3Hex, ClosedRangeUtc, RawBar, Reader, Side};
use chrono::{DateTime, Duration, NaiveDate, Utc};

use crate::dukascopy::error::DukascopyError;
use crate::dukascopy::path_layout;

/// One day's pre-decompressed raw bytes plus the source path — read up-front so
/// the iterator-returning `read_1m_bars` can detect a zero-byte file (return an
/// `Err` item) before kicking the zstd decoder.
struct DayBytes {
    path: PathBuf,
    bytes: Vec<u8>,
}

/// Single CSV row before validation. The CSV column is literally `volume`
/// (D2-13: tradedesk-dukascopy emits it under that name); we rename to
/// `tick_volume` at the boundary when constructing `RawBar`.
#[derive(serde::Deserialize)]
struct RawRow {
    timestamp: String,
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    volume: f64,
}

/// Concrete reader for `<cache_root>/<SYMBOL>/<YYYY>/<MM>/<DD>_<side>.csv.zst`.
///
/// Cheap to construct (just stores the root). Does NOT validate that the path
/// exists — preflight does that.
pub struct DukascopyReader {
    cache_root: PathBuf,
    calendar: crate::Calendar,
}

impl DukascopyReader {
    /// Construct from a cache root.
    #[must_use]
    pub fn new(cache_root: impl Into<PathBuf>) -> Self {
        Self {
            cache_root: cache_root.into(),
            calendar: crate::Calendar::fx_major(),
        }
    }

    /// Borrow the configured cache root. Useful in tests for sanity-checking
    /// the constructed reader against the fixture's `TempDir`.
    #[must_use]
    pub fn cache_root(&self) -> &std::path::Path {
        &self.cache_root
    }

    /// Read one day file fully into a `Vec<u8>` (the `.csv.zst` bytes) so we
    /// can both detect zero-byte files and feed the blake3 hasher off the same
    /// in-memory buffer. Returns `Ok(None)` when the file is absent — matches
    /// the `fingerprint_day` contract.
    fn read_day_bytes(
        &self,
        symbol: &str,
        side: Side,
        date: NaiveDate,
    ) -> Result<Option<DayBytes>, DukascopyError> {
        let path = path_layout::day_csv_zst(&self.cache_root, symbol, date, side);
        match std::fs::read(&path) {
            Ok(bytes) => Ok(Some(DayBytes { path, bytes })),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(DukascopyError::Io(e)),
        }
    }
}

impl Reader for DukascopyReader {
    type Error = DukascopyError;

    fn source_id(&self) -> &'static str {
        "dukascopy"
    }

    fn trading_calendar(&self) -> crate::Calendar {
        self.calendar.clone()
    }

    fn read_1m_bars<'a>(
        &'a self,
        symbol: &str,
        side: Side,
        range: ClosedRangeUtc,
    ) -> Result<crate::reader::RawBarIter<'a, Self::Error>, Self::Error> {
        let days = self.enumerate_days(symbol, side, range)?;
        // Arc-share the owned symbol into the per-day closure so each call gets
        // a cheap clone (just a refcount bump) without the borrow checker
        // refusing to let a `String` reference escape the `FnMut` closure body.
        let symbol_arc: std::sync::Arc<str> = std::sync::Arc::from(symbol);
        let iter = days.into_iter().flat_map(move |date| {
            let symbol_arc = std::sync::Arc::clone(&symbol_arc);
            day_bar_iter(self, symbol_arc, side, date, range)
        });
        Ok(Box::new(iter))
    }

    fn fingerprint_day(
        &self,
        symbol: &str,
        side: Side,
        date: NaiveDate,
    ) -> Result<Option<Blake3Hex>, Self::Error> {
        let Some(DayBytes { bytes, .. }) = self.read_day_bytes(symbol, side, date)? else {
            return Ok(None);
        };
        let hash = blake3::hash(&bytes);
        let hex = hash.to_hex();
        let bytes64: [u8; 64] = hex.as_bytes().try_into().map_err(|_| {
            DukascopyError::HexDecode(format!(
                "blake3 hex returned unexpected length: {}",
                hex.len()
            ))
        })?;
        Ok(Some(Blake3Hex::from_hex_bytes(&bytes64)))
    }

    fn enumerate_days(
        &self,
        symbol: &str,
        side: Side,
        range: ClosedRangeUtc,
    ) -> Result<Vec<NaiveDate>, Self::Error> {
        let symbol_root = self.cache_root.join(symbol);
        // If the symbol directory doesn't exist yet, that's not an error — it's
        // just an empty set of days. (Useful for synthetic-cache fixtures where
        // a SyntheticCache::new() is queried before any day is written.)
        if !symbol_root.exists() {
            return Ok(Vec::new());
        }

        // Trim the range to date bounds. `range.start.date_naive()` is the first
        // candidate day; `range.end.date_naive()` is exclusive (a bar at exactly
        // `range.end` is NOT yielded, so its day might not need scanning unless
        // the range straddles its midnight — kept conservative here).
        let start_date = range.start.date_naive();
        let end_date_exclusive = range.end.date_naive();

        let mut dates: Vec<NaiveDate> = Vec::new();
        // walkdir at depth=4: <SYMBOL>/<YYYY>/<MM>/<DD>_<side>.csv.zst. `min/max_
        // depth(4)` restricts to leaf day-files. `.sort_by_file_name()` is
        // MANDATORY — PATTERNS §"walkdir MUST sort" — for byte-identity across
        // runs.
        let walker = walkdir::WalkDir::new(&symbol_root)
            .sort_by_file_name()
            .min_depth(3)
            .max_depth(3);
        for entry in walker {
            let entry = entry?;
            if !entry.file_type().is_file() {
                continue;
            }
            let p = entry.path();
            // Parse expects <root>/<SYMBOL>/<YYYY>/<MM>/<DD>_<side>.csv.zst.
            // We walked from <root>/<SYMBOL> so paths already have that prefix —
            // parse_day_path walks from the END so it doesn't care about the
            // depth above the trailing 4 components.
            let Ok(parsed) = path_layout::parse_day_path(p) else {
                tracing::debug!(?p, "skipping unrecognised file under symbol root");
                continue;
            };
            if parsed.side != side {
                continue;
            }
            // Half-open [start_date, end_date_exclusive). When end_date_exclusive
            // exactly equals a date boundary, the bar at `range.end` itself is
            // not yielded; bars strictly inside the day BEFORE `end_date_
            // exclusive` are still candidates. We include `end_date_exclusive`
            // here as a candidate day so partial-day reads at the range tail
            // work — the per-bar timestamp filter in `read_1m_bars` is the final
            // gate.
            if parsed.date < start_date || parsed.date > end_date_exclusive {
                continue;
            }
            dates.push(parsed.date);
        }
        dates.sort_unstable();
        dates.dedup();
        Ok(dates)
    }
}

/// Yield the (filtered) `RawBar` stream for one day. Wrapped in a function so
/// `read_1m_bars`'s `flat_map` closure stays small and the per-day error path
/// surfaces cleanly as a single-element `Err` iterator.
///
/// `symbol` is taken by value (`Arc<str>`) because the per-day closure inside
/// `read_1m_bars`'s `flat_map` owns a fresh clone (Arc refcount bump) for each
/// invocation — passing by reference would tie the iterator's lifetime to the
/// closure's call-borrow, which the borrow checker rejects (the iterator
/// outlives the closure body).
#[allow(clippy::needless_pass_by_value)]
fn day_bar_iter<'a>(
    reader: &'a DukascopyReader,
    symbol: std::sync::Arc<str>,
    side: Side,
    date: NaiveDate,
    range: ClosedRangeUtc,
) -> Box<dyn Iterator<Item = Result<RawBar, DukascopyError>> + Send + 'a> {
    let day_bytes = match reader.read_day_bytes(&symbol, side, date) {
        Ok(Some(db)) => db,
        Ok(None) => {
            // No file → no bars. Gap detector handles missing-file in its own pass.
            tracing::debug!(symbol = %symbol, ?side, ?date, "day file absent; emitting no bars");
            return Box::new(std::iter::empty());
        }
        Err(e) => return Box::new(std::iter::once(Err(e))),
    };

    // Zero-byte detection (D2-02 mitigation). A real file written as a zero-byte
    // placeholder during an aborted download must NOT be treated as "no bars" —
    // surface a CorruptSourceFile so the gap detector can flag it distinctly.
    if day_bytes.bytes.is_empty() {
        tracing::warn!(path = ?day_bytes.path, "zero-byte source file");
        return Box::new(std::iter::once(Err(DukascopyError::CorruptSourceFile {
            path: day_bytes.path,
            detail: "zero bytes".to_string(),
        })));
    }

    tracing::debug!(path = ?day_bytes.path, "decoding Dukascopy day file");

    // Build the decoder pipeline. `Cursor<Vec<u8>>` is `Send`; everything below
    // owns its bytes for the lifetime of the iterator.
    let cursor = std::io::Cursor::new(day_bytes.bytes);
    let buffered: BufReader<std::io::Cursor<Vec<u8>>> =
        BufReader::with_capacity(1024 * 1024, cursor);
    let decoder = match zstd::stream::read::Decoder::new(buffered) {
        Ok(d) => d,
        Err(e) => {
            return Box::new(std::iter::once(Err(DukascopyError::CorruptSourceFile {
                path: day_bytes.path.clone(),
                detail: format!("zstd init failed: {e}"),
            })));
        }
    };
    let csv_reader = csv::ReaderBuilder::new()
        .has_headers(true)
        .from_reader(BoxedRead(Box::new(decoder)));

    let path_for_errors = day_bytes.path;
    let iter = csv_reader
        .into_deserialize::<RawRow>()
        .enumerate()
        .filter_map(move |(i, row_result)| {
            // The CSV header is consumed by `has_headers(true)`; data rows are
            // 0-indexed here. Surface 1-indexed line numbers in errors (i + 2
            // accounts for header + 1-indexing).
            let line_no = i + 2;
            let row = match row_result {
                Ok(r) => r,
                Err(e) => return Some(Err(DukascopyError::Csv(e))),
            };
            let parsed_ts = match DateTime::parse_from_str(&row.timestamp, "%Y-%m-%d %H:%M:%S%:z") {
                Ok(dt) => dt.with_timezone(&Utc),
                Err(e) => {
                    return Some(Err(DukascopyError::TimestampParse {
                        raw: row.timestamp,
                        source: e,
                    }));
                }
            };

            // Half-open [start, end) bar filter.
            if parsed_ts < range.start || parsed_ts >= range.end {
                let _ = line_no;
                let _ = &path_for_errors;
                return None;
            }

            let bar = RawBar {
                ts_open_utc: parsed_ts,
                ts_close_utc: parsed_ts + Duration::seconds(60),
                open: row.open,
                high: row.high,
                low: row.low,
                close: row.close,
                // Boundary rename — CSV `volume` becomes `tick_volume: f64`.
                // A1 invariant: never propagated as `volume`.
                tick_volume: row.volume,
            };
            Some(Ok(bar))
        });
    Box::new(iter)
}

/// Wrapper around a `Box<dyn Read + Send>` so `csv::ReaderBuilder::from_reader`
/// accepts a runtime-typed reader without monomorphisation gymnastics. `csv`
/// requires `Read`; the `Box<dyn>` is `Send` so the outer iterator is `Send`.
struct BoxedRead(Box<dyn Read + Send>);

impl Read for BoxedRead {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        self.0.read(buf)
    }
}
