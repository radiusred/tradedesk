//! Error model for the Dukascopy reader.
//!
//! - thiserror-derived
//! - NOT `Serialize` (the `Io(#[from] std::io::Error)` variant is incompatible with
//!   serde-derive)
//! - the miner converts it into its own wire error at its engine boundary; that
//!   conversion lives with the miner, which owns the wire error type

use std::path::PathBuf;

use crate::dukascopy::path_layout::PathParseError;

/// Reader-side error type. Variants follow the
/// "transparent for IO/CSV, struct-style for context-carrying" idiom from Phase 1.
#[derive(Debug, thiserror::Error)]
pub enum DukascopyError {
    /// IO error reading a source file or walking the cache. `zstd` failures also
    /// surface as `io::Error` (the crate's `Decoder::new` returns `io::Result`),
    /// so this variant covers both.
    #[error(transparent)]
    Io(#[from] std::io::Error),

    /// CSV parse failure (header malformed, row malformed, etc.).
    #[error("csv parse error: {0}")]
    Csv(#[from] csv::Error),

    /// `walkdir` failure during `enumerate_days`.
    #[error("walkdir error: {0}")]
    WalkDir(#[from] walkdir::Error),

    /// Failed to parse a CSV `timestamp` cell with the expected
    /// `%Y-%m-%d %H:%M:%S%:z` format.
    #[error("timestamp parse error on {raw:?}: {source}")]
    TimestampParse {
        raw: String,
        #[source]
        source: chrono::ParseError,
    },

    /// CSV row is missing the expected column. `line` is the 1-indexed CSV line
    /// number; `field` is the static column name (`"timestamp"`, `"open"`, etc.).
    #[error("missing field `{field}` at line {line}")]
    MissingField { line: usize, field: &'static str },

    /// Zero-byte file or zstd decode failure on what should be a valid `.csv.zst`.
    /// Distinct from `Io` because the upstream gap detector needs to distinguish
    /// "file absent" (Ok(None) from `fingerprint_day`) from "file present but
    /// corrupt" (this variant; gap detector emits `CorruptSourceFile`).
    #[error("source file is zero-byte or corrupt at {path}: {detail}")]
    CorruptSourceFile { path: PathBuf, detail: String },

    /// Unparseable directory structure under the cache root.
    #[error("path layout violation: {0}")]
    PathLayout(String),

    /// blake3 hex conversion error. Defensive — blake3's `to_hex` always emits
    /// 64 ASCII chars, so this variant is not expected to fire in practice.
    #[error("blake3 hex decode error: {0}")]
    HexDecode(String),
}

impl From<PathParseError> for DukascopyError {
    fn from(err: PathParseError) -> Self {
        Self::PathLayout(err.to_string())
    }
}
