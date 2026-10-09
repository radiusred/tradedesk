//! The deflated Sharpe and PBO against the Python reference implementation (`dsr.py`
//! and `cscv.py`), on the seeded returns matrices in `tests/fixtures/overfit_golden.json`
//! (written by a Python generator, not carried here; the file's `provenance` records the
//! command, the reference's commit and the SHA-256 of both modules).
//!
//! Tolerances: PBO, the partition counts and the count of logits at or below zero are
//! exact; the per-trial moments 1e-10 relative; PSR, DSR, `SR₀` and every logit and
//! logit statistic 1e-9 absolute.
#![allow(clippy::float_cmp)]

use serde_json::Value;
use tradedesk_backtest::metrics::return_stats;
use tradedesk_backtest::sweep::cscv::{CscvConfig, LogitSummary, pbo_cscv};
use tradedesk_backtest::sweep::dsr::{
    deflated_sharpe_ratio, expected_max_sharpe, probabilistic_sharpe_ratio,
};
use tradedesk_backtest::sweep::json::parse_exact;

fn golden() -> Value {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/tests/fixtures/overfit_golden.json"
    );
    // Exact float parsing, so the matrices are bit for bit what numpy wrote.
    parse_exact(&std::fs::read_to_string(path).unwrap()).unwrap()
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
fn the_fixture_names_its_reference() {
    let g = golden();
    let p = &g["provenance"];
    assert_eq!(p["ig_trader_commit"].as_str().unwrap().len(), 40);
    assert_eq!(p["dsr_py_sha256"].as_str().unwrap().len(), 64);
    assert_eq!(p["cscv_py_sha256"].as_str().unwrap().len(), 64);
}

#[test]
fn deflated_sharpe_matches_dsr_py_through_the_crates_own_moments() {
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
fn pbo_matches_cscv_py_exactly() {
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
