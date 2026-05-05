"""RAD-910 diagnostic — per-fold regime / feature-importance audit.

Runs the same walk-forward configuration as the RAD-903 first-pass on EURUSD
1-min bid/ask Dukascopy bars (2yr window, 200k train / 50k test, h=60),
captures per-fold timestamps + realised P&L stats + XGBoost feature-importance
gains, and writes a tidy CSV plus a sidecar JSON of per-fold gain dicts.
Used to identify the fold(s) driving the h=60 OOS Sharpe outlier flagged
in RAD-910 and to A/B regime / calendar feature variants.

Run modes:

* default — RAD-903 baseline feature stack (no regime / calendar columns)
* ``--regime-only`` — adds ``regime_rv_pct`` + ``regime_volofvol``
* ``--calendar-only`` — adds ``month_*`` + ``week_of_month`` + ``is_first_friday``
* ``--regime-features`` — both feature toggles

``--align-start <iso-ts>`` clamps the post-warmup feature/label series to
the same starting timestamp across configurations so per-fold geometry is
identical between baseline and regime runs.

Caveats kept identical to RAD-903 first-pass: mid close-to-close forward
returns, no spread cost, overlap-inflated Sharpe.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from tradedesk.ml.cv import (
    WalkForwardConfig,
    WalkForwardSplitter,
    fold_metrics_from_predictions,
)
from tradedesk.ml.features import FeatureBuilder, FeatureConfig
from tradedesk.ml.labels import LabelConfig, forward_return_labels
from tradedesk.ml.model import DirectionClassifier, DirectionClassifierConfig
from tradedesk.ml.walk_forward_runner import (
    MINUTES_PER_TRADING_YEAR,
    load_dukascopy_bidask_minutes,
)

log = logging.getLogger("rad910")


@dataclass
class FoldDiagnosis:
    fold: int
    train_start_ts: pd.Timestamp
    train_end_ts: pd.Timestamp
    test_start_ts: pd.Timestamp
    test_end_ts: pd.Timestamp
    n_train: int
    n_test: int
    sharpe: float
    accuracy: float
    auc: float
    log_loss: float
    hit_rate: float
    max_drawdown: float
    trade_count: int
    realised_vol_train: float
    realised_vol_test: float
    vol_ratio: float
    abs_ret_test: float
    skew_test: float
    importances: dict


def _build_dataset_with_index(
    bars: pd.DataFrame,
    horizon: int,
    feature_config: FeatureConfig | None = None,
) -> tuple[pd.DataFrame, pd.Series, pd.Series, pd.DatetimeIndex]:
    """Like build_dataset but also returns the surviving timestamps."""
    builder = FeatureBuilder(config=feature_config)
    X = builder.transform(bars)

    label_cfg = LabelConfig(horizon=horizon, neutral_band=0.0)
    raw_labels = forward_return_labels(bars, label_cfg)

    closes = bars["close"].astype(float)
    fr = closes.shift(-horizon) / closes - 1.0

    aligned_labels = raw_labels.reindex(X.index)
    aligned_fr = fr.reindex(X.index)
    valid = aligned_labels.notna() & aligned_fr.notna()

    X_valid = X.loc[valid]
    fr_valid = aligned_fr.loc[valid].astype(float)
    raw_int = aligned_labels.loc[valid].astype("int64")
    y_binary = (raw_int > 0).astype("int64")
    return X_valid, y_binary, fr_valid, X_valid.index


def run_diagnosis(
    cache_dir: Path,
    *,
    symbol: str = "EURUSD",
    date_from: date = date(2024, 3, 15),
    date_to: date = date(2026, 3, 15),
    horizon: int = 60,
    train_window_bars: int = 200_000,
    test_window_bars: int = 50_000,
    threshold: float = 0.55,
    n_estimators: int = 200,
    n_jobs: int = 4,
    output: Path | None = None,
    feature_config: FeatureConfig | None = None,
    label: str = "baseline",
    align_start: pd.Timestamp | None = None,
) -> tuple[pd.DataFrame, list[FoldDiagnosis]]:
    log.info(
        "loading %s bid/ask 1-min bars %s -> %s",
        symbol,
        date_from,
        date_to,
    )
    bars = load_dukascopy_bidask_minutes(cache_dir, symbol, date_from, date_to)
    log.info("loaded %d bars", len(bars))

    log.info("building features + labels at h=%d", horizon)
    X, y, fr, idx = _build_dataset_with_index(
        bars, horizon, feature_config=feature_config
    )
    log.info("dataset (pre-align): X=%s y=%s fr=%s", X.shape, y.shape, fr.shape)
    if align_start is not None:
        mask = X.index >= align_start
        X = X.loc[mask]
        y = y.loc[mask]
        fr = fr.loc[mask]
        log.info(
            "dataset (post-align >= %s): X=%s y=%s fr=%s",
            align_start,
            X.shape,
            y.shape,
            fr.shape,
        )

    splitter_cfg = WalkForwardConfig(
        train_window=train_window_bars,
        test_window=test_window_bars,
        embargo=horizon,
        purge=horizon,
    )
    splitter = WalkForwardSplitter(splitter_cfg)
    log.info("n_folds=%d", splitter.n_splits(X))

    model_cfg = DirectionClassifierConfig(
        n_estimators=n_estimators,
        n_jobs=n_jobs,
        early_stopping_rounds=None,  # keep deterministic; no eval split here
    )

    diagnoses: list[FoldDiagnosis] = []
    rows: list[dict] = []

    closes_log = np.log(bars["close"].astype(float).to_numpy())
    closes_idx = bars.index

    for fold in splitter.split(X):
        X_train = X.iloc[fold.train_idx]
        y_train = y.iloc[fold.train_idx].astype(np.int64)
        X_test = X.iloc[fold.test_idx]
        y_test = y.iloc[fold.test_idx].astype(np.int64)
        fr_test = fr.iloc[fold.test_idx]

        train_start_ts = X.index[fold.train_idx[0]]
        train_end_ts = X.index[fold.train_idx[-1]]
        test_start_ts = X.index[fold.test_idx[0]]
        test_end_ts = X.index[fold.test_idx[-1]]

        log.info(
            "fold %d: train %s..%s (%d), test %s..%s (%d)",
            fold.fold,
            train_start_ts,
            train_end_ts,
            len(fold.train_idx),
            test_start_ts,
            test_end_ts,
            len(fold.test_idx),
        )

        model = DirectionClassifier(config=model_cfg)
        model.fit(X_train, y_train)
        proba = model.predict_proba(X_test)
        p_up = proba[:, 1]

        metrics = fold_metrics_from_predictions(
            fold=fold.fold,
            n_train=len(fold.train_idx),
            train_start=fold.train_start,
            train_end=fold.train_end,
            test_start=fold.test_start,
            test_end=fold.test_end,
            y_true=y_test.to_numpy(),
            p_up=p_up,
            forward_returns=fr_test.to_numpy(),
            threshold=threshold,
            periods_per_year=MINUTES_PER_TRADING_YEAR,
        )

        # Regime stats (pre-shift) for the train and test windows.
        train_mask = (closes_idx >= train_start_ts) & (closes_idx <= train_end_ts)
        test_mask = (closes_idx >= test_start_ts) & (closes_idx <= test_end_ts)
        train_log_close = closes_log[train_mask]
        test_log_close = closes_log[test_mask]
        train_logret = np.diff(train_log_close)
        test_logret = np.diff(test_log_close)
        rv_train = float(np.std(train_logret, ddof=0)) if train_logret.size else float("nan")
        rv_test = float(np.std(test_logret, ddof=0)) if test_logret.size else float("nan")
        vol_ratio = rv_test / rv_train if rv_train and rv_train > 0 else float("nan")
        abs_ret_test = float(np.mean(np.abs(test_logret))) if test_logret.size else float("nan")
        skew_test = (
            float(pd.Series(test_logret).skew()) if test_logret.size > 2 else float("nan")
        )

        # Feature importance (gain). XGBoost retains feature_names_in_ when fitted
        # on a DataFrame, so we just need to map booster's f-prefixed keys back.
        booster = model.model.get_booster()
        gains = booster.get_score(importance_type="gain")
        feature_names = list(X_train.columns)
        importances: dict[str, float] = {}
        for k, v in gains.items():
            try:
                idx = int(k[1:]) if k.startswith("f") and k[1:].isdigit() else None
                name = feature_names[idx] if idx is not None and idx < len(feature_names) else k
            except (ValueError, IndexError):
                name = k
            importances[name] = float(v)

        diag = FoldDiagnosis(
            fold=fold.fold,
            train_start_ts=train_start_ts,
            train_end_ts=train_end_ts,
            test_start_ts=test_start_ts,
            test_end_ts=test_end_ts,
            n_train=int(len(fold.train_idx)),
            n_test=int(len(fold.test_idx)),
            sharpe=metrics.sharpe,
            accuracy=metrics.accuracy,
            auc=metrics.auc,
            log_loss=metrics.log_loss,
            hit_rate=metrics.hit_rate,
            max_drawdown=metrics.max_drawdown,
            trade_count=metrics.trade_count,
            realised_vol_train=rv_train,
            realised_vol_test=rv_test,
            vol_ratio=vol_ratio,
            abs_ret_test=abs_ret_test,
            skew_test=skew_test,
            importances=importances,
        )
        diagnoses.append(diag)

        rows.append(
            {
                "label": label,
                "horizon": horizon,
                "fold": diag.fold,
                "train_start": str(train_start_ts),
                "train_end": str(train_end_ts),
                "test_start": str(test_start_ts),
                "test_end": str(test_end_ts),
                "n_train": diag.n_train,
                "n_test": diag.n_test,
                "sharpe": diag.sharpe,
                "log_loss": diag.log_loss,
                "accuracy": diag.accuracy,
                "auc": diag.auc,
                "hit_rate": diag.hit_rate,
                "max_drawdown": diag.max_drawdown,
                "trade_count": diag.trade_count,
                "rv_train": rv_train,
                "rv_test": rv_test,
                "vol_ratio": vol_ratio,
                "abs_ret_test": abs_ret_test,
                "skew_test": skew_test,
            }
        )

        log.info(
            "fold %d done: sharpe=%.3f acc=%.4f auc=%.4f trades=%d vol_ratio=%.3f",
            diag.fold,
            diag.sharpe,
            diag.accuracy,
            diag.auc,
            diag.trade_count,
            vol_ratio,
        )

    metrics_df = pd.DataFrame(rows).set_index("fold")
    if output is not None:
        metrics_df.to_csv(output)
        # importances side-file
        imp_path = output.with_suffix(".importances.json")
        with imp_path.open("w") as f:
            json.dump(
                [
                    {"fold": d.fold, "importances": d.importances}
                    for d in diagnoses
                ],
                f,
                indent=2,
            )
        log.info("wrote %s + %s", output, imp_path)

    return metrics_df, diagnoses


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=Path("/paperclip/tradedesk/marketdata"))
    parser.add_argument("--symbol", default="EURUSD")
    parser.add_argument(
        "--date-from",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=date(2024, 3, 15),
    )
    parser.add_argument(
        "--date-to",
        type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
        default=date(2026, 3, 15),
    )
    parser.add_argument("--horizon", type=int, default=60)
    parser.add_argument("--train-bars", type=int, default=200_000)
    parser.add_argument("--test-bars", type=int, default=50_000)
    parser.add_argument("--threshold", type=float, default=0.55)
    parser.add_argument("--n-estimators", type=int, default=200)
    parser.add_argument("--n-jobs", type=int, default=4)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--label", default="baseline")
    parser.add_argument(
        "--align-start",
        type=lambda s: pd.Timestamp(s, tz="UTC"),
        default=None,
        help=(
            "Trim the post-warmup feature/label/return series to start at this "
            "UTC timestamp. Use to align fold geometry across baseline / regime "
            "runs that have different warmup lengths."
        ),
    )
    parser.add_argument(
        "--regime-features",
        action="store_true",
        help="Enable BOTH regime and calendar feature toggles",
    )
    parser.add_argument(
        "--regime-only",
        action="store_true",
        help="Enable regime features (rv_pct, vol-of-vol) without calendar dummies",
    )
    parser.add_argument(
        "--calendar-only",
        action="store_true",
        help="Enable calendar dummies without regime features",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    feature_config: FeatureConfig | None = None
    if args.regime_features or args.regime_only or args.calendar_only:
        try:
            feature_config = FeatureConfig(
                include_regime_features=args.regime_features or args.regime_only,
                include_calendar_features=args.regime_features or args.calendar_only,
            )
        except TypeError:
            log.warning(
                "FeatureConfig does not yet accept regime/calendar toggles — "
                "running with default config"
            )
            feature_config = FeatureConfig()

    metrics_df, _ = run_diagnosis(
        args.cache,
        symbol=args.symbol,
        date_from=args.date_from,
        date_to=args.date_to,
        horizon=args.horizon,
        train_window_bars=args.train_bars,
        test_window_bars=args.test_bars,
        threshold=args.threshold,
        n_estimators=args.n_estimators,
        n_jobs=args.n_jobs,
        output=args.out,
        feature_config=feature_config,
        label=args.label,
        align_start=args.align_start,
    )

    print()
    print(metrics_df.to_string(float_format=lambda v: f"{v:.4f}"))
    print()
    print(
        f"summary | mean Sharpe = {metrics_df['sharpe'].mean():.3f}, "
        f"median = {metrics_df['sharpe'].median():.3f}, "
        f"min = {metrics_df['sharpe'].min():.3f}, "
        f"max = {metrics_df['sharpe'].max():.3f}, "
        f"folds positive = {(metrics_df['sharpe'] > 0).sum()}/{len(metrics_df)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
