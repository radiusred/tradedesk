//! Parameter sweeps: many cells of one strategy over in-memory bars, run in parallel
//! under rayon, every trial recorded in a registry, and the deflated Sharpe and the
//! probability of backtest overfitting reported across the sweep.
//!
//! - [`StrategyRegistry`] says which strategies a sweep can run: a name and a
//!   [`StrategyFactory`] for each, registered by the crate that owns the strategy
//!   ([`strategies`]). No strategy ships with this crate.
//! - [`SweepSpec`] says what to run: the strategy (by its registered name) and its base
//!   config, the
//!   instruments, the window and warm-up, the venue and cost config, the exit mode, the
//!   FX table, the sizing, the end-of-run rule, the capital, and a parameter grid (or an
//!   explicit list of cells) over the strategy's config.
//! - [`run_sweep`] loads the bars **once** and [`run_sweep_on`] runs every cell over that
//!   one [`MarketData`] under rayon. Records stream to a [`RegistrySink`] as cells
//!   finish, in cell order. A cell that fails (or panics) is a recorded failure, never a
//!   failure of the sweep.
//! - [`run_sweep_cancellable`] is [`run_sweep`] with a cancel flag (the CLI's SIGINT):
//!   once it is set no new cell starts, cells already running finish, records already in
//!   cell order are still written whole, and the sweep returns
//!   [`SweepError::Cancelled`]. [`validate_spec`] checks a spec without running it.
//! - [`registry`] holds the records and the JSON Lines file; [`report`] computes the
//!   deflated Sharpe ([`dsr`]) and PBO ([`cscv`]) from them.
//!
//! **Cells.** A cell maps parameter paths (dotted for nested fields, e.g.
//! `regime.confirm_on`) to values. It resolves by serialising the base config to JSON,
//! setting each path, and deserialising (unknown fields refused) before the strategy's
//! own validation. A path absent from the base config is a spec error, found before
//! anything runs; a value the strategy rejects is that cell's failure.
//!
//! **Price scale.** When the strategy's factory names a config path for it
//! ([`StrategyFactory::raw_scale_path`]), every cell's value there is set to its
//! instrument's `raw_scale` from the cost config, so a signal that depends on the price
//! scale is evaluated in quote units and the value is in the trial's recorded `config`.
//! It is derived, so a grid or cell that names the path is a spec error.

pub mod cscv;
pub mod dsr;
pub mod json;
pub mod registry;
pub mod report;
pub mod strategies;
pub mod walkforward;

use std::collections::{BTreeMap, BTreeSet};
use std::panic::{AssertUnwindSafe, catch_unwind};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;

use chrono::{Days, NaiveDate, Utc};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tradedesk_data::Reader;

use crate::cost::config::BUILTIN_COSTS_TOML;
use crate::engine::{self, EndOfRun, EndOfRunRule, EngineConfig, SizingPolicy};
use crate::exit::ExitEvaluation;
use crate::ledger::{AccountFx, Ledger, RunMetadata};
use crate::metrics::MetricsConfig;
use crate::{BarTimeframe, CostConfig, LoadRequest, MarketData, OneSidedPolicy, load};

pub use registry::{
    FailureStage, JsonlRegistry, RegistryError, RegistryRecord, RegistrySink, SCHEMA_VERSION,
    SweepRecord, TrialOutcome, TrialRecord, parse_registry, read_registry,
};
pub use report::{ReportError, ReportOptions, SweepReport};
pub use strategies::{BoxedStrategy, DuplicateStrategy, StrategyFactory, StrategyRegistry};
pub use walkforward::{WalkForwardError, WalkForwardOptions, WalkForwardReport};

/// The code version written into every record and hashed into every id.
pub const CODE_VERSION: &str = env!("CARGO_PKG_VERSION");

/// The build written into every record and hashed into every id beside
/// [`CODE_VERSION`] (#59, #53): the git commit the workspace was built from, prefixed
/// `dirty-` when the tree had uncommitted changes, or `unknown` without git
/// ([`tradedesk_data::CODE_REVISION`], from tradedesk-data's build script).
pub const CODE_REVISION: &str = tradedesk_data::CODE_REVISION;

/// The code a sweep runs: its release and its build.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Build {
    version: &'static str,
    revision: &'static str,
}

const BUILD: Build = Build {
    version: CODE_VERSION,
    revision: CODE_REVISION,
};

/// Which config a sweep's cells start from.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BaseConfig {
    /// The strategy's frozen reference config, when its factory provides one. The
    /// default when a spec gives no `base`.
    #[default]
    Frozen,
    /// The strategy's defaults.
    Default,
}

/// The run's FX table as written in a spec; validated by [`AccountFx::new`].
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FxSpec {
    /// Account currency, e.g. `"GBP"`.
    pub account_currency: String,
    /// Quote currency → account-currency value of one unit.
    #[serde(default)]
    pub rates: BTreeMap<String, f64>,
}

/// What a sweep runs. Loaded from TOML with [`SweepSpec::from_toml_str`] or
/// [`SweepSpec::from_path`]; see the crate README for an example.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SweepSpec {
    /// A label for people; not part of any trial id.
    #[serde(default)]
    pub name: String,
    /// The strategy's registered name ([`StrategyRegistry`]).
    pub strategy: String,
    /// The config every cell starts from.
    #[serde(default)]
    pub base: BaseConfig,
    /// Instruments; each is crossed with every parameter cell.
    pub instruments: Vec<String>,
    /// First UTC day of the trading window.
    pub first_day: NaiveDate,
    /// Last UTC day of the trading window (inclusive).
    pub last_day: NaiveDate,
    /// Calendar days loaded before `first_day` for indicator warm-up (default 0).
    #[serde(default)]
    pub warmup_days: u32,
    /// Venue id in the cost config.
    pub venue: String,
    /// A cost config file; absent means the builtin `config/costs.toml`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cost_config: Option<PathBuf>,
    /// Stop/target evaluation mode.
    pub exit_evaluation: ExitEvaluation,
    /// The fixed FX table.
    pub account_fx: FxSpec,
    /// Entry sizing.
    pub sizing: SizingPolicy,
    /// End-of-run handling.
    pub end_of_run: EndOfRun,
    /// Starting capital, account currency.
    pub starting_capital: f64,
    /// Annual risk-free rate (default 0).
    #[serde(default)]
    pub risk_free_rate: f64,
    /// A cartesian grid: parameter path → values. Keys in sorted order, the last
    /// varying fastest. Exclusive with `cells`.
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub grid: BTreeMap<String, Vec<Value>>,
    /// An explicit list of cells. Exclusive with `grid`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub cells: Vec<BTreeMap<String, Value>>,
}

/// Where a sweep's cost config came from, with the blake3 of its TOML text.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct CostConfigRef {
    /// `"builtin"` or the file path.
    pub source: String,
    /// blake3 hex of the TOML text.
    pub blake3: String,
}

/// Why a sweep could not run at all (a cell's failure is recorded instead).
#[derive(Debug, thiserror::Error)]
pub enum SweepError {
    /// The spec is invalid.
    #[error("invalid sweep spec: {0}")]
    Spec(String),
    /// The spec file could not be read or parsed.
    #[error("reading the sweep spec: {0}")]
    SpecFile(String),
    /// The cost config could not be loaded.
    #[error("cost config: {0}")]
    Costs(#[from] crate::ConfigError),
    /// The market data could not be loaded.
    #[error("loading market data: {0}")]
    Load(String),
    /// Writing to the registry failed; the sweep stopped.
    #[error("writing the registry: {0}")]
    Registry(#[from] std::io::Error),
    /// The cancel flag was set before every cell had run. The registry holds whole
    /// records only: the header (unless cancelled before it) and the first `written`
    /// trials in cell order.
    #[error("cancelled after {written} trial(s) were recorded")]
    Cancelled {
        /// Trials written before the sweep stopped.
        written: usize,
    },
}

/// What a finished sweep did.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SweepSummary {
    /// The sweep's id.
    pub sweep_id: String,
    /// Cells run.
    pub cells: usize,
    /// Cells that completed.
    pub ok: usize,
    /// Cells recorded as failures.
    pub failed: usize,
}

impl SweepSpec {
    /// Parse a spec from TOML. Validation happens when the sweep is prepared.
    ///
    /// # Errors
    /// [`SweepError::SpecFile`] when the TOML does not match the schema.
    pub fn from_toml_str(text: &str) -> Result<Self, SweepError> {
        toml::from_str(text).map_err(|e| SweepError::SpecFile(e.to_string()))
    }

    /// Read and parse a TOML spec file.
    ///
    /// # Errors
    /// [`SweepError::SpecFile`] when the file cannot be read or parsed.
    pub fn from_path(path: impl AsRef<std::path::Path>) -> Result<Self, SweepError> {
        let path = path.as_ref();
        let text = std::fs::read_to_string(path)
            .map_err(|e| SweepError::SpecFile(format!("{}: {e}", path.display())))?;
        Self::from_toml_str(&text)
    }

    /// The parameter cells, in order: the grid's cartesian product, the explicit list,
    /// or one empty cell.
    fn parameter_cells(&self) -> Vec<BTreeMap<String, Value>> {
        if !self.cells.is_empty() {
            return self.cells.clone();
        }
        let mut out = vec![BTreeMap::new()];
        for (key, values) in &self.grid {
            out = out
                .into_iter()
                .flat_map(|cell| {
                    values.iter().map(move |v| {
                        let mut c = cell.clone();
                        c.insert(key.clone(), v.clone());
                        c
                    })
                })
                .collect();
        }
        out
    }
}

/// Set the dotted `path` in `target` to `value`. Every segment must already exist.
fn set_path(target: &mut Value, path: &str, value: Value) -> Result<(), String> {
    let mut node = target;
    let mut segments = path.split('.').peekable();
    while let Some(segment) = segments.next() {
        let Value::Object(map) = node else {
            return Err(format!("{path:?}: {segment:?} is not inside an object"));
        };
        let Some(child) = map.get_mut(segment) else {
            return Err(format!("{path:?}: no field {segment:?} in the base config"));
        };
        if segments.peek().is_none() {
            *child = value;
            return Ok(());
        }
        node = child;
    }
    Err(format!("{path:?}: empty parameter path"))
}

/// One cell, resolved as far as it goes before running.
struct Cell {
    index: usize,
    instrument: String,
    params: BTreeMap<String, Value>,
    config: Result<Value, String>,
    trial_id: String,
}

/// A validated sweep, ready to load and run.
struct Prepared<'a> {
    spec: &'a SweepSpec,
    factory: &'a dyn StrategyFactory,
    build: Build,
    costs: CostConfig,
    cost_ref: CostConfigRef,
    fx: AccountFx,
    run: RunMetadata,
    sweep_id: String,
    cells: Vec<Cell>,
    timeframes: BTreeSet<BarTimeframe>,
}

impl<'a> Prepared<'a> {
    fn new(spec: &'a SweepSpec, strategies: &'a StrategyRegistry) -> Result<Self, SweepError> {
        Self::built(spec, strategies, BUILD)
    }

    #[allow(clippy::too_many_lines, reason = "one validation pass over the spec")]
    fn built(
        spec: &'a SweepSpec,
        strategies: &'a StrategyRegistry,
        build: Build,
    ) -> Result<Self, SweepError> {
        let bad = |m: String| Err(SweepError::Spec(m));
        let Some(factory) = strategies.get(&spec.strategy) else {
            let known = strategies.names().collect::<Vec<_>>();
            return bad(format!(
                "unknown strategy {:?}; registered: {known:?}",
                spec.strategy
            ));
        };
        if spec.instruments.is_empty() {
            return bad("instruments is empty".into());
        }
        if spec.instruments.iter().collect::<BTreeSet<_>>().len() != spec.instruments.len() {
            return bad("an instrument is listed twice".into());
        }
        if spec.last_day < spec.first_day {
            return bad(format!(
                "last_day {} is before first_day {}",
                spec.last_day, spec.first_day
            ));
        }
        if !(spec.starting_capital.is_finite() && spec.starting_capital > 0.0) {
            return bad("starting_capital must be finite and > 0".into());
        }
        if !spec.risk_free_rate.is_finite() {
            return bad("risk_free_rate must be finite".into());
        }
        if !spec.grid.is_empty() && !spec.cells.is_empty() {
            return bad("give either grid or cells, not both".into());
        }
        if let Some((k, _)) = spec.grid.iter().find(|(_, v)| v.is_empty()) {
            return bad(format!("grid parameter {k:?} has no values"));
        }
        if let Some(path) = factory.raw_scale_path() {
            if spec.grid.contains_key(path) || spec.cells.iter().any(|c| c.contains_key(path)) {
                return bad(format!(
                    "{path:?} is set from the instrument's raw_scale in the cost config and \
                     cannot be a parameter"
                ));
            }
        }

        let (costs, cost_ref) = match &spec.cost_config {
            None => (
                CostConfig::builtin(),
                CostConfigRef {
                    source: "builtin".into(),
                    blake3: blake3::hash(BUILTIN_COSTS_TOML.as_bytes())
                        .to_hex()
                        .to_string(),
                },
            ),
            Some(path) => {
                let text =
                    std::fs::read_to_string(path).map_err(|source| crate::ConfigError::Io {
                        path: path.clone(),
                        source,
                    })?;
                (
                    CostConfig::from_toml_str(&text)?,
                    CostConfigRef {
                        source: path.display().to_string(),
                        blake3: blake3::hash(text.as_bytes()).to_hex().to_string(),
                    },
                )
            }
        };
        let fx = AccountFx::new(
            spec.account_fx.account_currency.clone(),
            spec.account_fx.rates.iter().map(|(k, v)| (k.clone(), *v)),
        )
        .map_err(|e| SweepError::Spec(format!("account_fx: {e}")))?;
        let ledger = Ledger::new(&costs, &spec.venue, spec.exit_evaluation, fx.clone())
            .map_err(|e| SweepError::Spec(e.to_string()))?;
        for instrument in &spec.instruments {
            let model = ledger.fill_model(instrument).ok_or_else(|| {
                SweepError::Spec(format!(
                    "venue {:?} has no costs for {instrument:?}",
                    spec.venue
                ))
            })?;
            fx.rate(&model.spec().quote_currency)
                .map_err(|e| SweepError::Spec(format!("account_fx: {e}")))?;
        }
        let mut run = ledger.metadata().clone();
        run.end_of_run = Some(EndOfRunRule::new(spec.end_of_run));

        let base = factory.base_config(spec.base).map_err(SweepError::Spec)?;
        let parameter_cells = spec.parameter_cells();
        let mut cells = Vec::new();
        let mut timeframes = BTreeSet::new();
        for instrument in &spec.instruments {
            // Read, not re-shaped: the instrument's raw → quote scale (validation above
            // guarantees the venue prices it, so the spec exists).
            let raw_scale = costs
                .instrument(instrument)
                .map(|i| i.raw_scale)
                .ok_or_else(|| {
                    SweepError::Spec(format!("no instrument spec for {instrument:?}"))
                })?;
            for params in &parameter_cells {
                let mut config = base.clone();
                for (path, value) in params {
                    set_path(&mut config, path, value.clone()).map_err(SweepError::Spec)?;
                }
                if let Some(path) = factory.raw_scale_path() {
                    set_path(&mut config, path, Value::from(raw_scale))
                        .map_err(SweepError::Spec)?;
                }
                // Build once here to learn the timeframes and to check the values.
                let config = match factory.build(config.clone()) {
                    Ok(strategy) => {
                        timeframes.insert(strategy.primary_timeframe());
                        timeframes.extend(strategy.context_timeframes().iter().copied());
                        Ok(config)
                    }
                    Err(e) => Err(e),
                };
                let key = registry::TrialKey {
                    code_version: build.version,
                    code_revision: build.revision,
                    strategy: &spec.strategy,
                    instrument,
                    params,
                    config: config.as_ref().ok(),
                    first_day: spec.first_day,
                    last_day: spec.last_day,
                    warmup_days: spec.warmup_days,
                    venue: &spec.venue,
                    cost_config_blake3: &cost_ref.blake3,
                    exit_evaluation: spec.exit_evaluation,
                    account_fx: &fx,
                    sizing: spec.sizing,
                    end_of_run: spec.end_of_run,
                    starting_capital: spec.starting_capital,
                    risk_free_rate: spec.risk_free_rate,
                };
                cells.push(Cell {
                    index: cells.len(),
                    instrument: instrument.clone(),
                    params: params.clone(),
                    trial_id: registry::hash_json(&key),
                    config,
                });
            }
        }
        let sweep_id =
            registry::hash_json(&(build.version, build.revision, spec, &cost_ref.blake3));
        Ok(Self {
            spec,
            factory,
            build,
            costs,
            cost_ref,
            fx,
            run,
            sweep_id,
            cells,
            timeframes,
        })
    }

    fn load_first_day(&self) -> NaiveDate {
        self.spec
            .first_day
            .checked_sub_days(Days::new(u64::from(self.spec.warmup_days)))
            .unwrap_or(NaiveDate::MIN)
    }

    fn load_requests(&self) -> Vec<LoadRequest> {
        self.spec
            .instruments
            .iter()
            .map(|symbol| {
                LoadRequest::utc_days(
                    symbol.clone(),
                    self.load_first_day(),
                    self.spec.last_day,
                    self.timeframes.iter().copied(),
                )
            })
            .collect()
    }

    fn header(&self) -> RegistryRecord {
        RegistryRecord::Sweep(Box::new(SweepRecord {
            schema_version: SCHEMA_VERSION,
            sweep_id: self.sweep_id.clone(),
            code_version: self.build.version.into(),
            code_revision: Some(self.build.revision.into()),
            spec: self.spec.clone(),
            cost_config: self.cost_ref.clone(),
            run: self.run.clone(),
            cells: self.cells.len(),
            started_utc: Utc::now(),
        }))
    }

    fn record(
        &self,
        cell: &Cell,
        started: chrono::DateTime<Utc>,
        outcome: TrialOutcome,
    ) -> TrialRecord {
        let spec = self.spec;
        TrialRecord {
            schema_version: SCHEMA_VERSION,
            trial_id: cell.trial_id.clone(),
            sweep_id: self.sweep_id.clone(),
            index: cell.index,
            code_version: self.build.version.into(),
            code_revision: Some(self.build.revision.into()),
            strategy: spec.strategy.clone(),
            instrument: cell.instrument.clone(),
            params: cell.params.clone(),
            config: cell.config.as_ref().ok().cloned(),
            first_day: spec.first_day,
            last_day: spec.last_day,
            warmup_days: spec.warmup_days,
            venue: spec.venue.clone(),
            cost_config: self.cost_ref.clone(),
            exit_evaluation: spec.exit_evaluation,
            account_fx: self.fx.clone(),
            sizing: spec.sizing,
            end_of_run: spec.end_of_run,
            starting_capital: spec.starting_capital,
            risk_free_rate: spec.risk_free_rate,
            run: self.run.clone(),
            started_utc: started,
            finished_utc: Utc::now(),
            outcome,
        }
    }

    /// Run one cell over `data`.
    fn run_cell(&self, cell: &Cell, data: &MarketData) -> TrialOutcome {
        let failed = |stage, error: String| TrialOutcome::Failed { stage, error };
        let config = match &cell.config {
            Ok(config) => config.clone(),
            Err(e) => return failed(FailureStage::Config, e.clone()),
        };
        let mut strategy = match self.factory.build(config) {
            Ok(s) => s,
            Err(e) => return failed(FailureStage::Config, e),
        };
        let spec = self.spec;
        let engine_config = EngineConfig {
            instrument: cell.instrument.clone(),
            venue: spec.venue.clone(),
            exit_evaluation: spec.exit_evaluation,
            account_fx: self.fx.clone(),
            sizing: spec.sizing,
            end_of_run: spec.end_of_run,
            metrics: MetricsConfig {
                first_day: spec.first_day,
                last_day: spec.last_day,
                starting_capital: spec.starting_capital,
                risk_free_rate: spec.risk_free_rate,
            },
        };
        match engine::run(strategy.as_mut(), data, &self.costs, &engine_config) {
            Ok(out) => TrialOutcome::Ok {
                engine: out.stats,
                metrics: Box::new(out.metrics),
                trades: Some(out.ledger.closed_trades().to_vec()),
            },
            Err(e) => failed(FailureStage::Run, e.to_string()),
        }
    }
}

/// Check `spec` as a sweep would before running anything: the strategy is registered in
/// `strategies`, the schema-level checks, the cost config, the venue, the FX table and
/// every parameter path.
///
/// # Errors
/// [`SweepError::Spec`] or [`SweepError::Costs`], as [`run_sweep`] would return them.
pub fn validate_spec(spec: &SweepSpec, strategies: &StrategyRegistry) -> Result<(), SweepError> {
    Prepared::new(spec, strategies).map(|_| ())
}

/// Load the bars a sweep needs **once** through `reader`, then [`run_sweep_on`] them.
/// The spec's strategy is looked up in `strategies`.
///
/// # Errors
/// [`SweepError`] for an invalid spec (an unregistered strategy included), a cost
/// config or data load failure, or a registry write failure. A cell's failure is
/// recorded, not returned.
pub fn run_sweep<R: Reader>(
    spec: &SweepSpec,
    strategies: &StrategyRegistry,
    reader: &R,
    sink: &mut dyn RegistrySink,
) -> Result<SweepSummary, SweepError> {
    run_sweep_cancellable(spec, strategies, reader, sink, &AtomicBool::new(false))
}

/// [`run_sweep`] that stops early once `cancel` is set: no new cell starts, cells
/// already running finish, records already in cell order are written whole, and the
/// sweep returns [`SweepError::Cancelled`]. The flag is checked before the load, before
/// the header and before each cell; loading itself is not interrupted.
///
/// # Errors
/// As [`run_sweep`], plus [`SweepError::Cancelled`].
pub fn run_sweep_cancellable<R: Reader>(
    spec: &SweepSpec,
    strategies: &StrategyRegistry,
    reader: &R,
    sink: &mut dyn RegistrySink,
    cancel: &AtomicBool,
) -> Result<SweepSummary, SweepError> {
    let prepared = Prepared::new(spec, strategies)?;
    if cancel.load(Ordering::SeqCst) {
        return Err(SweepError::Cancelled { written: 0 });
    }
    let data = load(reader, &prepared.load_requests(), OneSidedPolicy::Reject)
        .map_err(|e| SweepError::Load(e.to_string()))?;
    execute_prepared(&prepared, &data, sink, cancel)
}

/// Run every cell of `spec` over `data`, already loaded (it must hold each instrument
/// from `first_day − warmup_days` to `last_day` at the strategy's timeframes). The
/// header is written first, then each trial as soon as every earlier cell is written.
///
/// # Errors
/// As [`run_sweep`], without the load.
pub fn run_sweep_on(
    spec: &SweepSpec,
    strategies: &StrategyRegistry,
    data: &MarketData,
    sink: &mut dyn RegistrySink,
) -> Result<SweepSummary, SweepError> {
    execute_prepared(
        &Prepared::new(spec, strategies)?,
        data,
        sink,
        &AtomicBool::new(false),
    )
}

fn execute_prepared(
    prepared: &Prepared<'_>,
    data: &MarketData,
    sink: &mut dyn RegistrySink,
    cancel: &AtomicBool,
) -> Result<SweepSummary, SweepError> {
    if cancel.load(Ordering::SeqCst) {
        return Err(SweepError::Cancelled { written: 0 });
    }
    sink.write(&prepared.header())?;
    let mut summary = SweepSummary {
        sweep_id: prepared.sweep_id.clone(),
        cells: prepared.cells.len(),
        ok: 0,
        failed: 0,
    };
    execute(
        prepared.cells.len(),
        |i| {
            let cell = &prepared.cells[i];
            let started = Utc::now();
            let outcome = prepared.run_cell(cell, data);
            prepared.record(cell, started, outcome)
        },
        |i, panic| {
            let cell = &prepared.cells[i];
            let outcome = TrialOutcome::Failed {
                stage: FailureStage::Panic,
                error: panic,
            };
            prepared.record(cell, Utc::now(), outcome)
        },
        |record| {
            match record.outcome {
                TrialOutcome::Ok { .. } => summary.ok += 1,
                TrialOutcome::Failed { .. } => summary.failed += 1,
            }
            sink.write(&RegistryRecord::Trial(Box::new(record)))
        },
        cancel,
    )?;
    let written = summary.ok + summary.failed;
    if written < summary.cells {
        return Err(SweepError::Cancelled { written });
    }
    Ok(summary)
}

fn panic_message(payload: &(dyn std::any::Any + Send)) -> String {
    payload
        .downcast_ref::<&str>()
        .map(|s| (*s).to_owned())
        .or_else(|| payload.downcast_ref::<String>().cloned())
        .unwrap_or_else(|| "a panic with a non-string payload".to_owned())
}

/// Run `run(i)` for `i in 0..n` on rayon and hand each result to `emit` **in index
/// order**, as soon as every lower index has been emitted (a reorder buffer over a
/// channel: results stream while later cells still run, and the order is
/// deterministic). A panic in `run(i)` becomes `on_panic(i, message)`. After an `emit`
/// error no more results are emitted, cells not yet started are skipped, and the error
/// is returned. Once `cancel` is set, cells not yet started are skipped too; results
/// already in order are still emitted, and the rest are dropped.
pub(crate) fn execute<T, F, P, E>(
    n: usize,
    run: F,
    on_panic: P,
    mut emit: E,
    cancel: &AtomicBool,
) -> std::io::Result<()>
where
    T: Send,
    F: Fn(usize) -> T + Sync,
    P: Fn(usize, String) -> T + Sync,
    E: FnMut(T) -> std::io::Result<()>,
{
    let stop = AtomicBool::new(false);
    let (tx, rx) = mpsc::channel::<(usize, T)>();
    std::thread::scope(|scope| {
        let (run, on_panic, stop) = (&run, &on_panic, &stop);
        scope.spawn(move || {
            (0..n).into_par_iter().for_each_with(tx, |tx, i| {
                if stop.load(Ordering::Relaxed) || cancel.load(Ordering::Relaxed) {
                    return;
                }
                let result = catch_unwind(AssertUnwindSafe(|| run(i)))
                    .unwrap_or_else(|payload| on_panic(i, panic_message(payload.as_ref())));
                // The receiver is gone only after an emit error; the result is dropped.
                let _ = tx.send((i, result));
            });
        });
        let mut pending = BTreeMap::new();
        let mut next = 0;
        for (i, result) in rx {
            pending.insert(i, result);
            while let Some(result) = pending.remove(&next) {
                if let Err(e) = emit(result) {
                    stop.store(true, Ordering::Relaxed);
                    return Err(e);
                }
                next += 1;
            }
        }
        Ok(())
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn execute_emits_in_order_and_isolates_a_panic() {
        let mut seen = Vec::new();
        execute(
            50,
            |i| {
                assert!(i != 17, "cell 17 blows up");
                // Later cells finish first, so the reorder buffer has work to do.
                std::thread::sleep(std::time::Duration::from_micros(
                    u64::try_from(50 - i).unwrap() * 20,
                ));
                Ok(i)
            },
            |i, msg| Err((i, msg)),
            |r| {
                seen.push(r);
                Ok(())
            },
            &AtomicBool::new(false),
        )
        .unwrap();
        assert_eq!(seen.len(), 50);
        for (i, r) in seen.iter().enumerate() {
            if i == 17 {
                assert_eq!(r, &Err((17, "cell 17 blows up".to_owned())));
            } else {
                assert_eq!(r, &Ok(i));
            }
        }
    }

    #[test]
    fn execute_stops_on_an_emit_error() {
        let mut emitted = 0;
        let err = execute(
            20,
            |i| i,
            |i, _| i,
            |i| {
                emitted += 1;
                if i == 3 {
                    Err(std::io::Error::other("disk full"))
                } else {
                    Ok(())
                }
            },
            &AtomicBool::new(false),
        )
        .unwrap_err();
        assert_eq!(err.to_string(), "disk full");
        assert_eq!(emitted, 4);
    }

    #[test]
    fn execute_stops_starting_cells_once_cancelled() {
        // The flag is set while cell 0 is emitted: every result emitted is whole and in
        // order, and far fewer than all the cells ran.
        let cancel = AtomicBool::new(false);
        let started = std::sync::atomic::AtomicUsize::new(0);
        let mut seen = Vec::new();
        execute(
            10_000,
            |i| {
                started.fetch_add(1, Ordering::SeqCst);
                std::thread::sleep(std::time::Duration::from_micros(50));
                i
            },
            |i, _| i,
            |i| {
                cancel.store(true, Ordering::SeqCst);
                seen.push(i);
                Ok(())
            },
            &cancel,
        )
        .unwrap();
        assert!(!seen.is_empty());
        assert_eq!(seen, (0..seen.len()).collect::<Vec<_>>());
        assert!(started.load(Ordering::SeqCst) < 10_000);
    }

    use crate::toy::{self, NAME, ThresholdConfig};

    #[test]
    fn the_base_config_round_trips_through_json_and_builds() {
        let strategies = toy::registry();
        let factory = strategies.get(NAME).unwrap();
        let value = factory.base_config(BaseConfig::Default).unwrap();
        let text = serde_json::to_string(&value).unwrap();
        assert_eq!(serde_json::from_str::<Value>(&text).unwrap(), value);
        let back: ThresholdConfig = serde_json::from_value(value.clone()).unwrap();
        assert_eq!(back, ThresholdConfig::default());
        assert!(factory.build(value.clone()).is_ok());
        // A strategy with no frozen config says so.
        let e = factory.base_config(BaseConfig::Frozen).unwrap_err();
        assert!(e.contains(NAME) && e.contains("default"), "{e}");
        // Unknown fields are refused, so a typo cannot fall back to a default.
        let mut typo = value;
        typo["enter_abov"] = Value::from(1.0);
        assert!(factory.build(typo).is_err());
    }

    #[test]
    fn a_spec_names_a_registered_strategy() {
        let spec: SweepSpec = toml::from_str(SPEC).unwrap();
        assert_eq!(spec.strategy, NAME);
        // The name round-trips through the spec as the string it is.
        let text = toml::to_string(&spec).unwrap();
        assert!(text.contains(&format!("strategy = \"{NAME}\"")), "{text}");
        assert_eq!(SweepSpec::from_toml_str(&text).unwrap(), spec);
        // An unregistered name is a spec error that lists the registered ones.
        let strategies = toy::registry();
        let mut other = spec.clone();
        other.strategy = "nope".into();
        let err = validate_spec(&other, &strategies).unwrap_err().to_string();
        assert!(err.contains("\"nope\"") && err.contains(NAME), "{err}");
        // With nothing registered, every spec is refused.
        let err = validate_spec(&spec, &StrategyRegistry::new())
            .unwrap_err()
            .to_string();
        assert!(err.contains("registered: []"), "{err}");
        assert!(validate_spec(&spec, &strategies).is_ok());
    }

    #[test]
    fn a_name_registers_once() {
        let mut strategies = toy::registry();
        let err = strategies
            .register(NAME, toy::ThresholdFactory)
            .err()
            .unwrap();
        assert_eq!(err, DuplicateStrategy(NAME.to_owned()));
        strategies.register("other", toy::ThresholdFactory).unwrap();
        assert_eq!(strategies.names().collect::<Vec<_>>(), ["other", NAME]);
        assert!(!strategies.is_empty() && StrategyRegistry::new().is_empty());
        assert_eq!(
            format!("{strategies:?}"),
            format!("{{\"other\", \"{NAME}\"}}")
        );
    }

    #[test]
    fn set_path_needs_every_segment_to_exist() {
        let mut v = serde_json::json!({"a": {"b": 1}, "c": null});
        set_path(&mut v, "a.b", Value::from(2)).unwrap();
        set_path(&mut v, "c", Value::from(3)).unwrap();
        assert_eq!(v, serde_json::json!({"a": {"b": 2}, "c": 3}));
        assert!(set_path(&mut v, "a.x", Value::from(1)).is_err());
        assert!(set_path(&mut v, "c.d", Value::from(1)).is_err());
        assert!(set_path(&mut v, "", Value::from(1)).is_err());
    }

    #[test]
    fn a_grid_expands_with_the_last_key_fastest() {
        let mut spec: SweepSpec = toml::from_str(SPEC).unwrap();
        spec.grid.insert("a".into(), vec![1.into(), 2.into()]);
        spec.grid.insert("b".into(), vec!["x".into(), "y".into()]);
        let cells = spec.parameter_cells();
        let pairs: Vec<(i64, &str)> = cells
            .iter()
            .map(|c| (c["a"].as_i64().unwrap(), c["b"].as_str().unwrap()))
            .collect();
        assert_eq!(pairs, vec![(1, "x"), (1, "y"), (2, "x"), (2, "y")]);
        spec.grid.clear();
        assert_eq!(spec.parameter_cells(), vec![BTreeMap::new()]);
    }

    const SPEC: &str = r#"
        strategy = "threshold_cross"
        base = "default"
        instruments = ["XAUUSD"]
        first_day = "2024-01-01"
        last_day = "2024-03-31"
        venue = "ig_spread_bet"
        exit_evaluation = { mode = "intrabar", both_hit = "stop_first" }
        account_fx = { account_currency = "GBP", rates = { USD = 0.8 } }
        sizing = { kind = "stake_per_point", stake = 1.0 }
        end_of_run = "close"
        starting_capital = 25000.0
    "#;

    #[test]
    fn a_spec_validates_before_running() {
        let strategies = toy::registry();
        let ok: SweepSpec = toml::from_str(SPEC).unwrap();
        assert!(Prepared::new(&ok, &strategies).is_ok());
        let with = |edit: &dyn Fn(&mut SweepSpec)| {
            let mut s = ok.clone();
            edit(&mut s);
            Prepared::new(&s, &strategies).err().map(|e| e.to_string())
        };
        let typo = with(&|s| {
            s.grid.insert("enter_abov".into(), vec![3.into()]);
        });
        assert!(typo.unwrap().contains("enter_abov"));
        assert!(with(&|s| s.instruments.push("NOPE".into())).is_some());
        assert!(with(&|s| s.venue = "nope".into()).is_some());
        assert!(
            with(&|s| s.account_fx.rates.clear())
                .unwrap()
                .contains("USD")
        );
        assert!(with(&|s| s.instruments.clear()).is_some());
        assert!(with(&|s| s.starting_capital = 0.0).is_some());
        assert!(
            with(&|s| {
                s.grid.insert("target_distance".into(), vec![1.into()]);
                s.cells.push(BTreeMap::new());
            })
            .is_some()
        );
        // An unknown top-level key is a parse error.
        assert!(SweepSpec::from_toml_str(&format!("{SPEC}\nbogus = 1\n")).is_err());
    }

    #[test]
    fn every_cell_carries_its_instruments_raw_scale() {
        let strategies = toy::registry();
        let mut spec: SweepSpec = toml::from_str(SPEC).unwrap();
        spec.instruments = vec!["XAUUSD".into(), "USA500IDXUSD".into()];
        spec.grid
            .insert("stop_distance".into(), vec![3000.0.into(), 4500.0.into()]);
        let prepared = Prepared::new(&spec, &strategies).unwrap();
        let scales: Vec<(String, f64)> = prepared
            .cells
            .iter()
            .map(|c| {
                let config = c.config.as_ref().unwrap();
                (
                    c.instrument.clone(),
                    config["price_scale"].as_f64().unwrap(),
                )
            })
            .collect();
        assert_eq!(
            scales,
            vec![
                ("XAUUSD".into(), 0.01),
                ("XAUUSD".into(), 0.01),
                ("USA500IDXUSD".into(), 1.0),
                ("USA500IDXUSD".into(), 1.0),
            ]
        );
        // The built strategy assumes the instrument's scale, so the engine accepts it.
        let built = strategies
            .get(NAME)
            .unwrap()
            .build(prepared.cells[0].config.clone().unwrap())
            .unwrap();
        assert_eq!(built.price_scale(), Some(0.01));
        // The path is derived: naming it is a spec error.
        let mut named = spec.clone();
        named.grid.insert("price_scale".into(), vec![1.0.into()]);
        let err = Prepared::new(&named, &strategies)
            .err()
            .unwrap()
            .to_string();
        assert!(err.contains("price_scale"), "{err}");
    }

    #[test]
    fn trial_ids_ignore_the_other_cells_but_not_the_inputs() {
        let strategies = toy::registry();
        let base: SweepSpec = toml::from_str(SPEC).unwrap();
        let ids = |spec: &SweepSpec| -> Vec<String> {
            Prepared::new(spec, &strategies)
                .unwrap()
                .cells
                .iter()
                .map(|c| c.trial_id.clone())
                .collect()
        };
        let mut one = base.clone();
        one.grid.insert("target_distance".into(), vec![4.0.into()]);
        let mut two = one.clone();
        two.grid
            .get_mut("target_distance")
            .unwrap()
            .push(5.0.into());
        two.name = "renamed".into();
        assert_eq!(
            ids(&one)[0],
            ids(&two)[0],
            "a growing grid keeps the trial id"
        );
        let mut capital = one.clone();
        capital.starting_capital = 30_000.0;
        assert_ne!(ids(&one)[0], ids(&capital)[0]);
        let mut window = one.clone();
        window.warmup_days = 10;
        assert_ne!(ids(&one)[0], ids(&window)[0]);
        assert_eq!(ids(&one)[0].len(), 64);
    }

    #[test]
    fn the_ids_hash_the_build_not_only_the_release() {
        // #53: two builds of one release (an engine change between version bumps) give
        // different trial and sweep ids, so a re-run on newer code is never deduped
        // against a stale result.
        let strategies = toy::registry();
        let mut spec: SweepSpec = toml::from_str(SPEC).unwrap();
        spec.grid
            .insert("target_distance".into(), vec![4.0.into(), 5.0.into()]);
        let at = |revision| {
            let p = Prepared::built(
                &spec,
                &strategies,
                Build {
                    version: "1.3.0",
                    revision,
                },
            )
            .unwrap();
            let ids: Vec<String> = p.cells.iter().map(|c| c.trial_id.clone()).collect();
            (p.sweep_id, ids)
        };
        let (sweep_a, trials_a) = at("4b97e8d0");
        let (sweep_b, trials_b) = at("24537c51");
        assert_ne!(sweep_a, sweep_b);
        for (a, b) in trials_a.iter().zip(&trials_b) {
            assert_ne!(a, b);
        }
        // The same build gives the same ids.
        assert_eq!(at("4b97e8d0"), (sweep_a, trials_a));
        // Records carry the build.
        let p = Prepared::new(&spec, &strategies).unwrap();
        let RegistryRecord::Sweep(header) = p.header() else {
            panic!("a header");
        };
        assert_eq!(header.code_revision.as_deref(), Some(CODE_REVISION));
        let trial = p.record(
            &p.cells[0],
            Utc::now(),
            TrialOutcome::Failed {
                stage: FailureStage::Run,
                error: String::new(),
            },
        );
        assert_eq!(trial.code_revision.as_deref(), Some(CODE_REVISION));
        assert_eq!(trial.code_version, CODE_VERSION);
        // A line written in M1, before the field, reads back with no build.
        let mut v = serde_json::to_value(RegistryRecord::Trial(Box::new(trial))).unwrap();
        v.as_object_mut().unwrap().remove("code_revision");
        let RegistryRecord::Trial(m1) = serde_json::from_value(v).unwrap() else {
            panic!("a trial");
        };
        assert_eq!(m1.code_revision, None);
    }
}
