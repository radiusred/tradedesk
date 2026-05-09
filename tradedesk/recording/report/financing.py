"""Overnight financing and admin costs summary."""

from typing import Any


def _prepare_financing_summary(
    round_trips: list[dict[str, str]],
) -> dict[str, Any]:
    """Aggregate overnight financing and admin costs from round trips."""
    total_financing = 0.0
    total_admin = 0.0
    by_instrument: dict[str, dict[str, float]] = {}

    for rt in round_trips:
        fc = float(rt.get("financing_cost") or 0)
        ac = float(rt.get("admin_cost") or 0)
        total_financing += fc
        total_admin += ac
        inst = rt.get("instrument", "")
        if inst:
            entry = by_instrument.setdefault(inst, {"financing": 0.0, "admin": 0.0})
            entry["financing"] += fc
            entry["admin"] += ac

    has_data = total_financing != 0 or total_admin != 0
    instruments = [
        {"instrument": inst, "financing": v["financing"], "admin": v["admin"]}
        for inst, v in sorted(by_instrument.items())
        if v["financing"] or v["admin"]
    ]

    return {
        "available": has_data,
        "total_financing": total_financing,
        "total_admin": total_admin,
        "total": total_financing + total_admin,
        "instruments": instruments,
    }
