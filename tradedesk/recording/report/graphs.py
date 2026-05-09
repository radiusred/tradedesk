"""Backtest graph rendering (matplotlib/seaborn)."""

import logging
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

log = logging.getLogger(__name__)


def _prepare_graphs(
    round_trips_file: Path, equity_file: Path, cache_dir: Path | None = None
) -> None:
    output_dir = round_trips_file.parent / "graphs"
    output_dir.mkdir(exist_ok=True)

    round_trips = pd.read_csv(round_trips_file)
    equity = pd.read_csv(equity_file)

    round_trips["exit_ts"] = pd.to_datetime(round_trips["exit_ts"])
    _exit_ts = round_trips["exit_ts"]
    if _exit_ts.dt.tz is not None:
        _exit_ts = _exit_ts.dt.tz_convert("UTC").dt.tz_localize(None)
    round_trips["month"] = _exit_ts.dt.to_period("M")
    equity["timestamp"] = pd.to_datetime(equity["timestamp"])

    # 1. Monthly Performance Heatmap
    try:
        monthly_pnl = (
            round_trips.groupby(["month", "instrument"])["pnl"].sum().unstack(fill_value=0)
        )
        portfolio_monthly = monthly_pnl.sum(axis=1)
        monthly_pnl["PORTFOLIO"] = portfolio_monthly

        heatmap_data = monthly_pnl.copy()
        heatmap_data.index = heatmap_data.index.astype(str)
        plt.figure(figsize=(15, 10))
        sns.heatmap(heatmap_data.T, annot=True, fmt=".0f", cmap="RdYlGn", center=0)
        plt.title("Monthly PnL by Instrument")
        plt.tight_layout()
        plt.savefig(output_dir / "monthly_pnl_heatmap.png")
    except Exception as e:
        log.warning(f"Failed to generate monthly performance heatmap: {e}")

    # Equity Curve
    try:
        fig, ax = plt.subplots(figsize=(10, 8))
        sns.lineplot(
            data=equity, x="timestamp", y="equity", ax=ax, label="PORTFOLIO", color="#00AA00"
        )

        if cache_dir is not None:
            try:
                from tradedesk.execution.backtest import read_dukascopy_candles

                date_from = equity["timestamp"].min().date()
                date_to = equity["timestamp"].max().date()
                candles = read_dukascopy_candles(cache_dir, "GBRIDXGBP", "1D", date_from, date_to)
                closes = [c.close for c in candles]
                control: list[float] = [0.0]
                for i in range(1, len(closes)):
                    control.append(control[-1] + closes[i] - closes[i - 1])
                control_dates = [pd.Timestamp(c.timestamp) for c in candles]
                ax.plot(
                    pd.DatetimeIndex(control_dates),
                    control,
                    color="#888888",
                    linewidth=1,
                    label="FTSE 100",
                )
                ax.legend()
            except Exception as e:
                log.warning(f"Failed to add FTSE control line: {e}")

        ax.set_title("Equity Curve")
        ax.set_xlabel("Time")
        ax.set_ylabel("Equity")
        fig.savefig(output_dir / "equity_curve.png")
        plt.close(fig)
    except Exception as e:
        log.warning(f"Failed to generate equity curve plot: {e}")

    # MFE / MAE Analysis
    try:
        plt.figure(figsize=(12, 6))
        sns.scatterplot(data=round_trips, x="mae_pnl", y="pnl", hue="instrument", alpha=0.5)
        plt.title("MAE vs Final PnL (Risk vs Reward)")
        plt.axhline(0, color="black", linestyle="--")
        plt.tight_layout()
        plt.savefig(output_dir / "mae_vs_pnl.png")
    except Exception as e:
        log.warning(f"Failed to generate MAE vs PnL plot: {e}")

    # Equity Correlation
    try:
        round_trips["date"] = round_trips["exit_ts"].dt.date
        daily_pnl = round_trips.groupby(["date", "instrument"])["pnl"].sum().unstack(fill_value=0)
        correlation_matrix = daily_pnl.corr()

        plt.figure(figsize=(10, 8))
        sns.heatmap(correlation_matrix, annot=True, cmap="coolwarm", vmin=-1, vmax=1)
        plt.title("Daily PnL Correlation Matrix")
        plt.tight_layout()
        plt.savefig(output_dir / "instrument_correlation.png")
    except Exception as e:
        log.warning(f"Failed to generate instrument correlation matrix: {e}")
