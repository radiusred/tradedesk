"""Monthly performance breakdown data prep."""

from collections import defaultdict
from datetime import datetime
from types import SimpleNamespace


def _prepare_monthly_data(
    round_trips: list[dict[str, str]],
    inst_data: dict[str, list[dict[str, str]]],
) -> SimpleNamespace:
    monthly_pnl: defaultdict[str, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
    for rt in round_trips:
        exit_dt = datetime.fromisoformat(rt["exit_ts"].replace("Z", "+00:00"))
        month_key = exit_dt.strftime("%Y-%m")
        monthly_pnl[month_key][rt["instrument"]] += float(rt["pnl"])

    months = sorted(monthly_pnl.keys())
    instruments = sorted(inst_data.keys())
    short_names = {inst: inst.split(".")[2] if "." in inst else inst for inst in instruments}
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
