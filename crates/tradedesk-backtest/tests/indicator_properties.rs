//! Property tests on indicator invariants that hold for any valid input.

use proptest::prelude::*;
use tradedesk_backtest::Ohlcv;
use tradedesk_backtest::indicators::{Adx, Atr, BollingerBands, Ema, Indicator, Rsi, Sma};

/// Positive prices spanning the cache's raw scales (FX pips to gold cents).
fn closes() -> impl Strategy<Value = Vec<f64>> {
    proptest::collection::vec(0.01_f64..300_000.0, 1..200)
}

/// Valid bars: a random walk with high/low around open and close.
fn bars() -> impl Strategy<Value = Vec<Ohlcv>> {
    proptest::collection::vec((-0.05_f64..0.05, 0.0_f64..0.03, 0.0_f64..0.03), 1..200).prop_map(
        |steps| {
            let mut prev = 100.0_f64;
            steps
                .into_iter()
                .map(|(ret, up, down)| {
                    let close = prev * (1.0 + ret);
                    let bar = Ohlcv {
                        open: prev,
                        high: prev.max(close) * (1.0 + up),
                        low: prev.min(close) * (1.0 - down),
                        close,
                        tick_volume: 1.0,
                    };
                    prev = close;
                    bar
                })
                .collect()
        },
    )
}

proptest! {
    #[test]
    fn sma_of_a_constant_is_the_constant(c in 0.01_f64..300_000.0, period in 1_usize..60, n in 1_usize..120) {
        let mut sma = Sma::new(period).unwrap();
        for v in sma.batch(&vec![c; n]).unwrap().into_iter().flatten() {
            prop_assert!((v - c).abs() <= 1e-12 * c, "sma {v} vs constant {c}");
        }
    }

    #[test]
    fn ema_stays_within_the_range_of_its_inputs(xs in closes(), period in 1_usize..60) {
        let mut ema = Ema::new(period).unwrap();
        let (mut lo, mut hi) = (f64::INFINITY, f64::NEG_INFINITY);
        for &x in &xs {
            lo = lo.min(x);
            hi = hi.max(x);
            if let Some(v) = ema.update(x).unwrap() {
                let slack = 1e-12 * hi;
                prop_assert!(v >= lo - slack && v <= hi + slack, "ema {v} outside [{lo}, {hi}]");
            }
        }
    }

    #[test]
    fn rsi_is_within_zero_and_one_hundred(xs in closes(), period in 1_usize..30) {
        let mut rsi = Rsi::new(period).unwrap();
        for v in rsi.batch(&xs).unwrap().into_iter().flatten() {
            prop_assert!((0.0..=100.0).contains(&v), "rsi {v}");
        }
    }

    #[test]
    fn bollinger_bands_are_ordered(xs in closes(), period in 1_usize..40, k in 0.1_f64..4.0) {
        let mut bb = BollingerBands::new(period, k).unwrap();
        for v in bb.batch(&xs).unwrap().into_iter().flatten() {
            prop_assert!(v.std >= 0.0);
            prop_assert!(v.lower <= v.middle && v.middle <= v.upper, "{v:?}");
        }
    }

    #[test]
    fn atr_and_adx_stay_in_range(bs in bars(), period in 1_usize..30) {
        let mut atr = Atr::new(period).unwrap();
        let mut adx = Adx::new(period).unwrap();
        for bar in bs {
            if let Some(v) = atr.update(bar).unwrap() {
                prop_assert!(v >= 0.0, "atr {v}");
            }
            let out = adx.update(bar).unwrap();
            for v in [out.adx, out.plus_di, out.minus_di].into_iter().flatten() {
                prop_assert!((0.0..=100.0 + 1e-9).contains(&v), "adx/di {v}");
            }
        }
    }
}
