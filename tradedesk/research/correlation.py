"""Correlation gate calculator for candidate strategies vs LIVE sleeves.

Stage 2 of the research workflow rejects any candidate whose daily PnL is
≥0.6 correlated with an existing LIVE sleeve. This module automates the
matrix calculation that researchers previously did per candidate.

Inputs are flexible: accept reconstructed :class:`RoundTrip` objects from a
backtest, raw fill rows from a trade log CSV, or a pre-aggregated daily PnL
series. Output is a structured correlation result with the flagged sleeves
called out.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from tradedesk.recording import RoundTrip

DEFAULT_CORRELATION_THRESHOLD = 0.6
DEFAULT_MIN_OVERLAP_DAYS = 20


DailyPnl = dict[date, float]


@dataclass(frozen=True)
class CorrelationResult:
    """Output of :func:`correlation_gate`.

    Attributes:
        candidate: name of the candidate strategy under evaluation.
        threshold: correlation cut-off applied. The hard kill in §3.2 is 0.6.
        candidate_vs_sleeves: correlation of the candidate vs every LIVE
            sleeve, keyed by sleeve name. Sleeves with insufficient overlap
            are absent.
        sleeve_vs_sleeve: full pairwise matrix among the LIVE sleeves
            themselves (useful context, not part of the kill rule).
        flagged: ``(sleeve, correlation)`` tuples where the absolute
            correlation is at or above ``threshold``. The kill rule fires
            when this list is non-empty.
        skipped_sleeves: sleeves that did not have ``min_overlap_days``
            of overlapping trading days with the candidate.
        overlap_days: per-sleeve count of overlapping days actually used.
    """

    candidate: str
    threshold: float
    candidate_vs_sleeves: dict[str, float]
    sleeve_vs_sleeve: dict[str, dict[str, float]]
    flagged: list[tuple[str, float]]
    skipped_sleeves: list[str] = field(default_factory=list)
    overlap_days: dict[str, int] = field(default_factory=dict)

    @property
    def fails_gate(self) -> bool:
        """True when at least one sleeve crossed the kill threshold."""
        return bool(self.flagged)


def pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson product-moment correlation of two equal-length series.

    Returns ``0.0`` when either series has zero variance — there is no
    meaningful linear relationship to a constant series, and the kill rule
    interprets that as "not correlated enough to reject".
    """
    if len(xs) != len(ys):
        raise ValueError("series must have equal length")
    n = len(xs)
    if n < 2:
        return 0.0

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n

    cov = 0.0
    var_x = 0.0
    var_y = 0.0
    for x, y in zip(xs, ys):
        dx = x - mean_x
        dy = y - mean_y
        cov += dx * dy
        var_x += dx * dx
        var_y += dy * dy

    if var_x <= 0.0 or var_y <= 0.0:
        return 0.0
    return cov / math.sqrt(var_x * var_y)


def daily_pnl_from_round_trips(trips: Iterable[RoundTrip]) -> DailyPnl:
    """Aggregate :class:`RoundTrip` PnL by exit date.

    Mirrors the daily bucketing used by
    :func:`tradedesk.recording.compute_metrics` so correlations align with
    the Sharpe statistics quoted elsewhere.
    """
    daily: DailyPnl = {}
    for t in trips:
        day = _parse_date(t.exit_ts)
        daily[day] = daily.get(day, 0.0) + float(t.pnl)
    return daily


def daily_pnl_from_trade_rows(rows: Iterable[Mapping[str, Any]]) -> DailyPnl:
    """Aggregate trade-log fill rows into daily PnL.

    Accepts the same row shape as
    :func:`tradedesk.recording.metrics.round_trips_from_fills` — pairs of
    BUY/SELL fills per instrument with ``timestamp``, ``price``, ``size``,
    ``direction`` keys (``epic`` or ``instrument``).
    """
    from tradedesk.recording.metrics import round_trips_from_fills

    trips = round_trips_from_fills([dict(row) for row in rows])
    return daily_pnl_from_round_trips(trips)


def daily_pnl_from_csv(path: str | Path) -> DailyPnl:
    """Convenience loader: read a trade-log CSV and return daily PnL.

    Uses :func:`daily_pnl_from_trade_rows` so the schema is the same. CSV
    must include the trade-row columns (instrument or epic, direction,
    timestamp, price, size).
    """
    with Path(path).open("r", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = [dict(row) for row in reader]
    return daily_pnl_from_trade_rows(rows)


def correlation_gate(
    candidate_name: str,
    candidate_pnl: DailyPnl,
    live_sleeves: Mapping[str, DailyPnl],
    *,
    threshold: float = DEFAULT_CORRELATION_THRESHOLD,
    min_overlap_days: int = DEFAULT_MIN_OVERLAP_DAYS,
) -> CorrelationResult:
    """Compute the correlation matrix and flag sleeves above ``threshold``.

    Each pairwise correlation is computed only over the dates both series
    have a sample for. Days where either series is silent are skipped, not
    treated as zero — silent days carry no information about co-movement.

    Args:
        candidate_name: label for the candidate, used in the result.
        candidate_pnl: daily PnL series for the candidate strategy.
        live_sleeves: mapping of LIVE sleeve name to its daily PnL series.
        threshold: absolute correlation that triggers the kill flag.
            Defaults to 0.6 per the Stage 2 hard-kill rule in §3.2.
        min_overlap_days: minimum overlapping trading days required to
            compute a correlation. Sleeves below this floor are reported
            in ``skipped_sleeves`` and excluded from the kill check.

    Returns:
        :class:`CorrelationResult` with candidate-vs-sleeve correlations,
        the sleeve-vs-sleeve matrix, flagged sleeves, and overlap counts.
    """
    if not 0.0 < threshold <= 1.0:
        raise ValueError("threshold must be in (0, 1]")
    if min_overlap_days < 2:
        raise ValueError("min_overlap_days must be >= 2")

    candidate_vs: dict[str, float] = {}
    skipped: list[str] = []
    overlap_counts: dict[str, int] = {}

    for sleeve_name, sleeve_pnl in live_sleeves.items():
        xs, ys = _aligned(candidate_pnl, sleeve_pnl)
        overlap_counts[sleeve_name] = len(xs)
        if len(xs) < min_overlap_days:
            skipped.append(sleeve_name)
            continue
        candidate_vs[sleeve_name] = pearson(xs, ys)

    sleeve_matrix: dict[str, dict[str, float]] = {}
    sleeve_names = list(live_sleeves.keys())
    for i, a in enumerate(sleeve_names):
        sleeve_matrix.setdefault(a, {})
        for b in sleeve_names[i:]:
            if a == b:
                corr = 1.0
            else:
                xs, ys = _aligned(live_sleeves[a], live_sleeves[b])
                corr = pearson(xs, ys) if len(xs) >= min_overlap_days else float("nan")
            sleeve_matrix[a][b] = corr
            sleeve_matrix.setdefault(b, {})[a] = corr

    flagged = sorted(
        (
            (name, corr)
            for name, corr in candidate_vs.items()
            if abs(corr) >= threshold
        ),
        key=lambda item: -abs(item[1]),
    )

    return CorrelationResult(
        candidate=candidate_name,
        threshold=threshold,
        candidate_vs_sleeves=candidate_vs,
        sleeve_vs_sleeve=sleeve_matrix,
        flagged=flagged,
        skipped_sleeves=skipped,
        overlap_days=overlap_counts,
    )


def _aligned(a: DailyPnl, b: DailyPnl) -> tuple[list[float], list[float]]:
    """Return the two series aligned on their intersection of dates."""
    common = sorted(set(a.keys()) & set(b.keys()))
    return [a[d] for d in common], [b[d] for d in common]


def _parse_date(value: Any) -> date:
    """Parse a date or datetime-shaped value to a :class:`date`."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "/" and s[7] == "/":
        s = f"{s[0:4]}-{s[5:7]}-{s[8:]}"
    head = s[:10]
    return date.fromisoformat(head)
