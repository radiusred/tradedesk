"""Section 9 (key insights) data prep."""

from typing import Any

from .stats import calc_stats


def _prepare_insights(
    metrics: list[dict[str, str]],
    consistency_data: list[dict[str, Any]],
    risk_adj_data: list[dict[str, Any]],
    total_round_trips: int,
) -> dict[str, Any]:
    metrics = [m for m in metrics if m.get("instrument") != "PORTFOLIO"]

    insights: dict[str, Any] = {}
    final_equities = [float(m.get("final_equity", 0)) for m in metrics]
    if not final_equities:
        return insights

    best_idx = final_equities.index(max(final_equities))
    worst_idx = final_equities.index(min(final_equities))
    equity_range = max(final_equities) - min(final_equities)
    equity_stats = calc_stats(final_equities)
    equity_cv = (
        ((equity_stats["std"] / equity_stats["mean"]) * 100) if equity_stats["mean"] != 0 else 0
    )

    insights = {
        "best": {
            "instrument": metrics[best_idx]["instrument"],
            "val": final_equities[best_idx],
        },
        "worst": {
            "instrument": metrics[worst_idx]["instrument"],
            "val": final_equities[worst_idx],
        },
        "range": equity_range,
        "cv": equity_cv,
    }

    if consistency_data:
        most_consistent = min(consistency_data, key=lambda x: abs(float(x["cv_pnl"])))
        least_consistent = max(consistency_data, key=lambda x: abs(float(x["cv_pnl"])))
        insights["consistent"] = {
            "most": {
                "instrument": most_consistent["instrument"],
                "cv": most_consistent["cv_pnl"],
            },
            "least": {
                "instrument": least_consistent["instrument"],
                "cv": least_consistent["cv_pnl"],
            },
        }

    if risk_adj_data:
        best_risk_adj = max(risk_adj_data, key=lambda x: x["return_per_risk"])
        best_dd_ratio = max(risk_adj_data, key=lambda x: x["dd_ratio"])
        insights["risk"] = {
            "best_return": {
                "instrument": best_risk_adj["instrument"],
                "val": best_risk_adj["return_per_risk"],
            },
            "best_recovery": {
                "instrument": best_dd_ratio["instrument"],
                "val": best_dd_ratio["dd_ratio"],
            },
        }

    max_trades = max(metrics, key=lambda x: int(x.get("round_trips", 0)))
    min_trades = min(metrics, key=lambda x: int(x.get("round_trips", 0)))
    max_pct = (
        ((int(max_trades.get("round_trips", 0)) / total_round_trips) * 100)
        if total_round_trips
        else 0
    )
    min_pct = (
        ((int(min_trades.get("round_trips", 0)) / total_round_trips) * 100)
        if total_round_trips
        else 0
    )
    trade_pcts = [
        (int(m.get("round_trips", 0)) / total_round_trips * 100) if total_round_trips else 0
        for m in metrics
    ]
    exposure_stats = calc_stats(trade_pcts)
    exposure_cv = (
        ((exposure_stats["std"] / exposure_stats["mean"]) * 100)
        if exposure_stats["mean"] != 0
        else 0
    )
    insights["exposure"] = {
        "most": {
            "instrument": max_trades["instrument"],
            "count": max_trades["round_trips"],
            "pct": max_pct,
        },
        "least": {
            "instrument": min_trades["instrument"],
            "count": min_trades["round_trips"],
            "pct": min_pct,
        },
        "cv": exposure_cv,
    }

    return insights
