//! The trial registry: an append-only JSON Lines file with one [`RegistryRecord`] per
//! line, a `sweep` header when a sweep starts and one `trial` per cell as it finishes.
//!
//! Every field a trial's result depends on is written beside it, so a registry line can
//! be read without the spec that produced it. Loading a registry back is lossless: the
//! records compare equal to the ones written, because every number is read exactly
//! ([`super::json::parse_exact`]).
//!
//! **Concurrent writers (#59, #50).** [`JsonlRegistry::append`] opens the file in append
//! mode with no buffer of its own, and each record is serialised whole, newline
//! included, and written with one `write_all`. On a local POSIX filesystem each record is
//! then one append that other appenders cannot split, so several sweeps, or several
//! processes, can append to one file. Network filesystems (NFS) do not promise that.
//!
//! **A truncated tail (a crash mid-write) is detected and refused**, never repaired or
//! skipped: bytes after the last newline make [`JsonlRegistry::append`] and
//! [`read_registry`] fail with [`RegistryError::TruncatedTail`], which gives the byte
//! offset where the incomplete line starts. Truncating the file to that offset removes
//! the incomplete record and keeps every complete one.

use std::collections::BTreeMap;
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::path::Path;

use chrono::{DateTime, NaiveDate, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::{CostConfigRef, SweepSpec};
use crate::engine::{EndOfRun, EngineStats, SizingPolicy};
use crate::exit::ExitEvaluation;
use crate::ledger::{AccountFx, ClosedTrade, RunMetadata};
use crate::metrics::Metrics;

/// The registry schema version written in every record, and the only one
/// [`read_registry`] accepts (#59, #51).
///
/// Additive changes keep it, as the findings envelope does: a new field carries a serde
/// default, so lines written before it still read. It moves only for a change an older
/// reader would misread, and a reader then refuses a version it does not know rather
/// than drop the fields it does not understand.
pub const SCHEMA_VERSION: u32 = 1;

/// One registry line.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum RegistryRecord {
    /// Written once when a sweep starts.
    Sweep(Box<SweepRecord>),
    /// One cell's trial.
    Trial(Box<TrialRecord>),
}

/// The header of one sweep.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SweepRecord {
    /// [`SCHEMA_VERSION`].
    pub schema_version: u32,
    /// blake3 of the code version and build, the spec and the cost config hash.
    pub sweep_id: String,
    /// `CARGO_PKG_VERSION` of tradedesk-backtest.
    pub code_version: String,
    /// The build ([`super::CODE_REVISION`]: the git commit, `dirty-` prefixed for a
    /// modified tree). `None` in records written before it was recorded.
    #[serde(default)]
    pub code_revision: Option<String>,
    /// The spec as given.
    pub spec: SweepSpec,
    /// Where the cost config came from and its blake3.
    pub cost_config: CostConfigRef,
    /// The venue, product, exit mode, FX and cost placeholders every trial runs under.
    pub run: RunMetadata,
    /// Number of cells (instruments × parameter cells).
    pub cells: usize,
    /// When the sweep started.
    pub started_utc: DateTime<Utc>,
}

/// One trial: a cell run end to end, or the reason it could not be.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TrialRecord {
    /// [`SCHEMA_VERSION`].
    pub schema_version: u32,
    /// blake3 of everything the trial's result depends on: the code version and build,
    /// strategy, params and resolved config, instrument, window and warm-up, venue, cost
    /// config hash, exit mode, FX, sizing, end-of-run rule, capital and risk-free rate.
    pub trial_id: String,
    /// The sweep this trial ran in.
    pub sweep_id: String,
    /// The cell's position in the sweep (instruments outer, parameter cells inner).
    pub index: usize,
    /// `CARGO_PKG_VERSION` of tradedesk-backtest.
    pub code_version: String,
    /// The build ([`super::CODE_REVISION`]); `None` in records written before it was
    /// recorded.
    #[serde(default)]
    pub code_revision: Option<String>,
    /// The strategy's registered name.
    pub strategy: String,
    /// The instrument.
    pub instrument: String,
    /// The cell: parameter path → value, as given.
    pub params: BTreeMap<String, Value>,
    /// The full strategy config the cell resolved to; `None` if it did not resolve.
    pub config: Option<Value>,
    /// First UTC day of the trading window.
    pub first_day: NaiveDate,
    /// Last UTC day of the trading window (inclusive).
    pub last_day: NaiveDate,
    /// Calendar days of warm-up loaded before `first_day`.
    pub warmup_days: u32,
    /// Venue id.
    pub venue: String,
    /// The cost config's source and blake3.
    pub cost_config: CostConfigRef,
    /// Stop/target evaluation mode.
    pub exit_evaluation: ExitEvaluation,
    /// Fixed FX table.
    pub account_fx: AccountFx,
    /// Entry sizing.
    pub sizing: SizingPolicy,
    /// End-of-run handling.
    pub end_of_run: EndOfRun,
    /// Starting capital, account currency.
    pub starting_capital: f64,
    /// Annual risk-free rate.
    pub risk_free_rate: f64,
    /// The venue, product, exit mode, FX and cost placeholders.
    pub run: RunMetadata,
    /// When the cell started.
    pub started_utc: DateTime<Utc>,
    /// When the cell finished.
    pub finished_utc: DateTime<Utc>,
    /// The result.
    pub outcome: TrialOutcome,
}

/// A trial's result. A ruined run is `Ok` (its metrics say so); `Failed` means the cell
/// could not be run to the end.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum TrialOutcome {
    /// The run finished.
    Ok {
        /// What the engine did.
        engine: EngineStats,
        /// Every M1-R5 metric, equity curve included.
        metrics: Box<Metrics>,
        /// Every closed trade, in closing order (`Ledger::closed_trades`): what the
        /// walk-forward attributes to its test windows by exit time (#62). `None` in
        /// records written before trades were recorded.
        #[serde(default)]
        trades: Option<Vec<ClosedTrade>>,
    },
    /// The cell failed.
    Failed {
        /// `config` (the cell did not resolve to a valid strategy), `run` (the engine
        /// refused), or `panic`.
        stage: FailureStage,
        /// The error.
        error: String,
    },
}

/// Where a cell failed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum FailureStage {
    /// The parameters did not give a valid strategy config.
    Config,
    /// The engine or the metrics refused.
    Run,
    /// The cell panicked; the sweep carried on.
    Panic,
}

impl TrialRecord {
    /// The metrics of a completed trial.
    #[must_use]
    pub fn metrics(&self) -> Option<&Metrics> {
        match &self.outcome {
            TrialOutcome::Ok { metrics, .. } => Some(metrics),
            TrialOutcome::Failed { .. } => None,
        }
    }

    /// The closed trades of a completed trial; `None` for a failed cell or a record
    /// written before trades were recorded.
    #[must_use]
    pub fn trades(&self) -> Option<&[ClosedTrade]> {
        match &self.outcome {
            TrialOutcome::Ok { trades, .. } => trades.as_deref(),
            TrialOutcome::Failed { .. } => None,
        }
    }
}

/// Everything a trial's result depends on, hashed into its id. The sweep's other
/// cells are not in it, so a trial keeps its id when a grid grows.
#[derive(Debug, Serialize)]
pub(crate) struct TrialKey<'a> {
    pub code_version: &'a str,
    pub code_revision: &'a str,
    pub strategy: &'a str,
    pub instrument: &'a str,
    pub params: &'a BTreeMap<String, Value>,
    pub config: Option<&'a Value>,
    pub first_day: NaiveDate,
    pub last_day: NaiveDate,
    pub warmup_days: u32,
    pub venue: &'a str,
    pub cost_config_blake3: &'a str,
    pub exit_evaluation: ExitEvaluation,
    pub account_fx: &'a AccountFx,
    pub sizing: SizingPolicy,
    pub end_of_run: EndOfRun,
    pub starting_capital: f64,
    pub risk_free_rate: f64,
}

/// blake3 hex of `value`'s canonical JSON (struct fields in declaration order, maps
/// sorted by key).
pub(crate) fn hash_json<T: Serialize>(value: &T) -> String {
    let bytes = serde_json::to_vec(value).expect("registry types serialise to JSON");
    blake3::hash(&bytes).to_hex().to_string()
}

/// Where registry records go.
pub trait RegistrySink {
    /// Persist one record.
    ///
    /// # Errors
    /// An I/O failure; the sweep stops writing and returns it.
    fn write(&mut self, record: &RegistryRecord) -> std::io::Result<()>;
}

/// In memory, for tests and callers that post-process.
impl RegistrySink for Vec<RegistryRecord> {
    fn write(&mut self, record: &RegistryRecord) -> std::io::Result<()> {
        self.push(record.clone());
        Ok(())
    }
}

/// A JSON Lines registry: one record per line, each written with a single `write_all`
/// of the whole line and flushed.
#[derive(Debug)]
pub struct JsonlRegistry<W: Write> {
    out: W,
}

impl JsonlRegistry<File> {
    /// Open `path` for appending, creating it if needed. Existing lines are kept, and
    /// each record is one append (see the module docs on concurrent writers).
    ///
    /// # Errors
    /// [`RegistryError::Io`] when the file cannot be opened or read, and
    /// [`RegistryError::TruncatedTail`] when it ends in an incomplete line.
    pub fn append(path: impl AsRef<Path>) -> Result<Self, RegistryError> {
        let mut file = OpenOptions::new()
            .create(true)
            .read(true)
            .append(true)
            .open(path)?;
        if let Some(offset) = incomplete_tail(&mut file)? {
            return Err(RegistryError::TruncatedTail { offset });
        }
        Ok(Self::new(file))
    }
}

/// Where the incomplete last line of `file` starts, if it does not end in a newline.
/// Reads backwards from the end, so a large registry is not read whole.
fn incomplete_tail(file: &mut File) -> std::io::Result<Option<u64>> {
    const CHUNK: u64 = 64 * 1024;
    let len = file.metadata()?.len();
    let mut end = len;
    let mut buf = Vec::new();
    while end > 0 {
        let start = end.saturating_sub(CHUNK);
        buf.resize(
            usize::try_from(end - start).expect("a chunk fits in memory"),
            0,
        );
        file.seek(SeekFrom::Start(start))?;
        file.read_exact(&mut buf)?;
        if end == len && buf.last() == Some(&b'\n') {
            return Ok(None);
        }
        if let Some(i) = buf.iter().rposition(|&b| b == b'\n') {
            return Ok(Some(start + i as u64 + 1));
        }
        end = start;
    }
    Ok((len > 0).then_some(0))
}

impl<W: Write> JsonlRegistry<W> {
    /// A registry writing to `out`.
    pub fn new(out: W) -> Self {
        Self { out }
    }

    /// The writer back.
    pub fn into_inner(self) -> W {
        self.out
    }
}

impl<W: Write> RegistrySink for JsonlRegistry<W> {
    /// The whole line, newline included, is built first and written with one
    /// `write_all`, so a serialisation error writes nothing and concurrent appenders
    /// cannot interleave inside a record.
    fn write(&mut self, record: &RegistryRecord) -> std::io::Result<()> {
        let mut line = serde_json::to_vec(record)?;
        line.push(b'\n');
        self.out.write_all(&line)?;
        self.out.flush()
    }
}

/// Why a registry could not be read.
#[derive(Debug, thiserror::Error)]
pub enum RegistryError {
    /// The file could not be read.
    #[error("reading the registry: {0}")]
    Io(#[from] std::io::Error),
    /// A line is not JSON.
    #[error("registry line {line}: {source}")]
    Syntax {
        /// 1-based line number.
        line: usize,
        /// The syntax error.
        #[source]
        source: super::json::JsonError,
    },
    /// A line is JSON but not a registry record.
    #[error("registry line {line}: {source}")]
    Parse {
        /// 1-based line number.
        line: usize,
        /// The parse error.
        #[source]
        source: serde_json::Error,
    },
    /// A line's `schema_version` is missing or is not one this reader knows.
    #[error(
        "registry line {line}: schema_version {found} is not supported (this reader \
         knows {supported})"
    )]
    UnsupportedSchema {
        /// 1-based line number.
        line: usize,
        /// The `schema_version` found (`null` when absent).
        found: Value,
        /// The version this reader knows ([`SCHEMA_VERSION`]).
        supported: u32,
    },
    /// The file ends in an incomplete line: a write was cut short (a crash, a full
    /// disk). Nothing is read or appended until it is removed.
    #[error(
        "the registry ends in an incomplete line starting at byte {offset} (a write was \
         cut short); truncate the file to {offset} bytes to keep every complete record"
    )]
    TruncatedTail {
        /// Byte offset where the incomplete line starts.
        offset: u64,
    },
}

/// Read every record from a JSON Lines registry. Blank lines are skipped.
///
/// # Errors
/// [`RegistryError`] for an I/O failure, a line that is not a record, a
/// `schema_version` other than [`SCHEMA_VERSION`] ([`RegistryError::UnsupportedSchema`]),
/// or an incomplete last line ([`RegistryError::TruncatedTail`]).
pub fn read_registry(path: impl AsRef<Path>) -> Result<Vec<RegistryRecord>, RegistryError> {
    parse_registry(BufReader::new(File::open(path)?))
}

/// [`read_registry`] over any reader.
///
/// # Errors
/// As [`read_registry`].
pub fn parse_registry(mut input: impl BufRead) -> Result<Vec<RegistryRecord>, RegistryError> {
    let mut records = Vec::new();
    let mut buf = Vec::new();
    let mut offset: u64 = 0;
    let mut line = 0;
    loop {
        buf.clear();
        let n = input.read_until(b'\n', &mut buf)?;
        if n == 0 {
            break;
        }
        line += 1;
        if buf.last() != Some(&b'\n') {
            return Err(RegistryError::TruncatedTail { offset });
        }
        offset += n as u64;
        let text = std::str::from_utf8(&buf[..n - 1])
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        if text.trim().is_empty() {
            continue;
        }
        let value = super::json::parse_exact(text)
            .map_err(|source| RegistryError::Syntax { line, source })?;
        let found = value.get("schema_version").cloned().unwrap_or(Value::Null);
        if found.as_u64() != Some(u64::from(SCHEMA_VERSION)) {
            return Err(RegistryError::UnsupportedSchema {
                line,
                found,
                supported: SCHEMA_VERSION,
            });
        }
        records.push(
            serde_json::from_value(value)
                .map_err(|source| RegistryError::Parse { line, source })?,
        );
    }
    Ok(records)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sweep::{Prepared, SweepSpec};

    /// A sweep header well over 8 KiB (a long grid), the size QA's probe interleaved at.
    fn big_record(tag: u32) -> RegistryRecord {
        let spec = format!(
            r#"
            name = "concurrency {tag}"
            strategy = "threshold_cross"
            base = "default"
            instruments = ["XAUUSD"]
            first_day = "2024-01-01"
            last_day = "2024-03-31"
            venue = "ig_spread_bet"
            exit_evaluation = {{ mode = "intrabar", both_hit = "stop_first" }}
            account_fx = {{ account_currency = "GBP", rates = {{ USD = 0.8 }} }}
            sizing = {{ kind = "stake_per_point", stake = 1.0 }}
            end_of_run = "close"
            starting_capital = 25000.0
            [grid]
            target_distance = [{}]
            "#,
            (0..2000)
                .map(|i| format!("{}.125", 1000 + i))
                .collect::<Vec<_>>()
                .join(", ")
        );
        let spec: SweepSpec = toml::from_str(&spec).unwrap();
        let mut record = Prepared::new(&spec, &crate::toy::registry())
            .unwrap()
            .header();
        if let RegistryRecord::Sweep(s) = &mut record {
            s.started_utc = DateTime::<Utc>::UNIX_EPOCH;
        }
        assert!(serde_json::to_vec(&record).unwrap().len() > 16 * 1024);
        record
    }

    fn scratch(name: &str) -> std::path::PathBuf {
        let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../target/registry-unit-tests");
        let dir = std::env::var_os("CARGO_TARGET_DIR").map_or(dir, |t| {
            std::path::PathBuf::from(t).join("registry-unit-tests")
        });
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join(name);
        let _ = std::fs::remove_file(&path);
        path
    }

    #[test]
    fn an_unknown_schema_version_is_refused_with_its_line() {
        // QA's probe on #51: every record rewritten to `"schema_version": 2`.
        let mut text = Vec::new();
        {
            let mut registry = JsonlRegistry::new(&mut text);
            registry.write(&big_record(1)).unwrap();
            registry.write(&big_record(2)).unwrap();
        }
        let text = String::from_utf8(text).unwrap();
        assert_eq!(parse_registry(text.as_bytes()).unwrap().len(), 2);
        let v1 = format!("\"schema_version\":{SCHEMA_VERSION}");
        let v2 = text.replace(&v1, "\"schema_version\":2");
        let err = parse_registry(v2.as_bytes()).unwrap_err();
        assert!(
            matches!(&err, RegistryError::UnsupportedSchema { line: 1, found, supported: 1 }
                if found.as_u64() == Some(2)),
            "{err}"
        );
        assert!(err.to_string().contains("registry line 1"), "{err}");
        // Only the second line changed: refused at line 2.
        let (first, second) = text.split_once('\n').unwrap();
        let mixed = format!(
            "{first}\n{}",
            second.replacen(&v1, "\"schema_version\":2", 1)
        );
        assert!(matches!(
            parse_registry(mixed.as_bytes()),
            Err(RegistryError::UnsupportedSchema { line: 2, .. })
        ));
        // A line with no schema_version at all is refused too.
        let missing = text.replacen(&format!("{v1},"), "", 1);
        assert!(matches!(
            parse_registry(missing.as_bytes()),
            Err(RegistryError::UnsupportedSchema {
                line: 1,
                found: Value::Null,
                ..
            })
        ));
    }

    #[test]
    fn concurrent_appenders_never_interleave_a_record() {
        // QA's probe on #50: several writers, each with its own handle, append records
        // larger than a BufWriter's 8 KiB to one file at the same time.
        let path = scratch("concurrent.jsonl");
        let records: Vec<RegistryRecord> = (0..4).map(big_record).collect();
        std::thread::scope(|scope| {
            for record in &records {
                let path = &path;
                scope.spawn(move || {
                    let mut registry = JsonlRegistry::append(path).unwrap();
                    for _ in 0..10 {
                        registry.write(record).unwrap();
                    }
                });
            }
        });
        let back = read_registry(&path).unwrap();
        assert_eq!(back.len(), 40);
        for record in &records {
            assert_eq!(back.iter().filter(|r| *r == record).count(), 10);
        }
    }

    #[test]
    fn a_truncated_tail_is_detected_and_refused() {
        // QA's second probe on #50: a crash cut the last record short.
        let path = scratch("truncated.jsonl");
        {
            let mut registry = JsonlRegistry::append(&path).unwrap();
            registry.write(&big_record(1)).unwrap();
            registry.write(&big_record(2)).unwrap();
        }
        let complete = std::fs::read(&path).unwrap();
        let second = u64::try_from(complete.iter().position(|&b| b == b'\n').unwrap() + 1).unwrap();
        let cut = complete.len() as u64 - 100;
        std::fs::OpenOptions::new()
            .write(true)
            .open(&path)
            .unwrap()
            .set_len(cut)
            .unwrap();
        // Reading and appending are both refused, with the offset of the partial line.
        let read = read_registry(&path).unwrap_err();
        assert!(
            matches!(read, RegistryError::TruncatedTail { offset } if offset == second),
            "{read}"
        );
        let append = JsonlRegistry::append(&path).unwrap_err();
        assert!(
            matches!(append, RegistryError::TruncatedTail { offset } if offset == second),
            "{append}"
        );
        assert!(
            append
                .to_string()
                .contains(&format!("truncate the file to {second} bytes"))
        );
        // Nothing was glued onto the partial line.
        assert_eq!(std::fs::metadata(&path).unwrap().len(), cut);
        // The documented repair keeps every complete record, and appending works again.
        std::fs::OpenOptions::new()
            .write(true)
            .open(&path)
            .unwrap()
            .set_len(second)
            .unwrap();
        assert_eq!(read_registry(&path).unwrap(), vec![big_record(1)]);
        JsonlRegistry::append(&path)
            .unwrap()
            .write(&big_record(3))
            .unwrap();
        assert_eq!(
            read_registry(&path).unwrap(),
            vec![big_record(1), big_record(3)]
        );
        // A tail cut inside the first line starts at byte 0; an empty file is fine.
        std::fs::write(&path, b"{\"kind\":").unwrap();
        assert!(matches!(
            JsonlRegistry::append(&path),
            Err(RegistryError::TruncatedTail { offset: 0 })
        ));
        std::fs::write(&path, b"").unwrap();
        assert!(JsonlRegistry::append(&path).is_ok());
        assert!(read_registry(&path).unwrap().is_empty());
    }
}
