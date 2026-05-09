"""Section 1 (overall performance) and Section 2 (cross-instrument stats) data prep."""

from typing import Any

from .stats import calc_stats


def _prepare_overall_performance(
    metrics: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
    portfolio_metrics = [m for m in metrics if m.get("instrument") == "PORTFOLIO"]
    instrument_metrics = [m for m in metrics if m.get("instrument") != "PORTFOLIO"]
    sorted_instruments = sorted(
        instrument_metrics,
        key=lambda x: float(x.get("final_equity", 0)),
        reverse=True,
    )
    return portfolio_metrics, instrument_metrics, sorted_instruments


def _prepare_stats_table(
    metrics: list[dict[str, str]],
) -> list[dict[str, Any]]:
    metrics_cols = {
        "final_equity": [float(m.get("final_equity", 0)) for m in metrics],
        "max_dd": [float(m.get("max_dd", 0)) for m in metrics],
        "win_rate": [float(m.get("win_rate", 0)) for m in metrics],
        "profit_factor": [float(m.get("profit_factor", 0)) for m in metrics],
        "expectancy": [float(m.get("expectancy", 0)) for m in metrics],
    }

    stats_table = []
    for col, values in metrics_cols.items():
        stats = calc_stats(values)
        cv = (stats["std"] / abs(stats["mean"])) * 100 if stats["mean"] != 0 else 0
        stats_table.append({"metric": col, **stats, "cv": cv})

    return stats_table
