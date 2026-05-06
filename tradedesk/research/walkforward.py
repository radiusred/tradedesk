"""Walk-forward harness around :func:`tradedesk.execution.backtest.run_backtest`.

Automates the 2yr-train / 6mo-OOS loop used in the manual research workflow.
For each window, the harness runs an in-sample (IS) backtest over the train
slice and an out-of-sample (OOS) backtest over the OOS slice, then aggregates
IS / OOS Sharpe, the OOS/IS degradation ratio, and a per-calendar-year
drawdown table built from chained OOS equity curves.

The window-aware portfolio factory lets a caller plug in a fit-on-train,
evaluate-on-OOS strategy. Phase-naive factories that ignore the window
argument behave the same as a vanilla single-shot backtest, just sliced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

from tradedesk.execution.backtest import BacktestSpec, TransactionCosts, run_backtest
from tradedesk.recording import Metrics

if TYPE_CHECKING:
    from tradedesk.execution.backtest import BacktestClient
    from tradedesk.portfolio.base import BasePortfolio


log = logging.getLogger(__name__)


WindowPhase = Literal["train", "oos"]
WindowedPortfolioFactory = Callable[
    ["BacktestClient", "WalkForwardWindow", WindowPhase],
    "BasePortfolio",
]


DEFAULT_TRAIN_WINDOW = timedelta(days=730)
DEFAULT_OOS_WINDOW = timedelta(days=183)


@dataclass(frozen=True)
class WalkForwardWindow:
    """A single train + OOS window in the walk-forward sequence."""

    train_from: date
    train_to: date
    oos_from: date
    oos_to: date


@dataclass(frozen=True)
class WindowResult:
    """Per-window IS and OOS metrics."""

    window: WalkForwardWindow
    is_metrics: Metrics
    oos_metrics: Metrics


@dataclass(frozen=True)
class WalkForwardReport:
    """Aggregated walk-forward report.

    Attributes:
        windows: per-window IS and OOS metrics in chronological order.
        is_sharpe: mean of the per-window IS Sharpe ratios.
        oos_sharpe: mean of the per-window OOS Sharpe ratios.
        degradation_ratio: ``oos_sharpe / is_sharpe`` when ``is_sharpe`` is
            non-zero, else ``0.0``. A value below 1.0 indicates loss of
            edge out-of-sample.
        drawdown_by_year: maximum drawdown observed per calendar year on the
            chained OOS equity curve. Years with no OOS exposure are absent.
    """

    windows: list[WindowResult]
    is_sharpe: float
    oos_sharpe: float
    degradation_ratio: float
    drawdown_by_year: dict[int, float]


@dataclass
class WalkForwardSpec:
    """Configuration for :func:`run_walk_forward`.

    The harness wraps the existing single-instrument event-driven backtest.
    Pass a window-aware factory if your strategy needs to fit on the train
    slice and evaluate frozen parameters on the OOS slice; phase-naive
    factories are also accepted via :func:`run_walk_forward`'s overload.
    """

    portfolio_factory: WindowedPortfolioFactory
    instrument: str
    period: str
    cache_dir: Path
    symbol: str
    date_from: date
    date_to: date
    train_window: timedelta = DEFAULT_TRAIN_WINDOW
    oos_window: timedelta = DEFAULT_OOS_WINDOW
    price_side: str = "bid"
    half_spread_adjustment: float = 0.0
    transaction_costs: TransactionCosts = field(default_factory=TransactionCosts)


def walk_forward_windows(
    *,
    date_from: date,
    date_to: date,
    train_window: timedelta = DEFAULT_TRAIN_WINDOW,
    oos_window: timedelta = DEFAULT_OOS_WINDOW,
) -> list[WalkForwardWindow]:
    """Slice ``[date_from, date_to]`` into chronological train + OOS windows.

    Each window is ``train_window`` long, immediately followed by
    ``oos_window``. Windows step forward by ``oos_window`` (rolling OOS).
    The final window is dropped if it does not have a full OOS slice
    inside ``date_to`` — partial OOS slices skew Sharpe statistics and are
    not what the manual research workflow does.
    """
    if train_window <= timedelta(0):
        raise ValueError("train_window must be positive")
    if oos_window <= timedelta(0):
        raise ValueError("oos_window must be positive")
    if date_to <= date_from:
        raise ValueError("date_to must be after date_from")

    windows: list[WalkForwardWindow] = []
    train_start = date_from
    while True:
        train_end = train_start + train_window
        oos_end = train_end + oos_window
        if oos_end > date_to:
            break
        windows.append(
            WalkForwardWindow(
                train_from=train_start,
                train_to=train_end,
                oos_from=train_end,
                oos_to=oos_end,
            )
        )
        train_start = train_start + oos_window
    return windows


async def run_walk_forward(
    *,
    spec: WalkForwardSpec,
    out_dir: Path | None = None,
) -> WalkForwardReport:
    """Run the walk-forward sequence and return aggregated metrics.

    For every window:
      1. Run an IS backtest with phase=``"train"``.
      2. Run an OOS backtest with phase=``"oos"``.
      3. Capture both ``Metrics`` objects.

    OOS equity curves are chained together to produce a single per-year
    drawdown table. ``out_dir`` is optional; when provided each window's
    artefacts land under ``out_dir/{train_from}_{oos_to}/{is|oos}/``.

    Args:
        spec: walk-forward configuration.
        out_dir: optional root directory to persist per-window backtest
            artefacts (trades, equity, analysis). If ``None``, results live
            only in memory.

    Returns:
        :class:`WalkForwardReport` with per-window detail and aggregates.
    """
    windows = walk_forward_windows(
        date_from=spec.date_from,
        date_to=spec.date_to,
        train_window=spec.train_window,
        oos_window=spec.oos_window,
    )
    if not windows:
        raise ValueError(
            "No walk-forward windows fit between "
            f"{spec.date_from} and {spec.date_to} with train={spec.train_window} "
            f"oos={spec.oos_window}"
        )

    log.info(
        "Walk-forward: %d windows from %s to %s (train=%s, oos=%s)",
        len(windows),
        spec.date_from,
        spec.date_to,
        spec.train_window,
        spec.oos_window,
    )

    results: list[WindowResult] = []
    for window in windows:
        is_metrics = await _run_phase(spec, window, phase="train", out_dir=out_dir)
        oos_metrics = await _run_phase(spec, window, phase="oos", out_dir=out_dir)
        results.append(
            WindowResult(window=window, is_metrics=is_metrics, oos_metrics=oos_metrics)
        )

    return _build_report(results, out_dir=out_dir)


async def _run_phase(
    spec: WalkForwardSpec,
    window: WalkForwardWindow,
    *,
    phase: WindowPhase,
    out_dir: Path | None,
) -> Metrics:
    """Run a single phase (train or oos) of one walk-forward window."""
    if phase == "train":
        date_from = window.train_from
        # ``run_backtest`` treats date_to as inclusive; subtract a day so the
        # IS slice does not overlap the OOS slice.
        date_to = window.train_to - timedelta(days=1)
    else:
        date_from = window.oos_from
        date_to = window.oos_to - timedelta(days=1)

    backtest_spec = BacktestSpec(
        instrument=spec.instrument,
        period=spec.period,
        cache_dir=spec.cache_dir,
        symbol=spec.symbol,
        date_from=date_from,
        date_to=date_to,
        price_side=spec.price_side,
        half_spread_adjustment=spec.half_spread_adjustment,
        transaction_costs=spec.transaction_costs,
    )

    phase_out_dir = _phase_dir(out_dir, window, phase)

    def factory(client: "BacktestClient") -> "BasePortfolio":
        return spec.portfolio_factory(client, window, phase)

    return await run_backtest(
        spec=backtest_spec, out_dir=phase_out_dir, portfolio_factory=factory
    )


def _phase_dir(
    out_dir: Path | None, window: WalkForwardWindow, phase: WindowPhase
) -> Path:
    """Return the per-window per-phase output directory.

    ``run_backtest`` writes artefacts via the recording subscriber unconditionally,
    so we must always supply a real path. When ``out_dir`` is None we use a
    temp directory under the system tmp root.
    """
    if out_dir is None:
        import tempfile

        return Path(tempfile.mkdtemp(prefix=f"walkforward-{phase}-"))
    sub = f"{window.train_from.isoformat()}_{window.oos_to.isoformat()}"
    target = out_dir / sub / phase
    target.mkdir(parents=True, exist_ok=True)
    return target


def _build_report(
    results: list[WindowResult], *, out_dir: Path | None
) -> WalkForwardReport:
    """Aggregate per-window metrics into the final report."""
    is_sharpes = [r.is_metrics.sharpe_ratio for r in results]
    oos_sharpes = [r.oos_metrics.sharpe_ratio for r in results]

    is_sharpe = sum(is_sharpes) / len(is_sharpes)
    oos_sharpe = sum(oos_sharpes) / len(oos_sharpes)
    degradation = (oos_sharpe / is_sharpe) if abs(is_sharpe) > 1e-12 else 0.0

    drawdown_by_year = _drawdown_by_year_from_oos(results, out_dir=out_dir)

    return WalkForwardReport(
        windows=results,
        is_sharpe=is_sharpe,
        oos_sharpe=oos_sharpe,
        degradation_ratio=degradation,
        drawdown_by_year=drawdown_by_year,
    )


def _drawdown_by_year_from_oos(
    results: list[WindowResult], *, out_dir: Path | None
) -> dict[int, float]:
    """Build a per-calendar-year max drawdown table from OOS equity curves.

    When ``out_dir`` is provided, each window's OOS ``equity.csv`` contains
    timestamped equity samples; we bucket those by calendar year and compute
    the running peak/drawdown within each bucket.

    Without an ``out_dir`` we fall back to the per-window OOS ``max_drawdown``
    metric, attributing it to the year that contains the OOS midpoint. This
    is approximate but keeps the harness usable for in-memory runs.
    """
    if out_dir is None:
        per_year: dict[int, float] = {}
        for r in results:
            mid = r.window.oos_from + (r.window.oos_to - r.window.oos_from) / 2
            year = mid.year
            mdd = float(r.oos_metrics.max_drawdown)
            per_year[year] = min(per_year.get(year, 0.0), mdd)
        return per_year

    samples_by_year: dict[int, list[float]] = {}
    for r in results:
        oos_dir = _phase_dir(out_dir, r.window, "oos")
        equity_csv = oos_dir / "equity.csv"
        if not equity_csv.exists():
            continue
        for ts, equity in _read_equity_csv(equity_csv):
            samples_by_year.setdefault(ts.year, []).append(equity)

    drawdowns: dict[int, float] = {}
    for year, samples in samples_by_year.items():
        peak = float("-inf")
        mdd = 0.0
        for x in samples:
            peak = max(peak, x)
            mdd = min(mdd, x - peak)
        drawdowns[year] = mdd
    return drawdowns


def _read_equity_csv(path: Path) -> list[tuple[datetime, float]]:
    """Read equity samples from a backtest ``equity.csv``.

    Tolerant to the same timestamp formats accepted by
    :func:`tradedesk.recording.metrics._parse_ts` (Z suffix, space separator,
    yyyy/mm/dd dates).
    """
    import csv

    rows: list[tuple[datetime, float]] = []
    with path.open("r", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            ts_raw = (row.get("timestamp") or "").strip()
            eq_raw = (row.get("equity") or "").strip()
            if not ts_raw or not eq_raw:
                continue
            ts = _parse_ts(ts_raw)
            try:
                rows.append((ts, float(eq_raw)))
            except ValueError:
                continue
    return rows


def _parse_ts(ts: str) -> datetime:
    """Parse a backtest equity timestamp into a timezone-naive ``datetime``."""
    s = ts.strip()
    if len(s) >= 10 and s[4] == "/" and s[7] == "/":
        s = f"{s[0:4]}-{s[5:7]}-{s[8:]}"
    s = s.replace("Z", "+00:00")
    s = s.replace(" ", "T", 1)
    parsed = datetime.fromisoformat(s)
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed
