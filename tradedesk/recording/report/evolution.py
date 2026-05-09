"""Section 8 (time-based PnL evolution) data prep, plus per-instrument grouping helper."""

from collections import defaultdict
from datetime import datetime
from typing import Any


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

        evolution_data.append(
            {
                "instrument": inst,
                "final_pnl": cum_pnl[-1],
                "peak_pnl": max(running_max),
                "max_dd": max_dd,
                "dd_count": in_drawdown,
                "dd_pct": (in_drawdown / len(inst_trips) * 100),
            }
        )
    return evolution_data
