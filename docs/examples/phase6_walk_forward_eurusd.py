"""Run the Phase 6 first-pass walk-forward on EURUSD 1-min Dukascopy data.

Usage::

    python docs/examples/phase6_walk_forward_eurusd.py \\
        --cache /paperclip/tradedesk/marketdata \\
        --date-from 2018-01-01 --date-to 2026-03-15 \\
        --out /tmp/phase6_eurusd.csv

The script wires :func:`tradedesk.ml.run_walk_forward` to a Dukascopy cache
and writes a tidy CSV with columns ``horizon, fold, n_train, n_test,
log_loss, accuracy, auc, hit_rate, sharpe, max_drawdown, trade_count``
suitable for posting on the RAD-896 Phase 6 sprint thread.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from tradedesk.ml.model import DirectionClassifierConfig
from tradedesk.ml.walk_forward_runner import (
    WalkForwardRunConfig,
    run_walk_forward,
)


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--symbol", default="EURUSD")
    p.add_argument("--date-from", type=_parse_date, default=date(2018, 1, 1))
    p.add_argument("--date-to", type=_parse_date, default=date(2026, 3, 15))
    p.add_argument(
        "--horizons",
        type=int,
        nargs="+",
        default=[15, 60],
        help="Forward-return horizons in 1-min bars",
    )
    p.add_argument("--threshold", type=float, default=0.55)
    p.add_argument(
        "--train-window-bars",
        type=int,
        default=500_000,
        help="~2 years of 24x5 FX bars (default: 500000)",
    )
    p.add_argument(
        "--test-window-bars",
        type=int,
        default=125_000,
        help="~6 months of 24x5 FX bars (default: 125000)",
    )
    p.add_argument(
        "--n-estimators",
        type=int,
        default=200,
        help="XGBoost n_estimators per fold (default: 200, lower than the "
        "DirectionClassifier default of 500 to keep total wall-time bounded)",
    )
    p.add_argument(
        "--n-jobs",
        type=int,
        default=4,
        help="XGBoost n_jobs per fold (default: 4; the DirectionClassifier "
        "default of 1 prioritises bit-exact determinism over speed)",
    )
    p.add_argument(
        "--label-neutral-band",
        type=float,
        default=0.0,
        help="Forward-return magnitude below which labels are treated as flat",
    )
    p.add_argument(
        "--spread-aware",
        action="store_true",
        help=(
            "Switch labels to LabelConfig(spread_aware=True) and feed "
            "direction-aware ask-to-bid round-trip forward returns into "
            "walk_forward_evaluate (RAD-908). Ignores --label-neutral-band."
        ),
    )
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )

    model_cfg = DirectionClassifierConfig(
        n_estimators=args.n_estimators,
        n_jobs=args.n_jobs,
        early_stopping_rounds=None,
    )
    cfg = WalkForwardRunConfig(
        symbol=args.symbol,
        date_from=args.date_from,
        date_to=args.date_to,
        horizons=tuple(args.horizons),
        threshold=args.threshold,
        train_window_bars=args.train_window_bars,
        test_window_bars=args.test_window_bars,
        model_config=model_cfg,
        label_neutral_band=args.label_neutral_band,
        spread_aware=args.spread_aware,
    )

    result = run_walk_forward(args.cache, cfg)

    rows: list[pd.DataFrame] = []
    for horizon, df in result.per_horizon_metrics.items():
        if df.empty:
            continue
        out = df.reset_index().assign(horizon=horizon)
        rows.append(out)
    combined = (
        pd.concat(rows, ignore_index=True)
        if rows
        else pd.DataFrame()
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(args.out, index=False)

    print(
        f"Wrote {len(combined)} fold rows across "
        f"{len(result.per_horizon_metrics)} horizons to {args.out}"
    )
    if not combined.empty:
        for horizon, df in result.per_horizon_metrics.items():
            if df.empty:
                continue
            print(f"\n=== horizon={horizon} ({len(df)} folds) ===")
            print(df.to_string(float_format=lambda x: f"{x:.4f}"))
            print(
                f"  mean Sharpe={df['sharpe'].mean():.3f}  "
                f"median={df['sharpe'].median():.3f}  "
                f"mean accuracy={df['accuracy'].mean():.3f}  "
                f"mean hit_rate={df['hit_rate'].mean():.3f}"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
