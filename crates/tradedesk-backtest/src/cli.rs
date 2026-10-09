//! The backtester's command line: run a parameter sweep into a trial registry, and
//! report the deflated Sharpe, the PBO and the walk-forward of a sweep in a registry.
//!
//! ```text
//! tradedesk-backtest sweep <SPEC> --out <REGISTRY> --cache-root <DIR>
//! tradedesk-backtest report <REGISTRY> [--sweep <ID>] [--format text|json]
//!                                  [--cscv-blocks <S>] [--prior-trials <N>]
//! tradedesk-backtest walkforward <REGISTRY> [--sweep <ID>] [--format text|json]
//!                                       [--train-months <M>] [--test-months <M>]
//!                                       [--step-months <M>]
//! ```
//!
//! **Strategies.** `sweep` runs the strategies in the [`StrategyRegistry`] it is given.
//! The `tradedesk-backtest` binary gives it an empty one: it reports on any registry, and
//! refuses every sweep spec, naming the strategies it knows (none). A strategy crate
//! builds its own binary on [`main`] with its strategies registered:
//!
//! ```no_run
//! use tradedesk_backtest::sweep::StrategyRegistry;
//!
//! fn main() -> std::process::ExitCode {
//!     let strategies = StrategyRegistry::new(); // register the crate's strategies here
//!     tradedesk_backtest::cli::main(&strategies)
//! }
//! ```
//!
//! The streams follow the miner's discipline:
//! - **stdout carries results only.** `sweep` writes one JSON line when the sweep starts,
//!   one per trial as it is recorded, and a summary line at the end. `report` and
//!   `walkforward` write text, or one JSON object with `--format json`. Every line is built
//!   whole before it is written and flushed after it, so a failure never leaves a partial
//!   line.
//! - **stderr carries diagnostics**, through `tracing` (`RUST_LOG`, default `info`).
//!
//! Exit codes ([`Exit`]): 0 ok; 1 input refused (spec, cost config or registry); 2 usage
//! (clap); 3 the sweep finished with recorded cell failures; 4 a runtime failure (data
//! load, registry or stdout write); 130 interrupted by SIGINT. The crate README's
//! "Command line" section is the reference.

use std::ffi::OsString;
use std::io::{IsTerminal as _, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use clap::{Parser, Subcommand, ValueEnum};
use serde_json::{Value, json};
use tracing_subscriber::EnvFilter;
use tradedesk_data::dukascopy::DukascopyReader;

use crate::sweep::cscv::CscvConfig;
use crate::sweep::report::PboOutcome;
use crate::sweep::walkforward::Stitched;
use crate::sweep::{
    JsonlRegistry, RegistryRecord, RegistrySink, ReportOptions, StrategyRegistry, SweepError,
    SweepReport, SweepSpec, TrialOutcome, WalkForwardOptions, WalkForwardReport, read_registry,
    run_sweep_cancellable, validate_spec,
};

/// The backtester's command line.
#[derive(Debug, Parser)]
#[command(name = "tradedesk-backtest", version, about)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Subcommand)]
enum Command {
    /// Run every cell of a sweep spec and append each trial to a registry.
    Sweep(SweepArgs),
    /// Report the deflated Sharpe and the PBO of one sweep in a registry.
    Report(ReportArgs),
    /// Report the rolling walk-forward of one sweep in a registry.
    Walkforward(WalkForwardArgs),
}

#[derive(Debug, clap::Args)]
struct SweepArgs {
    /// The sweep spec: TOML in the format of the crate README's "The spec".
    spec: PathBuf,
    /// The JSON Lines registry to append to; created if absent.
    #[arg(long)]
    out: PathBuf,
    /// Root of the Dukascopy cache (`<root>/<SYMBOL>/<YYYY>/<MM>/<DD>_<side>.csv.zst`).
    #[arg(long, env = "TRADEDESK_CACHE_ROOT")]
    cache_root: PathBuf,
}

#[derive(Debug, clap::Args)]
struct ReportArgs {
    /// The JSON Lines registry to read.
    registry: PathBuf,
    /// The sweep to report on; required when the registry holds several.
    #[arg(long)]
    sweep: Option<String>,
    /// Text for people, or one JSON object.
    #[arg(long, value_enum, default_value_t = Format::Text)]
    format: Format,
    /// CSCV blocks `S` (even, at least 2).
    #[arg(long, default_value_t = CscvConfig::default().blocks)]
    cscv_blocks: usize,
    /// Trials run before this sweep on the same question, added to the deflated
    /// Sharpe's `N`.
    #[arg(long, default_value_t = 0)]
    prior_trials: u64,
}

#[derive(Debug, clap::Args)]
struct WalkForwardArgs {
    /// The JSON Lines registry to read.
    registry: PathBuf,
    /// The sweep to report on; required when the registry holds several.
    #[arg(long)]
    sweep: Option<String>,
    /// Text for people, or one JSON object.
    #[arg(long, value_enum, default_value_t = Format::Text)]
    format: Format,
    /// Train window, calendar months.
    #[arg(long, default_value_t = WalkForwardOptions::default().train_months)]
    train_months: u32,
    /// Test window, calendar months.
    #[arg(long, default_value_t = WalkForwardOptions::default().test_months)]
    test_months: u32,
    /// Step between folds, calendar months (at least the test window).
    #[arg(long, default_value_t = WalkForwardOptions::default().step_months)]
    step_months: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
enum Format {
    Text,
    Json,
}

/// The process exit codes. A usage error exits with clap's 2 before any of these.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Exit {
    /// Everything ran; every cell completed.
    Ok = 0,
    /// The spec, the cost config or the registry was refused before anything ran.
    Input = 1,
    /// The sweep finished, and at least one cell is recorded as a failure.
    CellFailures = 3,
    /// Data could not be loaded, or the registry or stdout could not be written.
    Runtime = 4,
    /// SIGINT: no new cell started, and the registry holds whole lines only.
    Interrupted = 130,
}

impl Exit {
    /// The code to exit with: SIGINT wins over every outcome, as in `miner`.
    fn code(self, interrupted: bool) -> u8 {
        if interrupted {
            Self::Interrupted as u8
        } else {
            self as u8
        }
    }
}

/// The class of a sweep error.
fn sweep_exit(error: &SweepError) -> Exit {
    match error {
        SweepError::Spec(_) | SweepError::SpecFile(_) | SweepError::Costs(_) => Exit::Input,
        SweepError::Load(_) | SweepError::Registry(_) => Exit::Runtime,
        SweepError::Cancelled { .. } => Exit::Interrupted,
    }
}

/// The whole process: the stderr log subscriber, the SIGINT handler, then [`run`] over
/// the process arguments with `strategies`, writing results to stdout.
#[must_use]
pub fn main(strategies: &StrategyRegistry) -> ExitCode {
    // Diagnostics go to stderr from the first line on, colour only on a terminal.
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_ansi(std::io::stderr().is_terminal())
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();
    // The SIGINT handler goes in before the arguments are parsed, as in `miner`: a SIGINT
    // from then on only sets the flag, and the sweep stops starting cells.
    let interrupted = Arc::new(AtomicBool::new(false));
    {
        let flag = Arc::clone(&interrupted);
        if let Err(e) = ctrlc::set_handler(move || {
            flag.store(true, Ordering::SeqCst);
            tracing::warn!("SIGINT received: no new cell will start");
        }) {
            // Without the handler SIGINT keeps its default action (the process ends);
            // nothing else changes.
            tracing::warn!(error = %e, "could not install the SIGINT handler");
        }
    }

    ExitCode::from(run(
        std::env::args_os(),
        strategies,
        &mut std::io::stdout(),
        &interrupted,
    ))
}

/// Parse `args` (the program name first) and run the command, with `strategies` for
/// `sweep`, writing results to `stdout`. Returns the exit code ([`Exit`], or clap's on
/// a usage error, whose message clap prints itself). `interrupted` is the SIGINT flag:
/// once set, a sweep starts no new cell, and the exit code is 130 whatever happened.
pub fn run<I, T>(
    args: I,
    strategies: &StrategyRegistry,
    stdout: &mut dyn Write,
    interrupted: &AtomicBool,
) -> u8
where
    I: IntoIterator<Item = T>,
    T: Into<OsString> + Clone,
{
    let cli = match Cli::try_parse_from(args) {
        Ok(cli) => cli,
        Err(e) => {
            // `--help` and `--version` print to stdout and exit 0; a usage error prints
            // to stderr and exits 2.
            let _ = e.print();
            return u8::try_from(e.exit_code()).unwrap_or(2);
        }
    };
    let exit = match cli.command {
        Command::Sweep(args) => sweep(&args, strategies, stdout, interrupted),
        Command::Report(args) => report(&args, stdout),
        Command::Walkforward(args) => walkforward(&args, stdout),
    };
    exit.code(interrupted.load(Ordering::SeqCst))
}

/// Write `value` as one JSON line: built whole, then written and flushed.
fn write_json_line(out: &mut dyn Write, value: &Value) -> std::io::Result<()> {
    let mut line = serde_json::to_vec(value).map_err(std::io::Error::other)?;
    line.push(b'\n');
    out.write_all(&line)?;
    out.flush()
}

/// Writes each record to the registry, then its progress line to stdout.
struct CliSink<'a> {
    registry: JsonlRegistry<std::fs::File>,
    stdout: &'a mut dyn Write,
    path: PathBuf,
}

impl RegistrySink for CliSink<'_> {
    fn write(&mut self, record: &RegistryRecord) -> std::io::Result<()> {
        self.registry.write(record)?;
        write_json_line(self.stdout, &progress_line(record, &self.path))
    }
}

/// The stdout line for one registry record.
fn progress_line(record: &RegistryRecord, registry: &Path) -> Value {
    match record {
        RegistryRecord::Sweep(s) => json!({
            "kind": "sweep",
            "sweep_id": s.sweep_id,
            "cells": s.cells,
            "code_version": s.code_version,
            "code_revision": s.code_revision,
            "registry": registry.display().to_string(),
        }),
        RegistryRecord::Trial(t) => match &t.outcome {
            TrialOutcome::Ok {
                engine, metrics, ..
            } => json!({
                "kind": "trial",
                "sweep_id": t.sweep_id,
                "index": t.index,
                "trial_id": t.trial_id,
                "instrument": t.instrument,
                "params": t.params,
                "status": "ok",
                "sharpe": metrics.returns.sharpe,
                "total_return": metrics.total_return,
                "trades": metrics.trades.count,
                "entries": engine.entries,
            }),
            TrialOutcome::Failed { stage, error } => json!({
                "kind": "trial",
                "sweep_id": t.sweep_id,
                "index": t.index,
                "trial_id": t.trial_id,
                "instrument": t.instrument,
                "params": t.params,
                "status": "failed",
                "stage": stage,
                "error": error,
            }),
        },
    }
}

fn sweep(
    args: &SweepArgs,
    strategies: &StrategyRegistry,
    stdout: &mut dyn Write,
    interrupted: &AtomicBool,
) -> Exit {
    let spec = match SweepSpec::from_path(&args.spec).and_then(|spec| {
        validate_spec(&spec, strategies)?;
        Ok(spec)
    }) {
        Ok(spec) => spec,
        Err(e) => {
            tracing::error!(spec = %args.spec.display(), error = %e, "sweep spec refused");
            return sweep_exit(&e);
        }
    };
    let registry = match JsonlRegistry::append(&args.out) {
        Ok(r) => r,
        Err(e) => {
            tracing::error!(registry = %args.out.display(), error = %e, "registry refused");
            return Exit::Input;
        }
    };
    let mut sink = CliSink {
        registry,
        stdout,
        path: args.out.clone(),
    };
    let reader = DukascopyReader::new(&args.cache_root);
    match run_sweep_cancellable(&spec, strategies, &reader, &mut sink, interrupted) {
        Ok(summary) => {
            let line = json!({
                "kind": "summary",
                "sweep_id": summary.sweep_id,
                "cells": summary.cells,
                "ok": summary.ok,
                "failed": summary.failed,
                "registry": args.out.display().to_string(),
            });
            if let Err(e) = write_json_line(sink.stdout, &line) {
                tracing::error!(error = %e, "writing to stdout");
                return Exit::Runtime;
            }
            if summary.failed > 0 {
                tracing::warn!(failed = summary.failed, "cells recorded as failures");
                Exit::CellFailures
            } else {
                Exit::Ok
            }
        }
        Err(e @ SweepError::Cancelled { .. }) => {
            tracing::warn!(error = %e, "sweep interrupted");
            sweep_exit(&e)
        }
        Err(e) => {
            tracing::error!(error = %e, "sweep stopped");
            sweep_exit(&e)
        }
    }
}

fn report(args: &ReportArgs, stdout: &mut dyn Write) -> Exit {
    let records = match read_registry(&args.registry) {
        Ok(r) => r,
        Err(e) => {
            tracing::error!(registry = %args.registry.display(), error = %e, "registry refused");
            return Exit::Input;
        }
    };
    let options = ReportOptions {
        cscv: CscvConfig {
            blocks: args.cscv_blocks,
            ..CscvConfig::default()
        },
        prior_trials: args.prior_trials,
    };
    let report = match SweepReport::from_records(&records, args.sweep.as_deref(), options) {
        Ok(r) => r,
        Err(e) => {
            tracing::error!(error = %e, "no report");
            return Exit::Input;
        }
    };
    let written = match args.format {
        Format::Json => serde_json::to_value(&report)
            .map_err(std::io::Error::other)
            .and_then(|v| write_json_line(stdout, &v)),
        Format::Text => write_text(stdout, &render_text(&report)),
    };
    match written {
        Ok(()) => Exit::Ok,
        Err(e) => {
            tracing::error!(error = %e, "writing to stdout");
            Exit::Runtime
        }
    }
}

fn walkforward(args: &WalkForwardArgs, stdout: &mut dyn Write) -> Exit {
    let records = match read_registry(&args.registry) {
        Ok(r) => r,
        Err(e) => {
            tracing::error!(registry = %args.registry.display(), error = %e, "registry refused");
            return Exit::Input;
        }
    };
    let options = WalkForwardOptions {
        train_months: args.train_months,
        test_months: args.test_months,
        step_months: args.step_months,
    };
    let report = match WalkForwardReport::from_records(&records, args.sweep.as_deref(), options) {
        Ok(r) => r,
        Err(e) => {
            tracing::error!(error = %e, "no walk-forward");
            return Exit::Input;
        }
    };
    let written = match args.format {
        Format::Json => serde_json::to_value(&report)
            .map_err(std::io::Error::other)
            .and_then(|v| write_json_line(stdout, &v)),
        Format::Text => write_text(stdout, &render_walkforward(&report)),
    };
    match written {
        Ok(()) => Exit::Ok,
        Err(e) => {
            tracing::error!(error = %e, "writing to stdout");
            Exit::Runtime
        }
    }
}

/// The walk-forward for people: the folds, the stitched series and every cell.
fn render_walkforward(report: &WalkForwardReport) -> String {
    use std::fmt::Write as _;
    let mut out = String::new();
    let months = report.options;
    // `write!` to a String cannot fail.
    let _ = writeln!(out, "sweep {}", report.sweep_id);
    let _ = writeln!(
        out,
        "walk-forward: {} train / {} test / {} step months over {}..={}",
        months.train_months,
        months.test_months,
        months.step_months,
        report.first_day,
        report.last_day
    );
    let _ = writeln!(
        out,
        "trials: {} recorded, {} completed, {} failed, {} ruined; costed {}",
        report.trials, report.completed, report.failed, report.ruined, report.costed
    );
    render_folds(&mut out, report);
    let _ = writeln!(
        out,
        "mean Sharpe: IS {}, OOS {}",
        opt(report.mean_is_sharpe, 4),
        opt(report.mean_oos_sharpe, 4)
    );
    render_stitched(&mut out, report.stitched.as_ref());
    let _ = writeln!(
        out,
        "cells over the OOS days: index sharpe gross-pnl net-pnl trades params"
    );
    for cell in &report.cells {
        let _ = writeln!(
            out,
            "  {} {} {:.2} {:.2} {} {}",
            cell.index,
            opt(cell.sharpe, 4),
            cell.gross_pnl,
            cell.net_pnl,
            count(cell.trades),
            Value::from(serde_json::Map::from_iter(cell.params.clone()))
        );
    }
    out
}

/// `n`, or `-` when not recorded.
fn count(n: Option<usize>) -> String {
    n.map_or_else(|| "-".to_owned(), |n| n.to_string())
}

/// One line per fold: its windows and the cell it selected.
fn render_folds(out: &mut String, report: &WalkForwardReport) {
    use std::fmt::Write as _;
    let _ = writeln!(
        out,
        "folds: fold train test index IS-sharpe OOS-sharpe OOS-pnl OOS-trades params"
    );
    for result in &report.folds {
        let fold = result.fold;
        let window = format!(
            "{} {}..{} {}..{}",
            fold.fold, fold.train_first, fold.train_last, fold.test_first, fold.test_last
        );
        match &result.selected {
            Some(cell) => {
                let _ = writeln!(
                    out,
                    "  {window} #{} {} {} {:.2} {} {}",
                    cell.index,
                    opt(cell.train_sharpe, 4),
                    opt(cell.test_sharpe, 4),
                    cell.test_pnl,
                    count(cell.test_trades),
                    Value::from(serde_json::Map::from_iter(cell.params.clone()))
                );
            }
            None => {
                let _ = writeln!(out, "  {window} none (no completed trial without ruin)");
            }
        }
    }
}

/// The stitched out-of-sample series: Sharpe, £ P&L, drawdown and trades.
fn render_stitched(out: &mut String, stitched: Option<&Stitched>) {
    use std::fmt::Write as _;
    let Some(series) = stitched else {
        let _ = writeln!(out, "stitched OOS: none (a fold selected no trial)");
        return;
    };
    let _ = writeln!(
        out,
        "stitched OOS {}..={}: {} days, Sharpe {}, P&L {:.2}, final equity {:.2}",
        series.first_day,
        series.last_day,
        series.returns.observations,
        opt(series.returns.sharpe, 4),
        series.pnl,
        series.final_equity
    );
    let drawdown = series.max_drawdown.by_amount.as_ref().map_or_else(
        || "none".to_owned(),
        |dd| format!("{:.2} ({} to {})", dd.amount, dd.peak, dd.trough),
    );
    let _ = writeln!(out, "  max drawdown {drawdown}");
    match &series.trades {
        Some(trades) => {
            let _ = writeln!(
                out,
                "  OOS trades {}: gross {:.2}, costs {:.2}, net {:.2}, cost drag {}",
                trades.stats.count,
                trades.stats.gross_pnl,
                trades.stats.costs.total(),
                trades.stats.net_pnl,
                opt(trades.cost_drag, 4)
            );
        }
        None => {
            let _ = writeln!(out, "  OOS trades: not recorded");
        }
    }
}

/// The one plain-text write to stdout: the whole text in a single `write_all`.
fn write_text(out: &mut dyn Write, text: &str) -> std::io::Result<()> {
    out.write_all(text.as_bytes())?;
    out.flush()
}

/// `x` to `places` decimals, or `-` when undefined.
fn opt(x: Option<f64>, places: usize) -> String {
    x.map_or_else(|| "-".to_owned(), |v| format!("{v:.places$}"))
}

/// The report for people.
fn render_text(r: &SweepReport) -> String {
    use std::fmt::Write as _;
    let mut s = String::new();
    // `write!` to a String cannot fail.
    let _ = writeln!(s, "sweep {}", r.sweep_id);
    let _ = writeln!(
        s,
        "trials: {} recorded, {} completed, {} failed, {} ruined",
        r.trials, r.completed, r.failed, r.ruined
    );
    let _ = writeln!(
        s,
        "deflated Sharpe: N = {} ({} prior), sigma_SR daily = {}, SR0 daily = {:.6}",
        r.dsr.trials,
        r.dsr.prior_trials,
        opt(r.dsr.sharpe_std_daily, 6),
        r.dsr.benchmark_daily
    );
    match &r.best {
        Some(b) => {
            let _ = writeln!(
                s,
                "best trial: #{} {} {} (trial {})",
                b.trial.index,
                b.trial.instrument,
                Value::from(serde_json::Map::from_iter(b.trial.params.clone())),
                b.trial.trial_id
            );
            let _ = writeln!(
                s,
                "  Sharpe {:.4}, benchmark {:.4}, deflated {:.4}, DSR {:.4}, false discovery {:.4}",
                b.sharpe, b.benchmark, b.deflated_sharpe, b.dsr, b.false_discovery_probability
            );
        }
        None => {
            let _ = writeln!(s, "best trial: none with a defined Sharpe");
        }
    }
    match &r.pbo {
        PboOutcome::Computed(p) => {
            let _ = writeln!(
                s,
                "PBO: {:.4} over {} partitions (S = {}, {} trials x {} observations)",
                p.pbo, p.combinations, p.cscv.blocks, p.trials, p.observations
            );
            let _ = writeln!(
                s,
                "  logits: mean {:.4}, median {:.4}, min {:.4}, max {:.4}",
                p.logits.mean, p.logits.median, p.logits.min, p.logits.max
            );
        }
        PboOutcome::NotComputed { reason } => {
            let _ = writeln!(s, "PBO: not computed ({reason})");
        }
    }
    let _ = writeln!(
        s,
        "per trial (cell order): index instrument sharpe dsr observations params"
    );
    for t in &r.per_trial {
        let _ = writeln!(
            s,
            "  {} {} {} {} {} {}",
            t.index,
            t.instrument,
            opt(t.sharpe, 4),
            opt(t.dsr, 4),
            t.observations,
            Value::from(serde_json::Map::from_iter(t.params.clone()))
        );
    }
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exit_codes_are_the_documented_table_and_sigint_wins() {
        assert_eq!(Exit::Ok.code(false), 0);
        assert_eq!(Exit::Input.code(false), 1);
        assert_eq!(Exit::CellFailures.code(false), 3);
        assert_eq!(Exit::Runtime.code(false), 4);
        assert_eq!(Exit::Interrupted.code(false), 130);
        for exit in [Exit::Ok, Exit::Input, Exit::CellFailures, Exit::Runtime] {
            assert_eq!(exit.code(true), 130);
        }
        assert_eq!(sweep_exit(&SweepError::Spec(String::new())), Exit::Input);
        assert_eq!(
            sweep_exit(&SweepError::SpecFile(String::new())),
            Exit::Input
        );
        assert_eq!(sweep_exit(&SweepError::Load(String::new())), Exit::Runtime);
        assert_eq!(
            sweep_exit(&SweepError::Registry(std::io::Error::other("disk full"))),
            Exit::Runtime
        );
        assert_eq!(
            sweep_exit(&SweepError::Cancelled { written: 0 }),
            Exit::Interrupted
        );
    }

    #[test]
    fn the_cli_parses_the_documented_invocations() {
        let cli = Cli::try_parse_from([
            "tradedesk-backtest",
            "sweep",
            "spec.toml",
            "--out",
            "reg.jsonl",
            "--cache-root",
            "/data",
        ])
        .unwrap();
        assert!(matches!(cli.command, Command::Sweep(ref a) if a.out == Path::new("reg.jsonl")));
        let cli = Cli::try_parse_from([
            "tradedesk-backtest",
            "report",
            "reg.jsonl",
            "--format",
            "json",
        ])
        .unwrap();
        let Command::Report(a) = cli.command else {
            panic!("report");
        };
        assert_eq!(
            (a.format, a.cscv_blocks, a.prior_trials),
            (Format::Json, 16, 0)
        );
        assert!(Cli::try_parse_from(["tradedesk-backtest", "sweep", "spec.toml"]).is_err());
        let cli = Cli::try_parse_from(["tradedesk-backtest", "walkforward", "reg.jsonl"]).unwrap();
        let Command::Walkforward(a) = cli.command else {
            panic!("walkforward");
        };
        assert_eq!(
            (a.format, a.train_months, a.test_months, a.step_months),
            (Format::Text, 24, 6, 6)
        );
    }
}
