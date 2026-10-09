//! Per-day blake3 fingerprint sidecar (D2-03 / D2-05).
//!
//! Each cached Arrow IPC file has a sibling `<…>.fingerprints.json` whose body is
//! a [`FingerprintSidecar`] — the source-of-truth for cache invalidation:
//!
//! - `aggregator_version` + `arrow_schema_version` carry the two version pivots; a
//!   mismatch with the in-process consts forces a full rebuild ([`crate::cache`]
//!   §"Invalidation").
//! - `per_day_fingerprint` is a **`BTreeMap<NaiveDate, String>`** (NEVER a
//!   hash-randomised map type) so its serialised key order is byte-deterministic.
//!   The string value is the 64-char lowercase blake3 hex of the source bytes.
//!
//! ## Atomic write
//!
//! [`write_sidecar_atomic`] uses the `tempfile::NamedTempFile::persist` pattern
//! (same as the Arrow IPC writer), so a crash mid-write leaves the previous
//! sidecar (if any) untouched and the temp file is cleaned up by tempfile's
//! `Drop`. We `serde_json::to_vec_pretty(...)` into a buffer first then write
//! `&[u8]` so the on-disk bytes are not subject to `BufWriter` flush timing or
//! platform-specific newline injection (cross-platform determinism).

use std::collections::{BTreeMap, BTreeSet};
use std::io::Write;
use std::path::Path;

use chrono::NaiveDate;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use crate::aggregator::Timeframe;
use crate::cache::CacheError;
use crate::reader::{Blake3Hex, Side};

/// Sidecar JSON shape (D2-03).
///
/// **Map type:** `per_day_fingerprint` is a `BTreeMap`. Using any hash-randomised
/// map would scramble the on-disk key order across runs and break the
/// byte-identity guarantee that Plan 02-06 will exercise end-to-end.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, JsonSchema)]
pub struct FingerprintSidecar {
    /// `crate::aggregator::AGGREGATOR_VERSION` at the time of cache build.
    pub aggregator_version: String,

    /// `crate::cache::ARROW_SCHEMA_VERSION` at the time of cache build.
    pub arrow_schema_version: String,

    /// Source identifier (e.g., `"dukascopy"`) — for human-readable triage.
    pub source_id: String,

    /// Symbol (e.g., `"EURUSD"`).
    pub symbol: String,

    /// Bid or ask side.
    pub side: Side,

    /// Timeframe of the cached [`crate::aggregator::BarFrame`].
    pub timeframe: Timeframe,

    /// Per-day blake3 hex fingerprint. Key is the UTC date of the source file;
    /// value is the 64-char lowercase hex of `blake3(file_bytes)`. **`BTreeMap`**
    /// so JSON output is byte-deterministic.
    pub per_day_fingerprint: BTreeMap<NaiveDate, String>,
}

/// Read a sidecar JSON from `path`.
///
/// Returns `Ok(None)` when the file is absent — used to distinguish "first-ever
/// cache build" from "corrupted sidecar".
///
/// # Errors
///
/// - [`CacheError::Io`] when the file exists but cannot be read.
/// - [`CacheError::Serde`] when the file exists but is not valid sidecar JSON.
pub fn read_sidecar(path: &Path) -> Result<Option<FingerprintSidecar>, CacheError> {
    match std::fs::read(path) {
        Ok(bytes) => {
            let sc: FingerprintSidecar = serde_json::from_slice(&bytes)?;
            Ok(Some(sc))
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(CacheError::Io(e)),
    }
}

/// Atomically write `sidecar` to `path` via the tempfile-persist pattern.
///
/// We serialise via `serde_json::to_vec_pretty` (into a `Vec<u8>`) BEFORE writing
/// to the tempfile. Pretty output preserves the `BTreeMap` key order in a
/// human-readable form (useful for triage); going through `Vec<u8>` first
/// guarantees the on-disk bytes are not subject to `BufWriter` flush timing or
/// platform-specific newline injection.
///
/// # Errors
///
/// - [`CacheError::Serde`] when JSON serialisation fails.
/// - [`CacheError::Io`] when the tempfile cannot be created / written / persisted.
pub fn write_sidecar_atomic(path: &Path, sidecar: &FingerprintSidecar) -> Result<(), CacheError> {
    let parent = path.parent().ok_or_else(|| {
        CacheError::PathLayout(format!(
            "sidecar path has no parent dir: {}",
            path.display()
        ))
    })?;
    std::fs::create_dir_all(parent)?;
    let bytes = serde_json::to_vec_pretty(sidecar)?;
    let mut tmp = tempfile::NamedTempFile::new_in(parent)?;
    tmp.write_all(&bytes)?;
    tmp.as_file().sync_all()?;
    tmp.persist(path).map_err(|e| CacheError::Io(e.error))?;
    Ok(())
}

/// Compute the set of dates needing regeneration.
///
/// - A date in `current` whose blake3 hex differs from `prev`'s entry → stale.
/// - A date in `current` not present in `prev` → stale (new source day).
/// - A date in `prev` not present in `current` → stale (source day disappeared;
///   the cache should drop its rows on the next rebuild).
///
/// Returned `Vec` is ascending-sorted (`BTreeMap` union iteration order).
#[must_use]
pub fn diff_days(
    prev: &BTreeMap<NaiveDate, String>,
    current: &BTreeMap<NaiveDate, Blake3Hex>,
) -> Vec<NaiveDate> {
    let mut out: BTreeSet<NaiveDate> = BTreeSet::new();

    for (date, fp) in current {
        match prev.get(date) {
            Some(prev_hex) if prev_hex == fp.as_str() => { /* unchanged */ }
            _ => {
                out.insert(*date);
            }
        }
    }
    for date in prev.keys() {
        if !current.contains_key(date) {
            out.insert(*date);
        }
    }
    out.into_iter().collect()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn make_sidecar() -> FingerprintSidecar {
        let mut per_day = BTreeMap::new();
        per_day.insert(
            NaiveDate::from_ymd_opt(2024, 6, 12).unwrap(),
            "a".repeat(64),
        );
        per_day.insert(
            NaiveDate::from_ymd_opt(2024, 6, 13).unwrap(),
            "b".repeat(64),
        );
        FingerprintSidecar {
            aggregator_version: "1.0.0".to_string(),
            arrow_schema_version: "1.0.0".to_string(),
            source_id: "mock".to_string(),
            symbol: "EURUSD".to_string(),
            side: Side::Bid,
            timeframe: Timeframe::Tf15m,
            per_day_fingerprint: per_day,
        }
    }

    #[test]
    fn round_trip_via_atomic_write_and_read() {
        let tmp = tempfile::TempDir::new().unwrap();
        let path = tmp.path().join("test.fingerprints.json");
        let sidecar = make_sidecar();
        write_sidecar_atomic(&path, &sidecar).unwrap();
        let read_back = read_sidecar(&path).unwrap().expect("sidecar present");
        assert_eq!(read_back, sidecar);
    }

    #[test]
    fn read_returns_none_for_missing_file() {
        let tmp = tempfile::TempDir::new().unwrap();
        let path = tmp.path().join("does_not_exist.fingerprints.json");
        assert!(read_sidecar(&path).unwrap().is_none());
    }

    #[test]
    fn two_writes_byte_identical() {
        let tmp = tempfile::TempDir::new().unwrap();
        let p1 = tmp.path().join("a.fingerprints.json");
        let p2 = tmp.path().join("b.fingerprints.json");
        let sc = make_sidecar();
        write_sidecar_atomic(&p1, &sc).unwrap();
        write_sidecar_atomic(&p2, &sc).unwrap();
        assert_eq!(std::fs::read(&p1).unwrap(), std::fs::read(&p2).unwrap());
    }

    #[test]
    fn diff_days_detects_changed_missing_and_added() {
        let d1 = NaiveDate::from_ymd_opt(2024, 6, 12).unwrap();
        let d2 = NaiveDate::from_ymd_opt(2024, 6, 13).unwrap();
        let d3 = NaiveDate::from_ymd_opt(2024, 6, 14).unwrap();

        let mut prev: BTreeMap<NaiveDate, String> = BTreeMap::new();
        prev.insert(d1, "a".repeat(64));
        prev.insert(d2, "b".repeat(64));

        let mut current: BTreeMap<NaiveDate, Blake3Hex> = BTreeMap::new();
        // d1 unchanged.
        current.insert(d1, Blake3Hex::from_hex_bytes(&[b'a'; 64]));
        // d2 changed.
        current.insert(d2, Blake3Hex::from_hex_bytes(&[b'c'; 64]));
        // d3 added.
        current.insert(d3, Blake3Hex::from_hex_bytes(&[b'd'; 64]));

        let stale = diff_days(&prev, &current);
        assert_eq!(stale, vec![d2, d3]);
    }

    #[test]
    fn diff_days_detects_removed() {
        let d1 = NaiveDate::from_ymd_opt(2024, 6, 12).unwrap();
        let d2 = NaiveDate::from_ymd_opt(2024, 6, 13).unwrap();

        let mut prev: BTreeMap<NaiveDate, String> = BTreeMap::new();
        prev.insert(d1, "a".repeat(64));
        prev.insert(d2, "b".repeat(64));

        let mut current: BTreeMap<NaiveDate, Blake3Hex> = BTreeMap::new();
        current.insert(d1, Blake3Hex::from_hex_bytes(&[b'a'; 64]));
        // d2 removed entirely.

        let stale = diff_days(&prev, &current);
        assert_eq!(stale, vec![d2]);
    }
}
