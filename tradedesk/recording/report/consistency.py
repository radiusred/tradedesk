"""Section 3 (consistency) and Section 4 (volatility classification) data prep."""

from collections import defaultdict
from typing import Any

from .stats import calc_stats


def _prepare_consistency_data(round_trips: list[dict[str, str]]) -> list[dict[str, Any]]:
    inst_pnls: defaultdict[str, list[float]] = defaultdict(list)
    for rt in round_trips:
        inst_pnls[rt["instrument"]].append(float(rt["pnl"]))

    consistency_data: list[dict[str, Any]] = []
    for inst, pnls in inst_pnls.items():
        pnl_stats = calc_stats(pnls)
        cv_pnl = (pnl_stats["std"] / pnl_stats["mean"] * 100) if pnl_stats["mean"] != 0 else 0
        iqr = pnl_stats["p75"] - pnl_stats["p25"]
        lower_bound = pnl_stats["p25"] - 1.5 * iqr
        upper_bound = pnl_stats["p75"] + 1.5 * iqr
        outliers = sum(1 for p in pnls if p < lower_bound or p > upper_bound)
        outlier_pct = (outliers / len(pnls)) * 100

        consistency_data.append(
            {
                "instrument": inst,
                "mean_pnl": pnl_stats["mean"],
                "std_pnl": pnl_stats["std"],
                "cv_pnl": cv_pnl,
                "median_pnl": pnl_stats["median"],
                "iqr": iqr,
                "outlier_pct": outlier_pct,
            }
        )
    consistency_data.sort(key=lambda x: float(x["cv_pnl"]))
    return consistency_data


def _classify_volatility(consistency_data: list[dict[str, Any]]) -> dict[str, Any]:
    volatility: dict[str, Any] = {}
    cv_values = [float(cd["cv_pnl"]) for cd in consistency_data]
    if cv_values:
        cv_median = sorted(cv_values)[len(cv_values) // 2]
        low_vol = [
            str(cd["instrument"]) for cd in consistency_data if float(cd["cv_pnl"]) < cv_median
        ]
        high_vol = [
            str(cd["instrument"]) for cd in consistency_data if float(cd["cv_pnl"]) >= cv_median
        ]
        volatility = {"median": cv_median, "low": low_vol, "high": high_vol}
    return volatility
