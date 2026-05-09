"""Section 6 (risk-adjusted performance) data prep."""

from typing import Any


def _prepare_risk_adj_data(
    metrics: list[dict[str, str]], consistency_data: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    risk_adj_data: list[dict[str, Any]] = []
    for m in metrics:
        inst = m.get("instrument")
        cd = next((c for c in consistency_data if c["instrument"] == inst), None)
        if cd:
            return_per_risk = (
                (float(m.get("final_equity", 0)) / float(cd["std_pnl"]))
                if float(cd["std_pnl"]) > 0
                else 0
            )
            dd_ratio = (
                (float(m.get("final_equity", 0)) / abs(float(m.get("max_dd", 0))))
                if float(m.get("max_dd", 0)) != 0
                else 0
            )
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
