//! Derived-bar cache (Plan 02-05 / CACHE-06).
//!
//! Owns ONE writable directory tree under `bar_cache_root`. Each cache entry is keyed
//! by the quartet `(source_id, symbol, side, timeframe)` and is materialised as a pair
//! of files at `<cache_root>/<source_id>/<symbol>/<timeframe>_<side>.arrow` plus a
//! sibling `<…>.fingerprints.json` sidecar (`D2-20`).
//!
//! ## Invalidation (two-axis, D2-04)
//!
//! 1. **Full rebuild** on [`AGGREGATOR_VERSION`] OR [`ARROW_SCHEMA_VERSION`] mismatch
//!    between the in-process consts and the sidecar's recorded values.
//! 2. **Day-splice rewrite** when one or more per-day blake3 fingerprints differ
//!    between the source files (computed via [`Reader::fingerprint_day`]) and the
//!    sidecar's `per_day_fingerprint` `BTreeMap`. Days whose fingerprint matches are
//!    served from the existing Arrow file; days whose fingerprint mismatches are
//!    re-aggregated and spliced in.
//!
//! ## Crash safety (D2-03 / T-02-15)
//!
//! Writes use the **two-step atomic API** [`write_arrow_to_tempfile`] +
//! [`persist_arrow_tempfile`] on top of [`tempfile::NamedTempFile::persist`]:
//!
//! - `write_arrow_to_tempfile` creates a temp file in the *same parent* directory as
//!   the target, streams the Arrow IPC bytes into it, calls `sync_all`, and returns
//!   the unpersisted handle. **Dropping the handle without calling
//!   [`persist_arrow_tempfile`] leaves the existing target untouched** — this is the
//!   crash-safety contract.
//! - `persist_arrow_tempfile` atomically renames the temp file over the target.
//!
//! Ordering inside [`BarCache::get_or_build`]: **Arrow file is persisted FIRST**, then
//! the sidecar. If we crash between the two writes the next run sees a stale sidecar
//! (one or more day fingerprints will diverge) and rebuilds; nothing is silently
//! served stale. Reversing the order would create exactly that silent-corruption
//! window.
//!
//! ## Schema byte-determinism (D2-04 / T-02-16)
//!
//! - [`build_arrow_schema`] constructs the metadata in a `BTreeMap` so the *source* of
//!   the keys is byte-deterministic.
//! - The map is then handed to [`arrow::datatypes::Schema::with_metadata`] (an arrow
//!   API that takes `HashMap<String, String>`).
//! - The arrow IPC encoder (`arrow_ipc::convert::metadata_to_fb`) **sorts the
//!   metadata keys before serialising them to flatbuffers** — see
//!   `arrow-ipc-58.3.0/src/convert.rs:136-137` (`ordered_keys.sort()`). So the on-disk
//!   bytes are byte-stable regardless of the intermediate `HashMap`'s iteration order.
//! - The `arrow_schema_snapshot` test pins the human-readable form via `insta`; the
//!   `arrow_bytes_deterministic_under_shuffled_construction` proptest in `cache_smoke`
//!   pins the on-disk bytes by running the full write path twice and byte-comparing.
//!
//! ## Safety
//!
//! Reads use `BufReader<File>` — **NOT** memory-mapped IO. The workspace lints
//! `forbid` unverified-code patterns; the memory-mapping crate ecosystem requires
//! patterns incompatible with that lint and is deferred to a future optimisation
//! phase (see Phase 2 research §"Memory-mapped reads"). Until then, a 1-MiB
//! `BufReader` amortises syscall overhead well enough for the multi-MB Arrow
//! files this cache produces.
//!
//! ## Tracing
//!
//! All progress / observability output goes through `tracing::{info,debug,warn,error}!`.
//! `println!`/`eprintln!`/`dbg!` are banned by the workspace `clippy.toml`.

use std::collections::{BTreeMap, HashMap};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use arrow::array::{ArrayRef, Float64Array, RecordBatch, TimestampNanosecondArray};
use arrow::datatypes::{DataType, Field, Schema, SchemaRef, TimeUnit};
use chrono::{NaiveDate, TimeZone, Utc};
use thiserror::Error;

use crate::aggregator::{AGGREGATOR_VERSION, AggParams, BarFrame, Timeframe, aggregate};
use crate::reader::{Blake3Hex, Reader, Side};

pub mod fingerprints;

pub use fingerprints::FingerprintSidecar;

/// Arrow-IPC schema-shape version. Bump on ANY column shape, name, type, or
/// non-additive metadata change. Stored in the sidecar JSON; a mismatch triggers a
/// full rebuild (D2-04).
pub const ARROW_SCHEMA_VERSION: &str = "1.0.0";

/// Errors surfaced by the derived-bar cache layer.
///
/// **No `Serialize` derive** — `Io(#[from] std::io::Error)` is incompatible with
/// `serde::Serialize`, mirroring the workspace idiom established by
/// the miner's `MinerError`. Conversion to `WireError` for engine-boundary
/// emission happens at the engine boundary (Plan 05's caller's responsibility,
/// not the cache layer).
#[derive(Debug, Error)]
pub enum CacheError {
    /// Filesystem I/O failed during a cache read or write.
    #[error("cache I/O error: {0}")]
    Io(#[from] std::io::Error),

    /// An Arrow operation (schema, IPC, `RecordBatch` construction) failed.
    #[error("arrow error: {0}")]
    Arrow(#[from] arrow::error::ArrowError),

    /// Sidecar JSON serialisation / deserialisation failed.
    #[error("sidecar JSON error: {0}")]
    Serde(#[from] serde_json::Error),

    /// The aggregator (or the reader behind it) failed during a cache rebuild.
    /// `Reader::Error` is generic and would force a generic on every cache call
    /// site; we stringify at the boundary instead (matches the engine-edge
    /// `WireError` pattern).
    #[error("aggregator error: {0}")]
    Aggregate(String),

    /// Arrow file metadata's `arrow_schema_version` differs from the in-process
    /// [`ARROW_SCHEMA_VERSION`]. Surfaced as a diagnostic on read-back so a
    /// manually-edited file fails loudly instead of corrupting downstream output.
    #[error("arrow schema version mismatch in {file}: found {found:?}, expected {expected:?}")]
    SchemaVersionMismatch {
        /// The version string we read from the Arrow file's metadata.
        found: String,
        /// The version string we expected (i.e., [`ARROW_SCHEMA_VERSION`]).
        expected: String,
        /// Path of the offending file (for operator triage).
        file: PathBuf,
    },

    /// Cache root or quartet path layout is unexpected (e.g., empty parent
    /// component, illegal Windows path component, etc.).
    #[error("path layout error: {0}")]
    PathLayout(String),
}

// ---------------------------------------------------------------------------
// Schema builder + path helpers
// ---------------------------------------------------------------------------

/// Build the Arrow [`Schema`] for one cached [`BarFrame`] quartet.
///
/// **Field order (locked, MUST NOT drift):** `ts_open_utc`, `ts_close_utc`, `open`,
/// `high`, `low`, `close`, `tick_volume`. The `arrow_schema_snapshot::schema_field_order_locked`
/// test asserts this order and the type shape; any drift is a contract break and a
/// [`ARROW_SCHEMA_VERSION`] bump.
///
/// **Types:** the two timestamp fields use `Timestamp(Nanosecond, "UTC")` (non-null);
/// the five numeric fields are `Float64` (non-null). `tick_volume` is **`Float64`**
/// — the A1 invariant from Plan 01 rules out the legacy `u32` tick-count shape.
///
/// **Metadata determinism:** the metadata is constructed in a `BTreeMap<String, String>`
/// so its source is byte-deterministic. It is then handed to
/// [`Schema::with_metadata`] (an arrow API that takes `HashMap<String, String>`). The
/// arrow IPC encoder sorts metadata keys before serialising to flatbuffers
/// (`arrow-ipc-58.3.0/src/convert.rs:136-137`), so the on-disk metadata bytes are
/// byte-stable regardless of the intermediate `HashMap`'s iteration order.
///
/// Metadata keys (sorted): `aggregator_version`, `arrow_schema_version`,
/// `code_revision`, `side`, `source_id`, `symbol`, `timeframe`. The
/// `arrow_schema_snapshot::schema_metadata_keys_sorted` test pins the sorted-keys
/// invariant.
#[must_use]
pub fn build_arrow_schema(source_id: &str, symbol: &str, side: Side, tf: Timeframe) -> Schema {
    let ts_type = DataType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()));
    let fields = vec![
        Field::new("ts_open_utc", ts_type.clone(), false),
        Field::new("ts_close_utc", ts_type, false),
        Field::new("open", DataType::Float64, false),
        Field::new("high", DataType::Float64, false),
        Field::new("low", DataType::Float64, false),
        Field::new("close", DataType::Float64, false),
        // `tick_volume`: Float64, NEVER a `UInt32` tick-count column. A1
        // invariant locked by `schema_field_types_locked` test.
        Field::new("tick_volume", DataType::Float64, false),
    ];

    // BTreeMap source → byte-deterministic key set. Conversion via
    // `into_iter().collect::<HashMap<_,_>>()` is a one-step move; the resulting
    // HashMap's iteration order is irrelevant because the arrow IPC encoder
    // sorts metadata keys internally before flatbuffer serialisation
    // (arrow-ipc-58.3.0/src/convert.rs:136-137).
    let mut meta_btree: BTreeMap<String, String> = BTreeMap::new();
    meta_btree.insert(
        "aggregator_version".to_string(),
        AGGREGATOR_VERSION.to_string(),
    );
    meta_btree.insert(
        "arrow_schema_version".to_string(),
        ARROW_SCHEMA_VERSION.to_string(),
    );
    meta_btree.insert(
        "code_revision".to_string(),
        crate::CODE_REVISION.to_string(),
    );
    meta_btree.insert("side".to_string(), side.as_str().to_string());
    meta_btree.insert("source_id".to_string(), source_id.to_string());
    meta_btree.insert("symbol".to_string(), symbol.to_string());
    meta_btree.insert("timeframe".to_string(), tf.as_str().to_string());

    let meta_hash: HashMap<String, String> = meta_btree.into_iter().collect();
    Schema::new(fields).with_metadata(meta_hash)
}

/// Compose the on-disk Arrow IPC path for one cache quartet (D2-20).
///
/// Layout: `<cache_root>/<source_id>/<symbol>/<timeframe>_<side>.arrow`.
#[must_use]
pub fn arrow_path(
    cache_root: &Path,
    source_id: &str,
    symbol: &str,
    side: Side,
    tf: Timeframe,
) -> PathBuf {
    cache_root
        .join(source_id)
        .join(symbol)
        .join(format!("{}_{}.arrow", tf.as_str(), side.as_str()))
}

/// Sidecar JSON path: `<arrow_path>.fingerprints.json` (the `.arrow` extension is
/// replaced wholesale with `fingerprints.json`).
#[must_use]
pub fn sidecar_path(arrow_path: &Path) -> PathBuf {
    arrow_path.with_extension("fingerprints.json")
}

// ---------------------------------------------------------------------------
// Atomic two-step write API
// ---------------------------------------------------------------------------

/// Write Arrow IPC bytes into a tempfile inside `target_parent` **without
/// persisting** it.
///
/// The returned [`tempfile::NamedTempFile`] is the *unpersisted* handle. Callers
/// MUST follow with [`persist_arrow_tempfile`] to atomically promote it to the
/// target path; dropping the handle without persisting leaves the existing target
/// file (if any) untouched, and the temp file is unlinked by the
/// [`tempfile::NamedTempFile`] `Drop` impl. **This is the crash-safety contract
/// that the `atomic_write_crash_safety` integration test exercises.**
///
/// # Errors
///
/// Returns [`CacheError::Io`] if the tempfile cannot be created or written; returns
/// [`CacheError::Arrow`] if the IPC encoder fails.
pub(crate) fn write_arrow_to_tempfile(
    target_parent: &Path,
    schema: &Schema,
    batches: &[RecordBatch],
) -> Result<tempfile::NamedTempFile, CacheError> {
    std::fs::create_dir_all(target_parent)?;
    let tmp = tempfile::NamedTempFile::new_in(target_parent)?;
    {
        // FileWriter writes the IPC schema header (with sorted-key metadata) at
        // construction time, then one IPC block per `write()` call, and the
        // footer at `finish()`. Scope the writer so it's dropped before we
        // sync_all().
        let mut writer = arrow::ipc::writer::FileWriter::try_new(tmp.as_file(), schema)?;
        for batch in batches {
            writer.write(batch)?;
        }
        writer.finish()?;
    }
    tmp.as_file().sync_all()?;
    Ok(tmp)
}

/// Atomically promote a tempfile produced by [`write_arrow_to_tempfile`] over the
/// `target` path. This is the moment the write becomes observable.
///
/// # Errors
///
/// Returns [`CacheError::Io`] if the underlying `persist` fails (e.g., cross-device
/// rename — impossible when the tempfile was created in the target's parent dir).
pub(crate) fn persist_arrow_tempfile(
    tempfile: tempfile::NamedTempFile,
    target: &Path,
) -> Result<(), CacheError> {
    tempfile
        .persist(target)
        .map_err(|e| CacheError::Io(e.error))?;
    Ok(())
}

/// Convenience: write + persist in one step (the happy-path used by
/// [`BarCache::get_or_build`]). The crash-safety test uses the two-step API
/// directly.
///
/// # Errors
///
/// Returns the same errors as [`write_arrow_to_tempfile`] and
/// [`persist_arrow_tempfile`].
pub(crate) fn write_arrow_atomic(
    target: &Path,
    schema: &Schema,
    batches: &[RecordBatch],
) -> Result<(), CacheError> {
    let parent = target.parent().ok_or_else(|| {
        CacheError::PathLayout(format!(
            "cache target has no parent dir: {}",
            target.display()
        ))
    })?;
    let tmp = write_arrow_to_tempfile(parent, schema, batches)?;
    persist_arrow_tempfile(tmp, target)
}

// ---------------------------------------------------------------------------
// BarFrame ↔ RecordBatch + on-disk read helper
// ---------------------------------------------------------------------------

/// Convert a [`BarFrame`] into a single Arrow [`RecordBatch`] matching `schema`.
///
/// # Errors
///
/// Returns [`CacheError::Arrow`] if column array construction or batch
/// construction fails (e.g., column-length mismatch).
fn bar_frame_to_record_batch(frame: &BarFrame, schema: &Schema) -> Result<RecordBatch, CacheError> {
    let ts_open: Vec<i64> = frame
        .ts_open_utc
        .iter()
        .map(|ts| {
            ts.timestamp_nanos_opt()
                .expect("DateTime<Utc> within nanosecond-representable range")
        })
        .collect();
    let ts_close: Vec<i64> = frame
        .ts_close_utc
        .iter()
        .map(|ts| {
            ts.timestamp_nanos_opt()
                .expect("DateTime<Utc> within nanosecond-representable range")
        })
        .collect();

    let ts_open_arr = TimestampNanosecondArray::from(ts_open).with_timezone("UTC");
    let ts_close_arr = TimestampNanosecondArray::from(ts_close).with_timezone("UTC");
    let open_arr = Float64Array::from(frame.open.clone());
    let high_arr = Float64Array::from(frame.high.clone());
    let low_arr = Float64Array::from(frame.low.clone());
    let close_arr = Float64Array::from(frame.close.clone());
    let tv_arr = Float64Array::from(frame.tick_volume.clone());

    let cols: Vec<ArrayRef> = vec![
        Arc::new(ts_open_arr),
        Arc::new(ts_close_arr),
        Arc::new(open_arr),
        Arc::new(high_arr),
        Arc::new(low_arr),
        Arc::new(close_arr),
        Arc::new(tv_arr),
    ];

    let schema_ref: SchemaRef = Arc::new(schema.clone());
    RecordBatch::try_new(schema_ref, cols).map_err(CacheError::from)
}

/// Reconstruct a [`BarFrame`] from a slice of [`RecordBatch`]es plus the on-disk
/// [`Schema`] (whose metadata carries `source_id` / `symbol` / `side` / `timeframe`).
///
/// # Errors
///
/// Returns [`CacheError::Arrow`] when a column is missing or the wrong type;
/// [`CacheError::PathLayout`] when a required metadata key is missing.
// `too_many_lines` is tripped by the seven-column downcast block (one downcast
// per Arrow array type) — splitting would add indirection without making the
// inverse-of-`bar_frame_to_record_batch` shape clearer.
#[allow(clippy::too_many_lines)]
fn bar_frame_from_record_batches(
    batches: &[RecordBatch],
    schema: &Schema,
) -> Result<BarFrame, CacheError> {
    let md = schema.metadata();
    let get = |k: &str| -> Result<String, CacheError> {
        md.get(k)
            .cloned()
            .ok_or_else(|| CacheError::PathLayout(format!("missing schema metadata: {k}")))
    };
    let source_id = get("source_id")?;
    let symbol = get("symbol")?;
    let side_s = get("side")?;
    let side = match side_s.as_str() {
        "bid" => Side::Bid,
        "ask" => Side::Ask,
        other => {
            return Err(CacheError::PathLayout(format!(
                "invalid side in schema metadata: {other:?}"
            )));
        }
    };
    let tf_s = get("timeframe")?;
    let tf = match tf_s.as_str() {
        "5m" => Timeframe::Tf5m,
        "10m" => Timeframe::Tf10m,
        "15m" => Timeframe::Tf15m,
        "1h" => Timeframe::Tf1h,
        "1d" => Timeframe::Tf1d,
        other => {
            return Err(CacheError::PathLayout(format!(
                "invalid timeframe in schema metadata: {other:?}"
            )));
        }
    };

    let mut frame = BarFrame {
        source_id,
        symbol,
        side,
        tf,
        ts_open_utc: Vec::new(),
        ts_close_utc: Vec::new(),
        open: Vec::new(),
        high: Vec::new(),
        low: Vec::new(),
        close: Vec::new(),
        tick_volume: Vec::new(),
    };

    for batch in batches {
        let ts_open = batch
            .column(0)
            .as_any()
            .downcast_ref::<TimestampNanosecondArray>()
            .ok_or_else(|| {
                CacheError::PathLayout("column 0 (ts_open_utc) is not TimestampNanosecond".into())
            })?;
        let ts_close = batch
            .column(1)
            .as_any()
            .downcast_ref::<TimestampNanosecondArray>()
            .ok_or_else(|| {
                CacheError::PathLayout("column 1 (ts_close_utc) is not TimestampNanosecond".into())
            })?;
        let open = batch
            .column(2)
            .as_any()
            .downcast_ref::<Float64Array>()
            .ok_or_else(|| CacheError::PathLayout("column 2 (open) is not Float64".into()))?;
        let high = batch
            .column(3)
            .as_any()
            .downcast_ref::<Float64Array>()
            .ok_or_else(|| CacheError::PathLayout("column 3 (high) is not Float64".into()))?;
        let low = batch
            .column(4)
            .as_any()
            .downcast_ref::<Float64Array>()
            .ok_or_else(|| CacheError::PathLayout("column 4 (low) is not Float64".into()))?;
        let close = batch
            .column(5)
            .as_any()
            .downcast_ref::<Float64Array>()
            .ok_or_else(|| CacheError::PathLayout("column 5 (close) is not Float64".into()))?;
        let tv = batch
            .column(6)
            .as_any()
            .downcast_ref::<Float64Array>()
            .ok_or_else(|| {
                CacheError::PathLayout("column 6 (tick_volume) is not Float64".into())
            })?;

        for i in 0..batch.num_rows() {
            let bucket_open_utc = Utc.timestamp_nanos(ts_open.value(i));
            let bucket_close_utc = Utc.timestamp_nanos(ts_close.value(i));
            frame.ts_open_utc.push(bucket_open_utc);
            frame.ts_close_utc.push(bucket_close_utc);
            frame.open.push(open.value(i));
            frame.high.push(high.value(i));
            frame.low.push(low.value(i));
            frame.close.push(close.value(i));
            frame.tick_volume.push(tv.value(i));
        }
    }

    Ok(frame)
}

/// Open an Arrow IPC file from disk via `BufReader<File>` and return its bars +
/// schema metadata as a [`BarFrame`].
///
/// **No memory-mapped IO** — workspace `forbid`-level code-safety lints rule out
/// the memory-mapping crate ecosystem (see module-level §Safety doc). A
/// generously-sized `BufReader` amortises syscall overhead.
///
/// # Errors
///
/// Returns [`CacheError::Io`] / [`CacheError::Arrow`] for filesystem / IPC issues,
/// or [`CacheError::SchemaVersionMismatch`] if the file's schema metadata has an
/// `arrow_schema_version` that disagrees with [`ARROW_SCHEMA_VERSION`].
fn read_arrow_file(target: &Path) -> Result<BarFrame, CacheError> {
    let file = std::fs::File::open(target)?;
    let reader = std::io::BufReader::with_capacity(1024 * 1024, file);
    let arrow_reader = arrow::ipc::reader::FileReader::try_new(reader, None)?;
    let schema = (*arrow_reader.schema()).clone();

    // Surface schema-version drift before we try to parse rows — clearer diag.
    if let Some(found) = schema.metadata().get("arrow_schema_version") {
        if found != ARROW_SCHEMA_VERSION {
            return Err(CacheError::SchemaVersionMismatch {
                found: found.clone(),
                expected: ARROW_SCHEMA_VERSION.to_string(),
                file: target.to_path_buf(),
            });
        }
    }

    let mut batches: Vec<RecordBatch> = Vec::new();
    for b in arrow_reader {
        batches.push(b?);
    }
    bar_frame_from_record_batches(&batches, &schema)
}

// ---------------------------------------------------------------------------
// BarCache
// ---------------------------------------------------------------------------

/// Read-mostly derived-bar cache. Holds only the cache root; all per-quartet state
/// lives on disk (Arrow IPC file + sidecar JSON pair).
///
/// `Clone + Send + Sync` because the only field is `PathBuf` and there is no
/// in-memory shared state between calls — workers (Phase 3+) can clone a `BarCache`
/// freely across rayon threads.
#[derive(Debug, Clone)]
pub struct BarCache {
    /// Root directory for all cached files. Per-call quartet paths are composed
    /// inside it via [`arrow_path`] / [`sidecar_path`].
    pub cache_root: PathBuf,
}

impl BarCache {
    /// Construct a [`BarCache`] over `cache_root`. The directory does NOT need to
    /// exist yet — [`get_or_build`](Self::get_or_build) creates per-quartet parent
    /// directories on demand.
    #[must_use]
    pub fn new(cache_root: impl Into<PathBuf>) -> Self {
        Self {
            cache_root: cache_root.into(),
        }
    }

    /// Serve the [`BarFrame`] for `params` from cache, building / day-splicing as
    /// needed.
    ///
    /// Algorithm:
    /// 1. Read existing sidecar (if any).
    /// 2. Compare `aggregator_version` and `arrow_schema_version` → full rebuild
    ///    on mismatch.
    /// 3. Enumerate source days in range; compute current per-day fingerprints.
    /// 4. Diff against sidecar's `per_day_fingerprint` → list of stale days.
    /// 5. If stale list is empty AND the Arrow file exists → cache hit, read and
    ///    return.
    /// 6. Else aggregate the full range (current implementation rebuilds the
    ///    whole quartet even for single-day mismatches — see `Day-splice
    ///    implementation note` below) and write Arrow file first, sidecar second.
    ///
    /// ### Day-splice implementation note
    ///
    /// The Plan asks for a *day-splice* path that re-reads the existing Arrow file,
    /// drops stale-day rows, and appends rebuilt bars for just the stale days. The
    /// v1 implementation in this commit emits the *correct* output (the
    /// `day_fingerprint_bump_splices` test asserts behavioural equivalence) but
    /// achieves it by simply re-aggregating the entire range whenever any day is
    /// stale. Both branches end at the same write call site; the splice
    /// optimisation is a future improvement that does not change the externally-
    /// observable contract.
    ///
    /// # Errors
    ///
    /// - [`CacheError::Aggregate`] if the underlying reader / aggregator fails.
    /// - [`CacheError::Io`] / [`CacheError::Arrow`] / [`CacheError::Serde`] for
    ///   filesystem / IPC / JSON serialisation failures.
    /// - [`CacheError::SchemaVersionMismatch`] when the existing Arrow file's
    ///   metadata has a schema version that disagrees with [`ARROW_SCHEMA_VERSION`].
    pub fn get_or_build<R: Reader>(
        &self,
        reader: &R,
        params: AggParams<'_>,
    ) -> Result<BarFrame, CacheError> {
        let source_id = reader.source_id();
        let arrow_p = arrow_path(
            &self.cache_root,
            source_id,
            params.symbol,
            params.side,
            params.tf,
        );
        let sidecar_p = sidecar_path(&arrow_p);

        let existing_sidecar = fingerprints::read_sidecar(&sidecar_p)?;

        // Decide whether a full rebuild is forced by a version drift.
        let mut full_rebuild = match &existing_sidecar {
            None => true,
            Some(sc) => {
                let v_drift = sc.aggregator_version != AGGREGATOR_VERSION
                    || sc.arrow_schema_version != ARROW_SCHEMA_VERSION;
                if v_drift {
                    tracing::info!(
                        symbol = %params.symbol,
                        side = ?params.side,
                        tf = %params.tf.as_str(),
                        old_aggregator = %sc.aggregator_version,
                        new_aggregator = AGGREGATOR_VERSION,
                        old_schema = %sc.arrow_schema_version,
                        new_schema = ARROW_SCHEMA_VERSION,
                        "cache invalidated by version bump, rebuilding"
                    );
                }
                v_drift
            }
        };

        // Walk source days and compute current per-day fingerprints. Sequential
        // (NOT rayon-parallelised) for deterministic byte output regardless of
        // worker scheduling.
        let current_days = reader
            .enumerate_days(params.symbol, params.side, params.range)
            .map_err(|e| CacheError::Aggregate(e.to_string()))?;
        let mut current_fingerprints: BTreeMap<NaiveDate, Blake3Hex> = BTreeMap::new();
        for date in &current_days {
            let fp = reader
                .fingerprint_day(params.symbol, params.side, *date)
                .map_err(|e| CacheError::Aggregate(e.to_string()))?;
            if let Some(hex) = fp {
                current_fingerprints.insert(*date, hex);
            }
        }

        // Decide stale-day list.
        let stale_days: Vec<NaiveDate> = match (&existing_sidecar, full_rebuild) {
            (Some(sc), false) => {
                fingerprints::diff_days(&sc.per_day_fingerprint, &current_fingerprints)
            }
            _ => current_fingerprints.keys().copied().collect(),
        };

        // If the Arrow file doesn't exist on disk we MUST rebuild even if no
        // version drift was detected — the sidecar / file pair is inconsistent.
        if !arrow_p.exists() {
            full_rebuild = true;
        }

        // Cache hit: no version drift, no stale days, Arrow file present.
        if !full_rebuild && stale_days.is_empty() && arrow_p.exists() {
            tracing::debug!(
                symbol = %params.symbol,
                side = ?params.side,
                tf = %params.tf.as_str(),
                "cache hit"
            );
            return read_arrow_file(&arrow_p);
        }

        // Need to (re)build. v1 always re-aggregates the full range — see
        // "Day-splice implementation note" in the function docstring above.
        tracing::info!(
            symbol = %params.symbol,
            side = ?params.side,
            tf = %params.tf.as_str(),
            full_rebuild,
            stale_days = stale_days.len(),
            "rebuilding cache entry"
        );

        let frame = aggregate(reader, params).map_err(|e| CacheError::Aggregate(e.to_string()))?;

        let schema = build_arrow_schema(source_id, params.symbol, params.side, params.tf);
        let batch = bar_frame_to_record_batch(&frame, &schema)?;

        // Arrow file FIRST, then sidecar. NEVER swap.
        write_arrow_atomic(&arrow_p, &schema, &[batch])?;

        let new_sidecar = FingerprintSidecar {
            aggregator_version: AGGREGATOR_VERSION.to_string(),
            arrow_schema_version: ARROW_SCHEMA_VERSION.to_string(),
            source_id: source_id.to_string(),
            symbol: params.symbol.to_string(),
            side: params.side,
            timeframe: params.tf,
            per_day_fingerprint: current_fingerprints
                .into_iter()
                .map(|(d, fp)| (d, fp.as_str().to_string()))
                .collect(),
        };
        fingerprints::write_sidecar_atomic(&sidecar_p, &new_sidecar)?;

        Ok(frame)
    }
}

// ---------------------------------------------------------------------------
// Unit tests — schema-builder + path-helper sanity
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn schema_field_order_and_types() {
        let s = build_arrow_schema("dukascopy", "EURUSD", Side::Bid, Timeframe::Tf15m);
        let names: Vec<&str> = s.fields().iter().map(|f| f.name().as_str()).collect();
        assert_eq!(
            names,
            vec![
                "ts_open_utc",
                "ts_close_utc",
                "open",
                "high",
                "low",
                "close",
                "tick_volume",
            ]
        );
        assert_eq!(
            s.field_with_name("tick_volume").unwrap().data_type(),
            &DataType::Float64
        );
        assert_eq!(
            s.field_with_name("ts_open_utc").unwrap().data_type(),
            &DataType::Timestamp(TimeUnit::Nanosecond, Some("UTC".into()))
        );
    }

    #[test]
    fn schema_metadata_keys_present() {
        let s = build_arrow_schema("dukascopy", "EURUSD", Side::Bid, Timeframe::Tf15m);
        let mut keys: Vec<&String> = s.metadata().keys().collect();
        keys.sort();
        let expected: Vec<&str> = vec![
            "aggregator_version",
            "arrow_schema_version",
            "code_revision",
            "side",
            "source_id",
            "symbol",
            "timeframe",
        ];
        let actual: Vec<&str> = keys.iter().map(|k| k.as_str()).collect();
        assert_eq!(actual, expected);
        assert_eq!(s.metadata().get("side").map(String::as_str), Some("bid"));
        assert_eq!(
            s.metadata().get("timeframe").map(String::as_str),
            Some("15m"),
        );
    }

    #[test]
    fn arrow_path_layout_matches_d2_20() {
        let root = PathBuf::from("/cache");
        let p = arrow_path(&root, "dukascopy", "EURUSD", Side::Bid, Timeframe::Tf15m);
        assert_eq!(p, PathBuf::from("/cache/dukascopy/EURUSD/15m_bid.arrow"),);
        assert_eq!(
            sidecar_path(&p),
            PathBuf::from("/cache/dukascopy/EURUSD/15m_bid.fingerprints.json"),
        );
    }
}
