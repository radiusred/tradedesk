//! Indicator goldens: every indicator against tradedesk's Python output (M1-R6).
//!
//! The fixtures under `tests/fixtures/indicators/` were produced by a Python generator
//! run against tradedesk's indicators (provenance and command in each file's header; the
//! generator itself stayed with the private checkout it ran in):
//! two real days of XAUUSD 15m, six months of USA500 daily, and two synthetic series
//! that reach the edge branches (RSI 0 and 100, ADX zero-TR seed, zero-volume VWAP
//! sessions). Each file carries its own input bars, so no market data is needed.
//!
//! Every output is compared within a `1e-9` relative tolerance, and `None` (not
//! ready) must match exactly, which pins each indicator's warm-up length.

mod support;

use std::io::Write as _;

use chrono::{DateTime, Utc};
use support::{Fixture, TOLERANCE, close_enough};
use tradedesk_backtest::Ohlcv;
use tradedesk_backtest::indicators::{
    Adx, Atr, BollingerBands, Ema, Indicator, Macd, Rsi, Sma, Vwap, VwapPrice, VwapSession,
};

const FIXTURES: [&str; 4] = [
    "indicators/xauusd_15m.csv",
    "indicators/usa500_1d.csv",
    "indicators/synthetic_a.csv",
    "indicators/synthetic_b.csv",
];

/// Rust outputs for one input bar, in the fixture's column order.
fn run(bars: &[(DateTime<Utc>, Ohlcv)]) -> Vec<Vec<(&'static str, Option<f64>)>> {
    let mut sma5 = Sma::new(5).unwrap();
    let mut sma20 = Sma::new(20).unwrap();
    let mut sma50 = Sma::new(50).unwrap();
    let mut ema12 = Ema::new(12).unwrap();
    let mut ema50 = Ema::new(50).unwrap();
    let mut atr = Atr::new(14).unwrap();
    let mut adx = Adx::new(14).unwrap();
    let mut bb = BollingerBands::new(20, 2.0).unwrap();
    let mut macd = Macd::new(12, 26, 9).unwrap();
    let mut rsi = Rsi::new(14).unwrap();
    let mut vwap_daily = Vwap::new(VwapPrice::Typical, VwapSession::UtcDay).unwrap();
    let mut vwap_h7 = Vwap::new(VwapPrice::Close, VwapSession::UtcHour(7)).unwrap();

    bars.iter()
        .map(|&(ts, bar)| {
            let c = bar.close;
            let a = adx.update(bar).unwrap();
            let b = bb.update(c).unwrap();
            let m = macd.update(c).unwrap();
            vec![
                ("sma_5", sma5.update(c).unwrap()),
                ("sma_20", sma20.update(c).unwrap()),
                ("sma_50", sma50.update(c).unwrap()),
                ("ema_12", ema12.update(c).unwrap()),
                ("ema_50", ema50.update(c).unwrap()),
                ("atr_14", atr.update(bar).unwrap()),
                ("adx_14", a.adx),
                ("plus_di_14", a.plus_di),
                ("minus_di_14", a.minus_di),
                ("bb_middle", b.map(|v| v.middle)),
                ("bb_upper", b.map(|v| v.upper)),
                ("bb_lower", b.map(|v| v.lower)),
                ("bb_std", b.map(|v| v.std)),
                ("macd", m.map(|v| v.macd)),
                ("macd_signal", m.map(|v| v.signal)),
                ("macd_histogram", m.map(|v| v.histogram)),
                ("rsi_14", rsi.update(c).unwrap()),
                ("vwap_typical_daily", vwap_daily.update((ts, bar)).unwrap()),
                ("vwap_close_h7", vwap_h7.update((ts, bar)).unwrap()),
            ]
        })
        .collect()
}

#[test]
fn every_indicator_matches_the_python_reference() {
    for name in FIXTURES {
        let fx = Fixture::read(name);
        let bars: Vec<(DateTime<Utc>, Ohlcv)> = fx
            .rows
            .iter()
            .map(|row| {
                (
                    fx.ts(row, "ts"),
                    Ohlcv {
                        open: fx.f64(row, "open"),
                        high: fx.f64(row, "high"),
                        low: fx.f64(row, "low"),
                        close: fx.f64(row, "close"),
                        tick_volume: fx.f64(row, "volume"),
                    },
                )
            })
            .collect();
        assert!(bars.len() > 100, "{name}: too few bars");

        let mut compared = 0usize;
        let mut max_rel = 0.0f64;
        for (i, (row, outputs)) in fx.rows.iter().zip(run(&bars)).enumerate() {
            for (column, actual) in outputs {
                let expected = fx.opt_f64(row, column);
                match (actual, expected) {
                    (None, None) => {}
                    (Some(a), Some(e)) => {
                        assert!(
                            close_enough(a, e, TOLERANCE),
                            "{name} row {i} {column}: rust {a} vs python {e}"
                        );
                        let r = (a - e).abs() / e.abs().max(1.0);
                        max_rel = max_rel.max(r);
                        compared += 1;
                    }
                    _ => panic!("{name} row {i} {column}: rust {actual:?} vs python {expected:?}"),
                }
            }
        }
        assert!(compared > 1000, "{name}: only {compared} values compared");
        // Mirrored arithmetic: the comparison is exact in practice. Keep the observed
        // worst case visible in the test log.
        let _ = writeln!(
            std::io::stderr(),
            "{name}: {compared} values, max relative diff {max_rel:e}"
        );
    }
}

#[test]
fn every_fixture_reaches_the_edge_branches_it_was_built_for() {
    let a = Fixture::read("indicators/synthetic_a.csv");
    assert!(
        a.rows.iter().any(|r| a.opt_f64(r, "rsi_14") == Some(0.0)),
        "synthetic_a seeds RSI with no gains"
    );
    let b = Fixture::read("indicators/synthetic_b.csv");
    assert!(
        b.rows.iter().any(|r| b.opt_f64(r, "rsi_14") == Some(100.0)),
        "synthetic_b seeds RSI with no losses"
    );
    assert!(
        b.rows
            .iter()
            .any(|r| b.opt_f64(r, "adx_14") == Some(0.0) && b.opt_f64(r, "plus_di_14") == Some(0.0)),
        "synthetic_b seeds ADX on zero true range"
    );
    assert_eq!(
        b.opt_f64(&b.rows[0], "vwap_typical_daily"),
        None,
        "synthetic_b opens a session with zero volume"
    );
}
