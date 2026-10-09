//! The command line end to end, over a synthetic Dukascopy cache written under
//! `CARGO_TARGET_TMPDIR`: a sweep, a second sweep into the same registry, the report and
//! the walk-forward as text and JSON, and every documented exit code. stdout must carry
//! only whole result lines; diagnostics go to stderr.
//!
//! The shipped `tradedesk-backtest` binary registers no strategy, so the sweeps here run
//! in-process through `cli::run` with the test strategy (`toy`) registered, as a strategy
//! crate's own binary would. `report`, `walkforward`, usage errors, SIGINT and the empty
//! registry's refusal run the built binary.
#![cfg(feature = "cli")]

mod synthetic;
mod toy;

use std::ffi::OsStr;
use std::fmt::Write as _;
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::sync::atomic::AtomicBool;

use assert_cmd::Command;
use chrono::{Duration, NaiveDate, TimeZone, Utc};
use serde_json::Value;
use synthetic::{Day, breakout_path, choppy_days, first_day};
use tradedesk_backtest::cli;
use tradedesk_backtest::sweep::{RegistryRecord, read_registry};
use tradedesk_data::Side;
use tradedesk_data::dukascopy::day_csv_zst;

/// A grid of the test strategy over the synthetic gold path: two target cells, with
/// `extra` appended to the `[grid]` table.
fn spec(extra: &str) -> String {
    format!(
        r#"
name = "cli synthetic threshold"
strategy = "threshold_cross"
base = "default"
instruments = ["XAUUSD"]
first_day = "2024-03-01"
last_day = "2024-04-30"
warmup_days = 60
venue = "ig_spread_bet"
exit_evaluation = {{ mode = "intrabar", both_hit = "stop_first" }}
account_fx = {{ account_currency = "GBP", rates = {{ USD = 0.8 }} }}
sizing = {{ kind = "stake_per_point", stake = 0.01 }}
end_of_run = "close"
starting_capital = 25000.0

[grid]
enter_above = [206000.0]
exit_below = [204000.0]
target_distance = [3000.0, 8000.0]
{extra}
"#
    )
}

/// The breakout path, then nine weeks of the choppy walk (as `sweep_synthetic`).
fn days() -> Vec<Day> {
    breakout_path(&choppy_days(63))
}

/// Write `days` as a Dukascopy cache: four minutes a day at 10:00Z walking
/// open → high → low → close, the same on both sides.
fn write_cache(root: &Path, days: &[Day]) {
    for (i, &(o, h, l, c)) in days.iter().enumerate() {
        let date = first_day() + Duration::days(i64::try_from(i).unwrap());
        let ten = Utc.from_utc_datetime(&date.and_hms_opt(10, 0, 0).unwrap());
        let mut body = String::from("timestamp,open,high,low,close,volume\n");
        for (m, price) in [o, h, l, c].into_iter().enumerate() {
            let ts = ten + Duration::minutes(i64::try_from(m).unwrap());
            writeln!(
                body,
                "{},{price},{price},{price},{price},1",
                ts.format("%Y-%m-%d %H:%M:%S%:z")
            )
            .unwrap();
        }
        for side in [Side::Bid, Side::Ask] {
            let path = day_csv_zst(root, "XAUUSD", date, side);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            let file = std::fs::File::create(&path).unwrap();
            let mut enc = zstd::stream::write::Encoder::new(file, 3).unwrap();
            enc.write_all(body.as_bytes()).unwrap();
            enc.finish().unwrap();
        }
    }
}

/// What an in-process run returned: its exit code and its stdout.
struct Run {
    code: u8,
    stdout: Vec<u8>,
}

/// A fresh directory for one test, with the cache written once into it.
struct Scratch {
    dir: PathBuf,
}

impl Scratch {
    fn new(name: &str) -> Self {
        let dir = Path::new(env!("CARGO_TARGET_TMPDIR"))
            .join("cli")
            .join(name);
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        write_cache(&dir.join("cache"), &days());
        Self { dir }
    }

    fn file(&self, name: &str, text: &str) -> PathBuf {
        let path = self.dir.join(name);
        std::fs::write(&path, text).unwrap();
        path
    }

    fn path(&self, name: &str) -> PathBuf {
        self.dir.join(name)
    }

    fn sweep(&self, spec: &Path, out: &Path) -> Run {
        sweep_in_process(spec, out, &self.dir.join("cache"), false)
    }
}

/// `sweep` in-process, with the test strategy registered, over `cache`.
fn sweep_in_process(spec: &Path, out: &Path, cache: &Path, interrupted: bool) -> Run {
    let mut stdout = Vec::new();
    let code = cli::run(
        [
            OsStr::new("tradedesk-backtest"),
            OsStr::new("sweep"),
            spec.as_os_str(),
            OsStr::new("--out"),
            out.as_os_str(),
            OsStr::new("--cache-root"),
            cache.as_os_str(),
        ],
        &toy::registry(),
        &mut stdout,
        &AtomicBool::new(interrupted),
    );
    Run { code, stdout }
}

/// The shipped binary.
fn bin() -> Command {
    let mut cmd = Command::cargo_bin("tradedesk-backtest").unwrap();
    cmd.env_remove("TRADEDESK_CACHE_ROOT")
        .env_remove("RUST_LOG");
    cmd
}

/// stdout as JSON lines: every line whole, every line an object with a `kind`.
fn json_lines(stdout: &[u8]) -> Vec<Value> {
    let text = std::str::from_utf8(stdout).unwrap();
    assert!(
        text.is_empty() || text.ends_with('\n'),
        "a partial line: {text:?}"
    );
    text.lines()
        .map(|l| serde_json::from_str::<Value>(l).unwrap_or_else(|e| panic!("{e}: {l}")))
        .collect()
}

fn kinds(lines: &[Value]) -> Vec<&str> {
    lines.iter().map(|l| l["kind"].as_str().unwrap()).collect()
}

/// A sweep of [`spec`] into `registry.jsonl`, which must succeed; returns its sweep id.
fn swept(s: &Scratch) -> (PathBuf, String) {
    let spec = s.file("spec.toml", &spec(""));
    let out = s.path("registry.jsonl");
    let run = s.sweep(&spec, &out);
    assert_eq!(run.code, 0);
    let sweep_id = json_lines(&run.stdout)[0]["sweep_id"]
        .as_str()
        .unwrap()
        .to_owned();
    (out, sweep_id)
}

#[test]
fn a_sweep_streams_json_lines_and_writes_the_registry() {
    let s = Scratch::new("sweep");
    let spec = s.file("spec.toml", &spec(""));
    let out = s.path("registry.jsonl");
    let run = s.sweep(&spec, &out);
    assert_eq!(run.code, 0);
    let lines = json_lines(&run.stdout);
    assert_eq!(kinds(&lines), vec!["sweep", "trial", "trial", "summary"]);
    let summary = &lines[3];
    assert_eq!(
        (&summary["cells"], &summary["ok"], &summary["failed"]),
        (&Value::from(2), &Value::from(2), &Value::from(0))
    );
    for trial in &lines[1..3] {
        assert_eq!(trial["status"], "ok");
        assert_eq!(trial["sweep_id"], summary["sweep_id"]);
        assert!(trial["entries"].as_u64().unwrap() > 1, "{trial}");
    }
    // The registry holds the header and both trials, with the ids stdout reported.
    let records = read_registry(&out).unwrap();
    assert_eq!(records.len(), 3);
    let RegistryRecord::Trial(t) = &records[1] else {
        panic!("a trial");
    };
    assert_eq!(lines[1]["trial_id"], t.trial_id.as_str());
    assert_eq!(t.strategy, toy::NAME);
    assert_eq!(
        t.code_revision.as_deref(),
        Some(tradedesk_data::CODE_REVISION)
    );

    // A second sweep appends to the same file.
    let again = s.sweep(&spec, &out);
    assert_eq!(again.code, 0);
    assert_eq!(read_registry(&out).unwrap().len(), 6);
}

#[test]
fn the_shipped_binary_registers_no_strategy() {
    let s = Scratch::new("empty-registry");
    let spec = s.file("spec.toml", &spec(""));
    let out = s.path("registry.jsonl");
    let run = bin()
        .arg("sweep")
        .arg(&spec)
        .arg("--out")
        .arg(&out)
        .arg("--cache-root")
        .arg(s.path("cache"))
        .output()
        .unwrap();
    assert_eq!(run.status.code(), Some(1));
    assert!(run.stdout.is_empty());
    let stderr = String::from_utf8(run.stderr).unwrap();
    assert!(stderr.contains("sweep spec refused"), "{stderr}");
    assert!(
        stderr.contains("unknown strategy") && stderr.contains("registered: []"),
        "{stderr}"
    );
    assert!(!out.exists(), "no registry is created for a refused spec");
}

#[test]
fn the_report_is_text_or_one_json_object() {
    let s = Scratch::new("report");
    let (out, sweep_id) = swept(&s);

    let text = bin().arg("report").arg(&out).output().unwrap();
    assert_eq!(
        text.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&text.stderr)
    );
    let body = String::from_utf8(text.stdout).unwrap();
    assert!(body.starts_with(&format!("sweep {sweep_id}\n")), "{body}");
    for needle in [
        "trials: 2 recorded, 2 completed, 0 failed, 0 ruined",
        "deflated Sharpe: N = 2 (0 prior)",
        "best trial: #",
        "partitions (S = 16, 2 trials",
    ] {
        assert!(body.contains(needle), "{needle:?} in {body}");
    }

    let json = bin()
        .args(["report", "--format", "json", "--prior-trials", "10"])
        .arg(&out)
        .output()
        .unwrap();
    assert_eq!(json.status.code(), Some(0));
    let lines = json_lines(&json.stdout);
    assert_eq!(lines.len(), 1);
    assert_eq!(lines[0]["sweep_id"], sweep_id.as_str());
    assert_eq!(lines[0]["dsr"]["trials"], 12);
    assert_eq!(lines[0]["per_trial"].as_array().unwrap().len(), 2);
}

#[test]
fn the_walkforward_is_text_or_one_json_object() {
    let s = Scratch::new("walkforward");
    let (out, sweep_id) = swept(&s);
    let months = [
        "--train-months",
        "1",
        "--test-months",
        "1",
        "--step-months",
        "1",
    ];

    // One fold over the two-month window: train March 2024, test April's 22 weekdays.
    let json = bin()
        .args(["walkforward", "--format", "json"])
        .args(months)
        .arg(&out)
        .output()
        .unwrap();
    assert_eq!(
        json.status.code(),
        Some(0),
        "{}",
        String::from_utf8_lossy(&json.stderr)
    );
    let lines = json_lines(&json.stdout);
    assert_eq!(lines.len(), 1);
    let r = &lines[0];
    assert_eq!(r["sweep_id"], sweep_id.as_str());
    assert_eq!(r["folds"].as_array().unwrap().len(), 1);
    let fold = &r["folds"][0];
    assert_eq!(fold["fold"]["test_first"], "2024-04-01");
    assert_eq!(fold["fold"]["test_last"], "2024-04-30");
    assert_eq!(fold["test_days"], 22);
    let stitched = &r["stitched"];
    assert_eq!(stitched["days"].as_array().unwrap().len(), 22);
    assert_eq!(stitched["returns"]["observations"], 22);
    // The sweep recorded each trial's trades, so the OOS trades are counted.
    assert_eq!(
        stitched["trades"]["stats"]["count"],
        fold["selected"]["test_trades"]
    );
    assert_eq!(r["cells"].as_array().unwrap().len(), 2);
    assert!(r["cells"][0]["trades"].is_u64());

    let text = bin()
        .arg("walkforward")
        .args(months)
        .arg(&out)
        .output()
        .unwrap();
    assert_eq!(text.status.code(), Some(0));
    let body = String::from_utf8(text.stdout).unwrap();
    assert!(body.starts_with(&format!("sweep {sweep_id}\n")), "{body}");
    for needle in [
        "walk-forward: 1 train / 1 test / 1 step months over 2024-03-01..=2024-04-30",
        "trials: 2 recorded, 2 completed, 0 failed, 0 ruined; costed true",
        "  1 2024-03-01..2024-03-31 2024-04-01..2024-04-30 #",
        "stitched OOS 2024-04-01..=2024-04-30: 22 days",
    ] {
        assert!(body.contains(needle), "{needle:?} in {body}");
    }

    // The registered 24-month train window does not fit two months: refused, exit 1.
    let refused = bin().arg("walkforward").arg(&out).output().unwrap();
    assert_eq!(refused.status.code(), Some(1));
    assert!(refused.stdout.is_empty());
    let stderr = String::from_utf8(refused.stderr).unwrap();
    assert!(stderr.contains("holds no fold"), "{stderr}");
}

#[test]
fn refused_input_exits_1_with_nothing_on_stdout() {
    let s = Scratch::new("refused");
    let out = s.path("registry.jsonl");
    // A key the spec does not know.
    let typo = s.file("typo.toml", &format!("{}\nbogus = 1\n", spec("")));
    // Parses, but names a venue the cost config does not have.
    let venue = s.file(
        "venue.toml",
        &spec("").replace("venue = \"ig_spread_bet\"", "venue = \"nowhere\""),
    );
    // Parses, but names a strategy that is not registered.
    let unknown = s.file(
        "unknown.toml",
        &spec("").replace("strategy = \"threshold_cross\"", "strategy = \"nope\""),
    );
    let missing = s.path("missing.toml");
    for bad in [&typo, &venue, &unknown, &missing] {
        let run = s.sweep(bad, &out);
        assert_eq!(run.code, 1, "{}", bad.display());
        assert!(
            run.stdout.is_empty(),
            "stdout stays clean for {}",
            bad.display()
        );
        assert!(!out.exists(), "no registry is created for a refused spec");
    }

    // A registry cut short by a crash: neither reported nor appended to.
    let good = s.file("spec.toml", &spec(""));
    assert_eq!(s.sweep(&good, &out).code, 0);
    let len = std::fs::metadata(&out).unwrap().len();
    std::fs::OpenOptions::new()
        .write(true)
        .open(&out)
        .unwrap()
        .set_len(len - 10)
        .unwrap();
    let report = bin().arg("report").arg(&out).output().unwrap();
    assert_eq!(report.status.code(), Some(1));
    assert!(report.stdout.is_empty());
    assert!(
        String::from_utf8(report.stderr)
            .unwrap()
            .contains("truncate the file")
    );
    let append = s.sweep(&good, &out);
    assert_eq!(append.code, 1);
    assert!(append.stdout.is_empty());
    assert_eq!(std::fs::metadata(&out).unwrap().len(), len - 10);

    // A report on a registry that does not exist.
    let none = bin()
        .arg("report")
        .arg(s.path("nope.jsonl"))
        .output()
        .unwrap();
    assert_eq!(none.status.code(), Some(1));
    assert!(none.stdout.is_empty());
}

#[test]
fn recorded_cell_failures_exit_3_and_runtime_failures_exit_4() {
    let s = Scratch::new("failures");
    // A stop distance of 0 is refused by the strategy: a recorded cell failure.
    let spec_with_failure = s.file("spec.toml", &spec("stop_distance = [5000.0, 0.0]"));
    let out = s.path("registry.jsonl");
    let run = s.sweep(&spec_with_failure, &out);
    assert_eq!(run.code, 3);
    let lines = json_lines(&run.stdout);
    assert_eq!(lines.last().unwrap()["failed"], 2);
    let failed: Vec<&Value> = lines.iter().filter(|l| l["status"] == "failed").collect();
    assert_eq!(failed.len(), 2);
    assert_eq!(failed[0]["stage"], "config");

    // No data under the cache root: the load fails at run time.
    let empty = s.path("empty-cache");
    std::fs::create_dir_all(&empty).unwrap();
    let good = s.file("good.toml", &spec(""));
    let load = sweep_in_process(&good, &s.path("load.jsonl"), &empty, false);
    assert_eq!(load.code, 4);
    assert!(load.stdout.is_empty());
}

#[test]
fn an_interrupted_sweep_exits_130_and_records_nothing() {
    // The SIGINT flag is already set when the sweep starts: it loads nothing, writes no
    // record and no line, and exits 130.
    let s = Scratch::new("interrupted");
    let spec = s.file("spec.toml", &spec(""));
    let out = s.path("registry.jsonl");
    let run = sweep_in_process(&spec, &out, &s.path("cache"), true);
    assert_eq!(run.code, 130);
    assert!(json_lines(&run.stdout).is_empty());
    assert!(read_registry(&out).unwrap().is_empty());
}

/// SIGINT exits 130 with whole lines only, whatever the command was doing. The spec is
/// read from a FIFO, so the test knows the binary is past its handler install (it is
/// blocked reading the spec) when the signal is sent. The shipped binary then refuses
/// the spec (it registers no strategy), and SIGINT still decides the exit code.
#[cfg(unix)]
#[test]
fn sigint_exits_130_and_leaves_whole_lines_only() {
    let s = Scratch::new("sigint");
    let fifo = s.path("spec.fifo");
    let made = std::process::Command::new("mkfifo")
        .arg(&fifo)
        .status()
        .unwrap();
    assert!(made.success());
    let out = s.path("registry.jsonl");
    let child = std::process::Command::new(assert_cmd::cargo::cargo_bin("tradedesk-backtest"))
        .arg("sweep")
        .arg(&fifo)
        .arg("--out")
        .arg(&out)
        .arg("--cache-root")
        .arg(s.path("cache"))
        .env_remove("RUST_LOG")
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    // Opening the FIFO for writing waits until the binary opens it to read the spec.
    let mut writer = std::fs::OpenOptions::new().write(true).open(&fifo).unwrap();
    let killed = std::process::Command::new("kill")
        .args(["-INT", &child.id().to_string()])
        .status()
        .unwrap();
    assert!(killed.success());
    std::thread::sleep(std::time::Duration::from_millis(200));
    writer.write_all(spec("").as_bytes()).unwrap();
    drop(writer);
    let run = child.wait_with_output().unwrap();
    let stderr = String::from_utf8(run.stderr).unwrap();
    assert_eq!(run.status.code(), Some(130), "{stderr}");
    assert!(stderr.contains("SIGINT received"), "{stderr}");
    assert!(json_lines(&run.stdout).is_empty());
    assert!(!out.exists());
}

#[test]
fn a_usage_error_exits_2() {
    let run = bin().args(["sweep", "spec.toml"]).output().unwrap();
    assert_eq!(run.status.code(), Some(2));
    assert!(run.stdout.is_empty());
    let unknown = bin().arg("frobnicate").output().unwrap();
    assert_eq!(unknown.status.code(), Some(2));
    // The same in-process.
    let mut stdout = Vec::new();
    let code = cli::run(
        ["tradedesk-backtest", "frobnicate"],
        &toy::registry(),
        &mut stdout,
        &AtomicBool::new(false),
    );
    assert_eq!(code, 2);
    assert!(stdout.is_empty());
}

#[test]
fn the_window_used_here_has_warm_up_and_a_trading_month() {
    // Keeps the spec honest about the synthetic data it runs over.
    let last = first_day() + Duration::days(i64::try_from(days().len()).unwrap() - 1);
    assert!(last >= NaiveDate::from_ymd_opt(2024, 4, 30).unwrap());
}
