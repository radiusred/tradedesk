"""Render the Comp 7 sample walk-forward report.

This is the script referenced from the [RAD-905] PR — it builds a
representative walk-forward CV run using the same random-walk fixture as
the leakage gate (so the report is reproducible without a Dukascopy
dependency) and emits the full markdown + PNG bundle into ``output_dir``.

It is *not* the Comp 5 strategy run — that lives in [RAD-903] and produces
real-feature data. When Comp 5 lands, replace the
:func:`_synthetic_walk_forward` block with a Comp 5 walk-forward run and
re-render.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from tradedesk.ml.cv import WalkForwardSplitter
from tradedesk.ml.model import DirectionClassifier, DirectionClassifierConfig
from tradedesk.ml.reporting import (
    render_markdown_report,
    run_leakage_sanity,
    walk_forward_collect,
)


def _classifier_factory() -> DirectionClassifier:
    return DirectionClassifier(
        DirectionClassifierConfig(
            n_estimators=80,
            max_depth=3,
            learning_rate=0.1,
            early_stopping_rounds=None,
            seed=42,
            n_jobs=1,
        )
    )


def _synthetic_walk_forward(
    *,
    n: int = 4000,
    seed: int = 0,
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Build a noisy random-walk dataset that exercises every report panel.

    Returns ``(X, y, forward_returns)``. The features carry a small but real
    edge (a noisy version of the label plus pure noise) so feature-importance
    gains and the equity curve are non-trivial without crossing the
    leakage-gate thresholds.
    """
    rng = np.random.default_rng(seed)
    h = 5
    increments = rng.normal(0.0, 1.0, n + h)
    close = 100.0 + increments.cumsum()
    forward_return = close[h : n + h] - close[:n]
    y = pd.Series((forward_return > 0).astype(np.int64), name="y")

    # Noisy edge: 30% signal, the rest random.
    edge = 0.30 * y.to_numpy().astype(float) + rng.normal(0.0, 1.0, n)
    momentum = pd.Series(close[:n]).diff(periods=10).fillna(0.0).to_numpy()
    noise = rng.normal(0.0, 1.0, n)

    X = pd.DataFrame(
        {"edge": edge, "momentum": momentum, "noise": noise},
        index=pd.RangeIndex(n),
    )
    fwd = pd.Series(forward_return / close[:n], index=X.index, name="forward_return")
    return X, y, fwd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("docs/ml/sample-walk-forward-report"),
        help="Where to write report.md + equity_curve.png.",
    )
    parser.add_argument(
        "--n-samples",
        type=int,
        default=4000,
        help="Synthetic sample count.",
    )
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    X, y, fwd = _synthetic_walk_forward(n=args.n_samples, seed=args.seed)
    splitter = WalkForwardSplitter(
        train_window=1500,
        test_window=500,
        purge=5,
        embargo=0,
    )
    artifacts = walk_forward_collect(
        X, y, splitter, _classifier_factory, forward_returns=fwd
    )
    leakage_result = run_leakage_sanity(model_factory=_classifier_factory)

    md_path = render_markdown_report(
        artifacts,
        args.output_dir,
        title="Phase 6 walk-forward CV — sample report",
        leakage_result=leakage_result,
    )
    print(f"wrote {md_path}")
    print(f"wrote {md_path.parent / 'equity_curve.png'}")
    print(
        f"folds={len(artifacts)} leakage_passed={leakage_result.passed} "
        f"min_acc={leakage_result.min_accuracy:.4f}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
