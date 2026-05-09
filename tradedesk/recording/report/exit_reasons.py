"""Exit reason breakdown data prep."""

from collections import defaultdict
from typing import Any


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
