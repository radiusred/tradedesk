"""Section 7 (similarity matrix) data prep."""

from typing import Any

from .stats import euclidean_distance, standardize


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
            dist = euclidean_distance(profile_metrics[inst1], profile_metrics[inst2], metric_keys)
            if dist < min_dist:
                min_dist = dist
                min_pair = (inst1, inst2)
            if dist > max_dist:
                max_dist = dist
                max_pair = (inst1, inst2)

    matrix_headers = [inst.split(".")[2] if "." in inst else inst for inst in instruments]
    matrix_rows: list[dict[str, Any]] = []
    for inst1 in instruments:
        dists: list[str] = []
        for inst2 in instruments:
            dist = (
                euclidean_distance(profile_metrics[inst1], profile_metrics[inst2], metric_keys)
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
