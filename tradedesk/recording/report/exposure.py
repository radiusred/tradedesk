"""Section 5 (exposure distribution) data prep."""

from typing import Any


def _prepare_exposure_data(metrics: list[dict[str, str]]) -> tuple[list[dict[str, Any]], int]:
    instrument_metrics = [m for m in metrics if m.get("instrument") != "PORTFOLIO"]
    total_round_trips = sum(int(m.get("round_trips", 0)) for m in instrument_metrics)
    exposure_data = []
    for m in sorted(instrument_metrics, key=lambda x: int(x.get("round_trips", 0)), reverse=True):
        pct_trades = (
            ((int(m.get("round_trips", 0)) / total_round_trips) * 100) if total_round_trips else 0
        )
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
