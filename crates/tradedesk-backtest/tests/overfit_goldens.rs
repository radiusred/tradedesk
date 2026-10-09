//! A regression golden for the deflated Sharpe and PBO: the fixed returns matrices and
//! case inputs in `tests/fixtures/overfit_golden.json`, and every expected value computed
//! from them by this crate's own `metrics::return_stats`, `sweep::dsr` and `sweep::cscv`.
//! [`regenerate_the_golden`] (ignored; run on purpose) rewrites the expected values; the
//! file's `provenance` names it, the command and the revision it ran at.
//!
//! Tolerances: PBO, the partition counts and the count of logits at or below zero are
//! exact; the per-trial moments 1e-10 relative; PSR, DSR, `SR₀` and every logit and
//! logit statistic 1e-9 absolute.
#![allow(clippy::float_cmp)]

use serde_json::{Map, Value, json};
use tradedesk_backtest::metrics::return_stats;
use tradedesk_backtest::sweep::cscv::{CscvConfig, LogitSummary, pbo_cscv};
use tradedesk_backtest::sweep::dsr::{
    deflated_sharpe_ratio, expected_max_sharpe, probabilistic_sharpe_ratio,
};
use tradedesk_backtest::sweep::json::parse_exact;

const PATH: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/tests/fixtures/overfit_golden.json"
);

/// The command that rewrites the fixture, recorded in its provenance.
const COMMAND: &str = "cargo test -p tradedesk-backtest --test overfit_goldens -- --ignored --exact regenerate_the_golden";

fn golden() -> Value {
    // Exact float parsing, so the matrices are bit for bit what was written.
    parse_exact(&std::fs::read_to_string(PATH).unwrap()).unwrap()
}

fn f(v: &Value) -> f64 {
    v.as_f64().unwrap_or_else(|| panic!("not a number: {v}"))
}

fn u(v: &Value) -> usize {
    usize::try_from(v.as_u64().unwrap()).unwrap()
}

fn abs_close(name: &str, got: f64, want: f64) {
    assert!((got - want).abs() <= 1e-9, "{name}: got {got}, want {want}");
}

fn rel_close(name: &str, got: f64, want: f64) {
    let tol = 1e-10 * want.abs().max(1e-300);
    assert!((got - want).abs() <= tol, "{name}: got {got}, want {want}");
}

fn matrix(g: &Value, name: &str) -> Vec<Vec<f64>> {
    g["matrices"][name]
        .as_array()
        .unwrap()
        .iter()
        .map(|col| col.as_array().unwrap().iter().map(f).collect())
        .collect()
}

#[test]
fn the_fixture_was_generated_by_this_crate() {
    let g = golden();
    let p = g["provenance"].as_object().unwrap();
    let keys: Vec<&str> = p.keys().map(String::as_str).collect();
    assert_eq!(keys, ["code_revision", "command", "generator", "inputs"]);
    assert_eq!(p["command"], COMMAND);
    assert!(
        p["generator"]
            .as_str()
            .unwrap()
            .contains("overfit_goldens.rs::regenerate_the_golden")
    );
    assert!(!p["code_revision"].as_str().unwrap().is_empty());
}

#[test]
fn deflated_sharpe_matches_the_golden_through_the_crates_own_moments() {
    let g = golden();
    for name in ["noise", "skill"] {
        let case = &g["dsr"][name];
        let columns = matrix(&g, name);
        let stats: Vec<_> = columns.iter().map(|c| return_stats(c, 0.0)).collect();
        // σ_SR over the defined Sharpes, ddof 1 — as the sweep report does it.
        let defined: Vec<f64> = stats.iter().filter_map(|s| s.sharpe_daily).collect();
        #[allow(clippy::cast_precision_loss)]
        let n = defined.len() as f64;
        let mean = defined.iter().sum::<f64>() / n;
        let sigma = (defined.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / (n - 1.0)).sqrt();
        rel_close("sharpe_std", sigma, f(&case["sharpe_std"]));
        let n_trials = case["n_trials"].as_u64().unwrap();
        assert_eq!(n_trials, columns.len() as u64);
        abs_close(
            "expected_max_sharpe",
            expected_max_sharpe(n_trials, sigma),
            f(&case["expected_max_sharpe"]),
        );
        for (j, (s, want)) in stats
            .iter()
            .zip(case["trials"].as_array().unwrap())
            .enumerate()
        {
            let label = format!("{name}[{j}]");
            assert_eq!(s.observations, u(&want["n_obs"]), "{label}");
            if want["sharpe"].is_null() {
                assert_eq!(s.sharpe_daily, None, "{label}: no variance");
                assert!(want["dsr"].is_null());
                continue;
            }
            let (sr, skew, kurt) = (
                s.sharpe_daily.unwrap(),
                s.skewness.unwrap(),
                s.kurtosis.unwrap(),
            );
            rel_close(&format!("{label} sharpe"), sr, f(&want["sharpe"]));
            rel_close(&format!("{label} skew"), skew, f(&want["skew"]));
            rel_close(&format!("{label} kurtosis"), kurt, f(&want["kurtosis"]));
            abs_close(
                &format!("{label} psr0"),
                probabilistic_sharpe_ratio(sr, 0.0, s.observations, skew, kurt).unwrap(),
                f(&want["psr0"]),
            );
            abs_close(
                &format!("{label} dsr"),
                deflated_sharpe_ratio(sr, s.observations, n_trials, sigma, skew, kurt).unwrap(),
                f(&want["dsr"]),
            );
        }
    }
}

#[test]
fn expected_max_and_psr_match_on_their_own() {
    let g = golden();
    for c in g["expected_max_sharpe"].as_array().unwrap() {
        abs_close(
            &format!("E[max] N={} σ={}", c["n_trials"], c["sharpe_std"]),
            expected_max_sharpe(c["n_trials"].as_u64().unwrap(), f(&c["sharpe_std"])),
            f(&c["value"]),
        );
    }
    for c in g["psr"].as_array().unwrap() {
        abs_close(
            &format!("psr {c}"),
            probabilistic_sharpe_ratio(
                f(&c["sharpe"]),
                f(&c["benchmark"]),
                u(&c["n_obs"]),
                f(&c["skew"]),
                f(&c["kurtosis"]),
            )
            .unwrap(),
            f(&c["value"]),
        );
    }
}

#[test]
fn pbo_matches_the_golden_exactly() {
    let g = golden();
    for case in g["pbo"].as_array().unwrap() {
        let name = case["name"].as_str().unwrap();
        let columns = matrix(&g, case["matrix"].as_str().unwrap());
        let config = CscvConfig {
            blocks: u(&case["blocks"]),
            label_horizon: u(&case["label_horizon"]),
            embargo: u(&case["embargo"]),
        };
        let r = pbo_cscv(&columns, config).unwrap();
        assert_eq!(r.combinations, u(&case["combinations"]), "{name}");
        assert_eq!(r.pbo, Some(f(&case["pbo"])), "{name}: PBO is exact");
        let le_zero = r.logits.iter().filter(|&&l| l <= 0.0).count();
        assert_eq!(le_zero, u(&case["logits_le_zero"]), "{name}");
        let want: Vec<f64> = case["logits"].as_array().unwrap().iter().map(f).collect();
        if case["logits_complete"].as_bool().unwrap() {
            assert_eq!(r.logits.len(), want.len(), "{name}");
        }
        for (i, (got, want)) in r.logits.iter().zip(&want).enumerate() {
            abs_close(&format!("{name} logit {i}"), *got, *want);
        }
        let s = LogitSummary::of(&r.logits).unwrap();
        let w = &case["summary"];
        assert_eq!(s.count, u(&w["count"]), "{name}");
        for (field, got) in [
            ("mean", s.mean),
            ("std", s.std.unwrap()),
            ("min", s.min),
            ("q25", s.q25),
            ("median", s.median),
            ("q75", s.q75),
            ("max", s.max),
        ] {
            abs_close(&format!("{name} {field}"), got, f(&w[field]));
        }
    }
}

/// The deflated-Sharpe case for one matrix: `σ_SR` over the defined daily Sharpes (ddof 1,
/// as the sweep report computes it), `E[max SR]`, and each trial's moments, PSR(0) and
/// DSR (`null` without variance).
fn dsr_case(columns: &[Vec<f64>]) -> Value {
    let stats: Vec<_> = columns.iter().map(|c| return_stats(c, 0.0)).collect();
    let defined: Vec<f64> = stats.iter().filter_map(|s| s.sharpe_daily).collect();
    #[allow(clippy::cast_precision_loss)]
    let n = defined.len() as f64;
    let mean = defined.iter().sum::<f64>() / n;
    let sigma = (defined.iter().map(|x| (x - mean).powi(2)).sum::<f64>() / (n - 1.0)).sqrt();
    let n_trials = columns.len() as u64;
    let trials: Vec<Value> = stats
        .iter()
        .map(|s| match (s.sharpe_daily, s.skewness, s.kurtosis) {
            (Some(sr), Some(skew), Some(kurt)) => json!({
                "sharpe": sr,
                "n_obs": s.observations,
                "skew": skew,
                "kurtosis": kurt,
                "psr0": probabilistic_sharpe_ratio(sr, 0.0, s.observations, skew, kurt),
                "dsr": deflated_sharpe_ratio(sr, s.observations, n_trials, sigma, skew, kurt),
            }),
            _ => json!({
                "sharpe": null,
                "n_obs": s.observations,
                "skew": null,
                "kurtosis": null,
                "psr0": null,
                "dsr": null,
            }),
        })
        .collect();
    json!({
        "n_trials": n_trials,
        "sharpe_std": sigma,
        "expected_max_sharpe": expected_max_sharpe(n_trials, sigma),
        "trials": trials,
    })
}

/// Rewrites `tests/fixtures/overfit_golden.json` from this crate's implementation. The
/// inputs stay as they are: the two returns matrices, and the inputs of every
/// `expected_max_sharpe`, `psr` and `pbo` case (an incomplete `logits` list keeps its
/// length). Every expected value is recomputed. Run it on purpose, with [`COMMAND`].
#[test]
#[ignore = "rewrites the fixture; run on purpose"]
fn regenerate_the_golden() {
    let g = golden();
    let mut dsr = Map::new();
    for name in ["noise", "skill"] {
        dsr.insert(name.to_owned(), dsr_case(&matrix(&g, name)));
    }
    let expected_max: Vec<Value> = g["expected_max_sharpe"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| {
            let mut c = c.clone();
            c["value"] = json!(expected_max_sharpe(
                c["n_trials"].as_u64().unwrap(),
                f(&c["sharpe_std"])
            ));
            c
        })
        .collect();
    let psr: Vec<Value> = g["psr"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| {
            let mut c = c.clone();
            c["value"] = json!(probabilistic_sharpe_ratio(
                f(&c["sharpe"]),
                f(&c["benchmark"]),
                u(&c["n_obs"]),
                f(&c["skew"]),
                f(&c["kurtosis"]),
            ));
            c
        })
        .collect();
    let pbo: Vec<Value> = g["pbo"]
        .as_array()
        .unwrap()
        .iter()
        .map(|case| {
            let columns = matrix(&g, case["matrix"].as_str().unwrap());
            let config = CscvConfig {
                blocks: u(&case["blocks"]),
                label_horizon: u(&case["label_horizon"]),
                embargo: u(&case["embargo"]),
            };
            let r = pbo_cscv(&columns, config).unwrap();
            let complete = case["logits_complete"].as_bool().unwrap();
            let kept = if complete {
                r.logits.len()
            } else {
                case["logits"].as_array().unwrap().len()
            };
            let s = LogitSummary::of(&r.logits).unwrap();
            let mut case = case.clone();
            case["pbo"] = json!(r.pbo.unwrap());
            case["combinations"] = json!(r.combinations);
            case["logits_le_zero"] = json!(r.logits.iter().filter(|&&l| l <= 0.0).count());
            case["summary"] = json!({
                "count": s.count,
                "mean": s.mean,
                "std": s.std.unwrap(),
                "min": s.min,
                "q25": s.q25,
                "median": s.median,
                "q75": s.q75,
                "max": s.max,
            });
            case["logits"] = json!(r.logits[..kept]);
            case
        })
        .collect();
    let out = json!({
        "provenance": {
            "generator": "tests/overfit_goldens.rs::regenerate_the_golden: this crate's \
                          metrics::return_stats, sweep::dsr and sweep::cscv",
            "command": COMMAND,
            "code_revision": tradedesk_data::CODE_REVISION,
            "inputs": "the noise and skill returns matrices and the inputs of every \
                       expected_max_sharpe, psr and pbo case are fixed data in this file; \
                       every other number is computed from them by the generator",
        },
        "matrices": g["matrices"].clone(),
        "dsr": dsr,
        "expected_max_sharpe": expected_max,
        "psr": psr,
        "pbo": pbo,
    });
    // One-space indent, as the file has always been laid out.
    let mut text = Vec::new();
    let formatter = serde_json::ser::PrettyFormatter::with_indent(b" ");
    serde::Serialize::serialize(
        &out,
        &mut serde_json::Serializer::with_formatter(&mut text, formatter),
    )
    .unwrap();
    text.push(b'\n');
    std::fs::write(PATH, text).unwrap();
}
