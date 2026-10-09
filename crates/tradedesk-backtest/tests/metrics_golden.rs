//! Sharpe, the other return statistics and the maximum drawdowns against pandas/numpy on
//! fixed equity series. The expected values in `tests/fixtures/metrics_golden.json` are
//! pandas/numpy's (versions in its `provenance`), and the repository's Python suite
//! recomputes them on every run (`tests/test_rust_crate_goldens.py`).

use std::path::PathBuf;

use chrono::NaiveDate;
use serde_json::Value;
use tradedesk_backtest::metrics::{
    Drawdown, PERIODS_PER_YEAR, STD_DDOF, max_drawdown, return_stats,
};

const TOLERANCE: f64 = 1e-10;

fn golden() -> Value {
    let path = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/metrics_golden.json");
    serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap()
}

fn day(v: &Value) -> NaiveDate {
    NaiveDate::parse_from_str(v.as_str().unwrap(), "%Y-%m-%d").unwrap()
}

fn assert_close(name: &str, actual: Option<f64>, expected: &Value) {
    let expected = expected.as_f64().unwrap();
    let actual = actual.unwrap_or_else(|| panic!("{name}: undefined, expected {expected}"));
    let scale = expected.abs().max(1e-300);
    assert!(
        (actual - expected).abs() / scale < TOLERANCE,
        "{name}: {actual} vs pandas {expected}"
    );
}

fn assert_episode(name: &str, actual: Option<&Drawdown>, expected: &Value) {
    let a = actual.unwrap_or_else(|| panic!("{name}: no drawdown"));
    assert_close(
        &format!("{name}.amount"),
        Some(a.amount),
        &expected["amount"],
    );
    assert_close(
        &format!("{name}.fraction"),
        Some(a.fraction),
        &expected["fraction"],
    );
    assert_close(
        &format!("{name}.peak_equity"),
        Some(a.peak_equity),
        &expected["peak_equity"],
    );
    assert_close(
        &format!("{name}.trough_equity"),
        Some(a.trough_equity),
        &expected["trough_equity"],
    );
    assert_eq!(a.peak, day(&expected["peak"]), "{name}.peak");
    assert_eq!(a.trough, day(&expected["trough"]), "{name}.trough");
    assert_eq!(
        a.recovery,
        expected["recovery"]
            .as_str()
            .map(|_| day(&expected["recovery"])),
        "{name}.recovery"
    );
}

#[test]
fn return_statistics_and_drawdowns_match_pandas() {
    let g = golden();
    let p = &g["provenance"];
    assert_eq!(p["pandas"], "3.0.1");
    assert_eq!(p["numpy"], "2.4.2");
    assert_eq!(p["periods_per_year"].as_f64(), Some(PERIODS_PER_YEAR));
    assert_eq!(p["std_ddof"].as_u64(), Some(u64::from(STD_DDOF)));

    let cases = g["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 3);
    for c in cases {
        let name = c["name"].as_str().unwrap();
        let capital = c["starting_capital"].as_f64().unwrap();
        let equity: Vec<f64> = c["equity"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_f64().unwrap())
            .collect();
        let dates: Vec<NaiveDate> = c["dates"].as_array().unwrap().iter().map(day).collect();
        let e = &c["expected"];

        // Simple returns exactly as the curve builds them: the first against the capital.
        let returns: Vec<f64> = std::iter::once(capital)
            .chain(equity.iter().copied())
            .collect::<Vec<_>>()
            .windows(2)
            .map(|w| w[1] / w[0] - 1.0)
            .collect();
        let s = return_stats(&returns, c["risk_free_rate"].as_f64().unwrap());
        assert_eq!(
            Some(s.observations as u64),
            e["observations"].as_u64(),
            "{name}"
        );
        for (field, value) in [
            ("mean_excess", s.mean_excess),
            ("std", s.std),
            ("sharpe_daily", s.sharpe_daily),
            ("sharpe", s.sharpe),
            ("volatility", s.volatility),
            ("sortino", s.sortino),
            ("skewness", s.skewness),
            ("kurtosis", s.kurtosis),
        ] {
            assert_close(&format!("{name}.{field}"), value, &e[field]);
        }

        let curve: Vec<(NaiveDate, f64)> = std::iter::once((day(&c["opening_date"]), capital))
            .chain(dates.into_iter().zip(equity))
            .collect();
        let dd = max_drawdown(&curve);
        assert_episode(
            &format!("{name}.by_amount"),
            dd.by_amount.as_ref(),
            &e["drawdown_by_amount"],
        );
        assert_episode(
            &format!("{name}.by_fraction"),
            dd.by_fraction.as_ref(),
            &e["drawdown_by_fraction"],
        );
    }
}
