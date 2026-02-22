"""Performance analysis report generation for backtest results using Jinja2 templates."""

import csv
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jinja2 import Environment, FileSystemLoader


def calc_stats(values: list[float]) -> dict[str, float]:
    """Calculate summary statistics for a list of values."""
    n = len(values)
    if n == 0:
        return {
            "count": 0,
            "mean": 0.0,
            "std": 0.0,
            "min": 0.0,
            "p25": 0.0,
            "median": 0.0,
            "p75": 0.0,
            "max": 0.0,
        }

    mean_val = sum(values) / n
    variance = sum((x - mean_val) ** 2 for x in values) / n
    std_dev = math.sqrt(variance)
    sorted_vals = sorted(values)

    return {
        "count": n,
        "mean": mean_val,
        "std": std_dev,
        "min": sorted_vals[0],
        "p25": sorted_vals[n // 4],
        "median": sorted_vals[n // 2],
        "p75": sorted_vals[3 * n // 4],
        "max": sorted_vals[-1],
    }


def standardize(values: list[float]) -> list[float]:
    """Standardize values using z-score normalization."""
    if not values:
        return []
    mean_val = sum(values) / len(values)
    variance = sum((x - mean_val) ** 2 for x in values) / len(values)
    std_dev = math.sqrt(variance)
    return [(x - mean_val) / std_dev if std_dev > 0 else 0 for x in values]


def euclidean_distance(
    p1: dict[str, float], p2: dict[str, float], metric_keys: list[str]
) -> float:
    """Calculate Euclidean distance between two performance profiles."""
    z_keys = [f"{key}_z" for key in metric_keys]
    return math.sqrt(
        sum((p1[zk] - p2[zk]) ** 2 for zk in z_keys if zk in p1 and zk in p2)
    )


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        return list(reader)


def _ensure_instrument_field(rows: list[dict[str, str]]) -> None:
    """Ensure each row has an 'instrument' key (backcompat with old CSVs using 'epic')."""
    for r in rows:
        if "epic" in r and "instrument" not in r:
            r["instrument"] = r["epic"]


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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    metrics_cols = {
        "final_equity": [float(m.get("final_equity", 0)) for m in metrics],
        "max_dd": [float(m.get("max_dd", 0)) for m in metrics],
        "win_rate": [float(m.get("win_rate", 0)) for m in metrics],
        "profit_factor": [float(m.get("profit_factor", 0)) for m in metrics],
        "expectancy": [float(m.get("expectancy", 0)) for m in metrics],
    }

    stats_table = []
    cv_data = []
    for col, values in metrics_cols.items():
        stats = calc_stats(values)
        stats_table.append({"metric": col, **stats})
        cv = (stats["std"] / abs(stats["mean"])) * 100 if stats["mean"] != 0 else 0
        cv_data.append({"metric": col, "cv": cv})
    return stats_table, cv_data


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


def _prepare_exposure_data(metrics: list[dict[str, str]]) -> tuple[list[dict[str, Any]], int]:
    total_round_trips = sum(int(m.get("round_trips", 0)) for m in metrics)
    exposure_data = []
    for m in sorted(
        metrics, key=lambda x: int(x.get("round_trips", 0)), reverse=True
    ):
        pct_trades = (
            (int(m.get("round_trips", 0)) / total_round_trips) * 100
        ) if total_round_trips else 0
        exposure_data.append(
            {
                "instrument": m.get("instrument"),
                "fills": int(m.get("fills", 0)),
                "trades": int(m.get("round_trips", 0)),
                "avg_hold": float(m.get("avg_hold_min", 0)),
                "pct_trades": pct_trades,
            }
        )
    return exposure_data, total_round_trips


def _prepare_risk_adj_data(
    metrics: list[dict[str, str]], consistency_data: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    risk_adj_data: list[dict[str, Any]] = []
    for m in metrics:
        inst = m.get("instrument")
        cd = next((c for c in consistency_data if c["instrument"] == inst), None)
        if cd:
            return_per_risk = (
                float(m.get("final_equity", 0)) / float(cd["std_pnl"])
            ) if float(cd["std_pnl"]) > 0 else 0
            dd_ratio = (
                float(m.get("final_equity", 0)) / abs(float(m.get("max_dd", 0)))
            ) if float(m.get("max_dd", 0)) != 0 else 0
            risk_adj_data.append(
                {
                    "instrument": inst,
                    "final_equity": float(m.get("final_equity", 0)),
                    "max_dd": float(m.get("max_dd", 0)),
                    "std_pnl": cd["std_pnl"],
                    "return_per_risk": return_per_risk,
                    "dd_ratio": dd_ratio,
                }
            )
    risk_adj_data.sort(key=lambda x: x["return_per_risk"], reverse=True)
    return risk_adj_data


def _prepare_similarity(
    metrics: list[dict[str, str]], consistency_data: list[dict[str, Any]]
) -> dict[str, Any]:
    similarity: dict[str, Any] = {}
    if len(consistency_data) < 2:
        return similarity

    profile_metrics: dict[str, dict[str, Any]] = {}
    for m in metrics:
        inst = m.get("instrument")
        cd = next((c for c in consistency_data if c["instrument"] == inst), None)
        if cd is None or inst is None:
            continue
        profile_metrics[inst] = {
            "final_equity": float(m.get("final_equity", 0)),
            "win_rate": float(m.get("win_rate", 0)),
            "profit_factor": float(m.get("profit_factor", 0)),
            "expectancy": float(m.get("expectancy", 0)),
            "cv_pnl": cd["cv_pnl"],
        }

    metric_keys = ["final_equity", "win_rate", "profit_factor", "expectancy", "cv_pnl"]
    instruments = list(profile_metrics.keys())

    for key in metric_keys:
        values = [profile_metrics[inst][key] for inst in instruments]
        standardized = standardize(values)
        for i, inst in enumerate(instruments):
            profile_metrics[inst][f"{key}_z"] = standardized[i]

    if len(instruments) < 2:
        return similarity

    min_dist = float("inf")
    max_dist = 0.0
    min_pair = None
    max_pair = None

    for i, inst1 in enumerate(instruments):
        for inst2 in instruments[i + 1 :]:
            dist = euclidean_distance(
                profile_metrics[inst1], profile_metrics[inst2], metric_keys
            )
            if dist < min_dist:
                min_dist = dist
                min_pair = (inst1, inst2)
            if dist > max_dist:
                max_dist = dist
                max_pair = (inst1, inst2)

    matrix_headers = [
        inst.split(".")[2] if "." in inst else inst for inst in instruments
    ]
    matrix_rows: list[dict[str, Any]] = []
    for inst1 in instruments:
        dists: list[str] = []
        for inst2 in instruments:
            dist = (
                euclidean_distance(
                    profile_metrics[inst1], profile_metrics[inst2], metric_keys
                )
                if inst1 != inst2
                else 0
            )
            dists.append(f"{dist:.2f}")
        matrix_rows.append(
            {
                "instrument": inst1.split(".")[2] if "." in inst1 else inst1,
                "dists": dists,
            }
        )

    similarity = {
        "min_pair": min_pair,
        "min_dist": min_dist,
        "max_pair": max_pair,
        "max_dist": max_dist,
        "matrix_headers": matrix_headers,
        "matrix_rows": matrix_rows,
    }
    return similarity


def _build_instrument_data(
    round_trips: list[dict[str, str]],
) -> dict[str, list[dict[str, str]]]:
    inst_data: defaultdict[str, list[dict[str, str]]] = defaultdict(list)
    for rt in round_trips:
        inst_data[rt["instrument"]].append(rt)
    return dict(inst_data)


def _prepare_evolution_data(
    inst_data: dict[str, list[dict[str, str]]],
) -> list[dict[str, Any]]:
    evolution_data = []
    for inst in sorted(inst_data.keys()):
        inst_trips = [
            (datetime.fromisoformat(rt["exit_ts"].replace("Z", "+00:00")), float(rt["pnl"]))
            for rt in inst_data[inst]
        ]
        inst_trips.sort()
        if not inst_trips:
            continue

        cum_pnl = []
        running_total = 0.0
        for _, pnl in inst_trips:
            running_total += pnl
            cum_pnl.append(running_total)

        running_max = []
        max_so_far = cum_pnl[0]
        for pnl in cum_pnl:
            max_so_far = max(max_so_far, pnl)
            running_max.append(max_so_far)

        drawdowns = [cum - mx for cum, mx in zip(cum_pnl, running_max)]
        max_dd = min(drawdowns)
        in_drawdown = sum(1 for dd in drawdowns if dd < 0)

        evolution_data.append({
            "instrument": inst,
            "final_pnl": cum_pnl[-1],
            "peak_pnl": max(running_max),
            "max_dd": max_dd,
            "dd_count": in_drawdown,
            "dd_pct": (in_drawdown / len(inst_trips) * 100),
        })
    return evolution_data


def _prepare_insights(
    metrics: list[dict[str, str]],
    consistency_data: list[dict[str, Any]],
    risk_adj_data: list[dict[str, Any]],
    total_round_trips: int,
) -> dict[str, Any]:
    insights: dict[str, Any] = {}
    final_equities = [float(m.get("final_equity", 0)) for m in metrics]
    if not final_equities:
        return insights

    best_idx = final_equities.index(max(final_equities))
    worst_idx = final_equities.index(min(final_equities))
    equity_range = max(final_equities) - min(final_equities)
    equity_stats = calc_stats(final_equities)
    equity_cv = (
        (equity_stats["std"] / equity_stats["mean"]) * 100
    ) if equity_stats["mean"] != 0 else 0

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
        (int(max_trades.get("round_trips", 0)) / total_round_trips) * 100
    ) if total_round_trips else 0
    min_pct = (
        (int(min_trades.get("round_trips", 0)) / total_round_trips) * 100
    ) if total_round_trips else 0
    trade_pcts = [
        (int(m.get("round_trips", 0)) / total_round_trips * 100) if total_round_trips else 0
        for m in metrics
    ]
    exposure_stats = calc_stats(trade_pcts)
    exposure_cv = (
        (exposure_stats["std"] / exposure_stats["mean"]) * 100
    ) if exposure_stats["mean"] != 0 else 0
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


def _prepare_additional_analysis(
    inst_data: dict[str, list[dict[str, str]]],
    round_trips: list[dict[str, str]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    win_loss_data = []
    streak_data = []
    hold_time_data = []
    best_worst_trades = []
    mfe_mae_data = []
    long_short_data = []

    for inst in sorted(inst_data.keys()):
        trips = inst_data[inst]
        wins = [float(rt["pnl"]) for rt in trips if float(rt["pnl"]) > 0]
        losses = [float(rt["pnl"]) for rt in trips if float(rt["pnl"]) < 0]

        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        win_loss_ratio = avg_win / abs(avg_loss) if avg_loss != 0 else 0
        win_loss_data.append(
            {
                "instrument": inst,
                "wins": len(wins),
                "losses": len(losses),
                "win_pct": (len(wins) / len(trips)) * 100 if trips else 0,
                "avg_win": avg_win,
                "avg_loss": avg_loss,
                "ratio": win_loss_ratio,
            }
        )

        trips_sorted = sorted(
            trips, key=lambda x: datetime.fromisoformat(x["exit_ts"].replace("Z", "+00:00"))
        )
        max_win_streak = max_loss_streak = current_win_streak = current_loss_streak = 0
        for rt in trips_sorted:
            pnl = float(rt["pnl"])
            if pnl > 0:
                current_win_streak += 1
                current_loss_streak = 0
                max_win_streak = max(max_win_streak, current_win_streak)
            else:
                current_loss_streak += 1
                current_win_streak = 0
                max_loss_streak = max(max_loss_streak, current_loss_streak)
        last_pnl = float(trips_sorted[-1]["pnl"]) if trips_sorted else 0
        current_state = (f"W:{current_win_streak}" if last_pnl > 0 else f"L:{current_loss_streak}")
        streak_data.append(
            {
                "instrument": inst,
                "max_win": max_win_streak,
                "max_loss": max_loss_streak,
                "current": current_state,
            }
        )

        hold_times = [float(rt.get("hold_minutes", 0)) for rt in trips]
        avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0
        variance = (
            sum((x - avg_hold) ** 2 for x in hold_times) / len(hold_times)
        ) if hold_times else 0
        hold_time_data.append(
            {
                "instrument": inst,
                "avg": avg_hold,
                "min": min(hold_times) if hold_times else 0,
                "max": max(hold_times) if hold_times else 0,
                "std": variance ** 0.5,
            }
        )

        best = max(trips, key=lambda x: float(x["pnl"]))
        worst = min(trips, key=lambda x: float(x["pnl"]))
        best_worst_trades.append(
            {
                "instrument": inst,
                "best": {
                    "pnl": float(best["pnl"]),
                    "entry_ts": best["entry_ts"],
                    "hold_minutes": float(best["hold_minutes"]),
                    "direction": best["direction"],
                },
                "worst": {
                    "pnl": float(worst["pnl"]),
                    "entry_ts": worst["entry_ts"],
                    "hold_minutes": float(worst["hold_minutes"]),
                    "direction": worst["direction"],
                },
            }
        )

        trips_with_mfe = [rt for rt in trips if rt.get("mfe_pnl", "") not in ("", None)]
        if trips_with_mfe:
            total_mfe_pnl = sum(float(rt["mfe_pnl"]) for rt in trips_with_mfe)
            total_mae_pnl = sum(float(rt["mae_pnl"]) for rt in trips_with_mfe)
            total_pnl = sum(float(rt["pnl"]) for rt in trips)
            mfe_mae_data.append(
                {
                    "instrument": inst,
                    "avg_mfe": total_mfe_pnl / len(trips),
                    "avg_mae": total_mae_pnl / len(trips),
                    "mfe_ratio": total_mfe_pnl / total_pnl if total_pnl != 0 else 0,
                    "mae_ratio": total_mae_pnl / total_pnl if total_pnl != 0 else 0,
                }
            )

        long_trades = [rt for rt in trips if rt.get("direction", "").lower() == "long"]
        short_trades = [rt for rt in trips if rt.get("direction", "").lower() == "short"]
        long_pnl = sum(float(rt["pnl"]) for rt in long_trades)
        short_pnl = sum(float(rt["pnl"]) for rt in short_trades)
        if long_pnl + short_pnl != 0:
            long_bias = (long_pnl / (long_pnl + short_pnl)) * 100
        else:
            long_bias = 50
        bias_str = (
            f"L{long_bias:.0f}%"
            if long_bias > 55
            else (f"S{100 - long_bias:.0f}%" if long_bias < 45 else "Neutral")
        )
        long_short_data.append(
            {
                "instrument": inst,
                "long_cnt": len(long_trades),
                "short_cnt": len(short_trades),
                "long_pnl": long_pnl,
                "short_pnl": short_pnl,
                "bias": bias_str,
            }
        )

    return (
        win_loss_data,
        streak_data,
        hold_time_data,
        best_worst_trades,
        mfe_mae_data,
        long_short_data,
    )


def _prepare_monthly_data(
    round_trips: list[dict[str, str]],
    inst_data: dict[str, list[dict[str, str]]],
) -> SimpleNamespace:
    monthly_pnl: defaultdict[str, defaultdict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    for rt in round_trips:
        exit_dt = datetime.fromisoformat(rt["exit_ts"].replace("Z", "+00:00"))
        month_key = exit_dt.strftime("%Y-%m")
        monthly_pnl[month_key][rt["instrument"]] += float(rt["pnl"])

    months = sorted(monthly_pnl.keys())
    instruments = sorted(inst_data.keys())
    short_names = {
        inst: inst.split(".")[2] if "." in inst else inst for inst in instruments
    }
    monthly_rows = []
    grand_total = 0.0
    inst_totals: defaultdict[str, float] = defaultdict(float)

    for month in months:
        values = []
        month_total = 0.0
        for inst in instruments:
            pnl = monthly_pnl[month].get(inst, 0)
            values.append(f"{pnl:.2f}")
            month_total += pnl
            inst_totals[inst] += pnl
        monthly_rows.append(SimpleNamespace(month=month, values=values, total=month_total))
        grand_total += month_total

    monthly_data = SimpleNamespace(
        headers=[short_names[inst] for inst in instruments],
        rows=monthly_rows,
        totals=SimpleNamespace(
            values=[f"**{inst_totals[inst]:.2f}**" for inst in instruments], grand=grand_total
        ),
    )
    return monthly_data


def _prepare_exit_reasons(
    round_trips: list[dict[str, str]],
    inst_data: dict[str, list[dict[str, str]]],
) -> dict[str, Any]:
    has_exit_reasons = any(
        rt.get("exit_reason", "") not in ("", "market_order") for rt in round_trips
    )
    exit_reasons: dict[str, Any] = {
        "available": has_exit_reasons,
        "portfolio": [],
        "instruments": [],
    }
    if not has_exit_reasons:
        return exit_reasons

    reason_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "total_pnl": 0.0, "wins": 0}
    )
    for rt in round_trips:
        reason = rt.get("exit_reason", "unknown") or "unknown"
        pnl = float(rt["pnl"])
        reason_stats[reason]["count"] += 1
        reason_stats[reason]["total_pnl"] += pnl
        if pnl > 0:
            reason_stats[reason]["wins"] += 1

    total_count = sum(v["count"] for v in reason_stats.values())

    def reason_key(r: str) -> float:
        return float(reason_stats[r]["total_pnl"])

    for reason in sorted(reason_stats, key=reason_key, reverse=True):
        s = reason_stats[reason]
        exit_reasons["portfolio"].append(
            {
                "reason": reason,
                "count": s["count"],
                "pct": (s["count"] / total_count) * 100 if total_count else 0,
                "total_pnl": s["total_pnl"],
                "avg_pnl": s["total_pnl"] / s["count"] if s["count"] else 0,
                "win_rate": (s["wins"] / s["count"]) * 100 if s["count"] else 0,
            }
        )

    for inst in sorted(inst_data.keys()):
        inst_reasons: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"count": 0, "total_pnl": 0.0, "wins": 0}
        )
        trips = inst_data[inst]
        for rt in trips:
            reason = rt.get("exit_reason", "unknown") or "unknown"
            pnl = float(rt["pnl"])
            inst_reasons[reason]["count"] += 1
            inst_reasons[reason]["total_pnl"] += pnl
            if pnl > 0:
                inst_reasons[reason]["wins"] += 1

        inst_total = sum(v["count"] for v in inst_reasons.values())
        inst_rows = []

        def inst_key(r: str) -> float:
            return float(inst_reasons[r]["total_pnl"])

        for reason in sorted(inst_reasons, key=inst_key, reverse=True):
            s = inst_reasons[reason]
            inst_rows.append(
                {
                    "reason": reason,
                    "count": s["count"],
                    "pct": (s["count"] / inst_total) * 100 if inst_total else 0,
                    "total_pnl": s["total_pnl"],
                    "avg_pnl": s["total_pnl"] / s["count"] if s["count"] else 0,
                    "win_rate": (s["wins"] / s["count"]) * 100 if s["count"] else 0,
                }
            )
        exit_reasons["instruments"].append(
            {"short_name": inst.split(".")[2] if "." in inst else inst, "rows": inst_rows}
        )

    return exit_reasons


def generate_analysis_report(output_dir: str | Path) -> None:
    output_path = Path(output_dir)
    metrics_file = output_path / "metrics.csv"
    round_trips_file = output_path / "round_trips.csv"
    report_file = output_path / "analysis.md"
    template_file = "report-template.j2"

    metrics = _read_csv(metrics_file)
    _ensure_instrument_field(metrics)
    round_trips = _read_csv(round_trips_file)
    _ensure_instrument_field(round_trips)

    portfolio_metrics, instrument_metrics, sorted_instruments = (
        _prepare_overall_performance(metrics)
    )
    stats_table, cv_data = _prepare_stats_table(metrics)
    consistency_data = _prepare_consistency_data(round_trips)
    volatility = _classify_volatility(consistency_data)
    exposure_data, total_round_trips = _prepare_exposure_data(metrics)
    risk_adj_data = _prepare_risk_adj_data(metrics, consistency_data)
    similarity = _prepare_similarity(metrics, consistency_data)
    inst_data = _build_instrument_data(round_trips)
    evolution_data = _prepare_evolution_data(inst_data)
    insights = _prepare_insights(
        metrics, consistency_data, risk_adj_data, total_round_trips
    )
    win_loss_data, streak_data, hold_time_data, best_worst_trades, mfe_mae_data, long_short_data = (
        _prepare_additional_analysis(inst_data, round_trips)
    )
    monthly_data = _prepare_monthly_data(round_trips, inst_data)
    exit_reasons = _prepare_exit_reasons(round_trips, inst_data)

    context = {
        "portfolio_metrics": portfolio_metrics,
        "instrument_metrics": sorted_instruments,
        "stats_table": stats_table,
        "cv_data": cv_data,
        "consistency_data": consistency_data,
        "volatility": volatility,
        "exposure_data": exposure_data,
        "risk_adj_data": risk_adj_data,
        "similarity": similarity,
        "evolution_data": evolution_data,
        "insights": insights,
        "win_loss_data": win_loss_data,
        "streak_data": streak_data,
        "monthly_data": monthly_data,
        "hold_time_data": hold_time_data,
        "best_worst_trades": best_worst_trades,
        "mfe_mae_data": mfe_mae_data,
        "long_short_data": long_short_data,
        "exit_reasons": exit_reasons,
    }

    env = Environment(loader=FileSystemLoader(str(Path(__file__).parent)))
    template = env.get_template(template_file)
    rendered = template.render(**context)

    with open(report_file, "w") as f:
        f.write(rendered)
