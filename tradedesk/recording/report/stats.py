"""Pure math helpers used across report sections."""

import math


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


def euclidean_distance(p1: dict[str, float], p2: dict[str, float], metric_keys: list[str]) -> float:
    """Calculate Euclidean distance between two performance profiles."""
    z_keys = [f"{key}_z" for key in metric_keys]
    return math.sqrt(sum((p1[zk] - p2[zk]) ** 2 for zk in z_keys if zk in p1 and zk in p2))
