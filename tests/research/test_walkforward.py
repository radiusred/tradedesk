"""Tests for the walk-forward harness."""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tradedesk.research import (
    WalkForwardSpec,
    WalkForwardWindow,
    run_walk_forward,
    walk_forward_windows,
)


def _spec(
    *,
    cache_dir: Path,
    factory,
    date_from: date = date(2018, 1, 1),
    date_to: date = date(2022, 1, 1),
    train_window: timedelta = timedelta(days=730),
    oos_window: timedelta = timedelta(days=183),
) -> WalkForwardSpec:
    return WalkForwardSpec(
        portfolio_factory=factory,
        instrument="EURUSD",
        period="HOUR",
        cache_dir=cache_dir,
        symbol="EURUSD",
        date_from=date_from,
        date_to=date_to,
        train_window=train_window,
        oos_window=oos_window,
    )


def test_walk_forward_windows_rolling_step():
    windows = walk_forward_windows(
        date_from=date(2020, 1, 1),
        date_to=date(2024, 1, 1),
        train_window=timedelta(days=730),
        oos_window=timedelta(days=183),
    )
    # train+oos is 913 days; we step by oos_window (183 days). 4 yrs ≈ 1461d.
    # Windows fit while train_start + 913 <= date_to.
    assert windows
    assert windows[0] == WalkForwardWindow(
        train_from=date(2020, 1, 1),
        train_to=date(2021, 12, 31),
        oos_from=date(2021, 12, 31),
        oos_to=date(2022, 7, 2),
    )
    # Subsequent windows step by oos_window.
    assert windows[1].train_from == date(2020, 1, 1) + timedelta(days=183)
    # Final OOS end never overruns date_to.
    assert windows[-1].oos_to <= date(2024, 1, 1)
    # OOS slices are contiguous (no gap, no overlap).
    for prev, nxt in zip(windows, windows[1:]):
        assert nxt.train_from == prev.train_from + timedelta(days=183)


def test_walk_forward_windows_drops_partial_oos():
    windows = walk_forward_windows(
        date_from=date(2020, 1, 1),
        date_to=date(2022, 6, 1),
        train_window=timedelta(days=730),
        oos_window=timedelta(days=183),
    )
    # Only one full window fits: train ends 2021-12-31, oos ends 2022-07-02 — but
    # 2022-07-02 > 2022-06-01, so even the first window is dropped.
    assert windows == []


def test_walk_forward_windows_validation():
    with pytest.raises(ValueError):
        walk_forward_windows(
            date_from=date(2020, 1, 1),
            date_to=date(2020, 1, 1),
        )
    with pytest.raises(ValueError):
        walk_forward_windows(
            date_from=date(2020, 1, 1),
            date_to=date(2021, 1, 1),
            train_window=timedelta(days=0),
        )
    with pytest.raises(ValueError):
        walk_forward_windows(
            date_from=date(2020, 1, 1),
            date_to=date(2021, 1, 1),
            oos_window=timedelta(days=-1),
        )


@pytest.mark.asyncio
async def test_run_walk_forward_runs_each_window_train_then_oos(tmp_path):
    """Each window produces exactly one train and one OOS run, and the
    portfolio factory sees the right phase tag each time."""
    seen: list[tuple[str, date, date]] = []

    def factory(_client, window, phase):
        seen.append((phase, window.train_from, window.oos_to))
        return MagicMock()

    sharpes = iter([1.5, 0.8, 1.4, 0.6])  # IS/OOS alternated across two windows

    async def fake_run(*, spec, out_dir, portfolio_factory):  # noqa: ARG001
        portfolio_factory(MagicMock())  # exercise factory & record phase
        return MagicMock(sharpe_ratio=next(sharpes), max_drawdown=-100.0)

    with patch("tradedesk.research.walkforward.run_backtest", new=fake_run):
        report = await run_walk_forward(
            spec=_spec(
                cache_dir=tmp_path,
                factory=factory,
                date_from=date(2018, 1, 1),
                date_to=date(2021, 1, 1),
                train_window=timedelta(days=730),
                oos_window=timedelta(days=183),
            ),
            out_dir=tmp_path,
        )

    # Two windows, each with a train and an OOS phase.
    phases = [phase for phase, *_ in seen]
    assert phases.count("train") == 2
    assert phases.count("oos") == 2
    # Train always precedes OOS for the same window.
    assert phases == ["train", "oos", "train", "oos"]

    # Aggregates: IS = mean(1.5, 1.4) = 1.45, OOS = mean(0.8, 0.6) = 0.7.
    assert report.is_sharpe == pytest.approx(1.45)
    assert report.oos_sharpe == pytest.approx(0.7)
    assert report.degradation_ratio == pytest.approx(0.7 / 1.45)
    assert len(report.windows) == 2


@pytest.mark.asyncio
async def test_run_walk_forward_zero_is_sharpe_yields_zero_degradation(tmp_path):
    sharpes = iter([0.0, 0.5])

    async def fake_run(*, spec, out_dir, portfolio_factory):  # noqa: ARG001
        portfolio_factory(MagicMock())
        return MagicMock(sharpe_ratio=next(sharpes), max_drawdown=0.0)

    with patch("tradedesk.research.walkforward.run_backtest", new=fake_run):
        report = await run_walk_forward(
            spec=_spec(
                cache_dir=tmp_path,
                factory=lambda *_: MagicMock(),
                date_from=date(2018, 1, 1),
                date_to=date(2020, 7, 5),  # exactly one window fits
            ),
            out_dir=tmp_path,
        )
    assert report.degradation_ratio == 0.0


@pytest.mark.asyncio
async def test_run_walk_forward_drawdown_by_year_from_equity_csv(tmp_path):
    """When out_dir is provided, per-year drawdown is computed from the
    chained OOS equity CSVs written by run_backtest."""
    out_dir = tmp_path / "wf"

    async def fake_run(*, spec, out_dir, portfolio_factory):  # noqa: ARG001
        portfolio_factory(MagicMock())
        # Drop a synthetic equity.csv into the OOS dir of each call.
        equity_path = Path(out_dir) / "equity.csv"
        equity_path.parent.mkdir(parents=True, exist_ok=True)
        with equity_path.open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["timestamp", "equity"])
            # Ramp up to peak then a drawdown.
            year = spec.date_from.year
            writer.writerow([f"{year}-03-01T00:00:00Z", "100"])
            writer.writerow([f"{year}-06-01T00:00:00Z", "120"])
            writer.writerow([f"{year}-09-01T00:00:00Z", "90"])  # dd = -30
        return MagicMock(sharpe_ratio=1.0, max_drawdown=-30.0)

    with patch("tradedesk.research.walkforward.run_backtest", new=fake_run):
        report = await run_walk_forward(
            spec=_spec(
                cache_dir=tmp_path,
                factory=lambda *_: MagicMock(),
                date_from=date(2018, 1, 1),
                date_to=date(2021, 1, 1),
                train_window=timedelta(days=730),
                oos_window=timedelta(days=183),
            ),
            out_dir=out_dir,
        )

    assert report.drawdown_by_year
    for mdd in report.drawdown_by_year.values():
        assert mdd <= 0.0


@pytest.mark.asyncio
async def test_run_walk_forward_no_windows_raises(tmp_path):
    with pytest.raises(ValueError, match="No walk-forward windows"):
        await run_walk_forward(
            spec=_spec(
                cache_dir=tmp_path,
                factory=lambda *_: MagicMock(),
                date_from=date(2024, 1, 1),
                date_to=date(2024, 6, 1),
            ),
            out_dir=tmp_path,
        )


@pytest.mark.asyncio
async def test_run_walk_forward_in_memory_drawdown_uses_window_metric(tmp_path):
    """Without out_dir, drawdown_by_year falls back to per-window OOS max_drawdown."""
    # Two windows x (train + oos) = 4 backtest calls. OOS drawdowns at indices
    # 1 and 3 are what the per-year fallback path consumes.
    sharpes = iter([1.5, 1.0, 1.3, 0.5])
    drawdowns = iter([-50.0, -100.0, -75.0, -250.0])

    async def fake_run(*, spec, out_dir, portfolio_factory):  # noqa: ARG001
        portfolio_factory(MagicMock())
        return MagicMock(sharpe_ratio=next(sharpes), max_drawdown=next(drawdowns))

    with patch("tradedesk.research.walkforward.run_backtest", new=fake_run):
        report = await run_walk_forward(
            spec=_spec(
                cache_dir=tmp_path,
                factory=lambda *_: MagicMock(),
                date_from=date(2018, 1, 1),
                date_to=date(2021, 1, 1),
                train_window=timedelta(days=730),
                oos_window=timedelta(days=183),
            ),
            out_dir=None,
        )

    # Two windows with drawdowns -100 and -250 attributed to OOS midpoint years.
    assert sum(1 for v in report.drawdown_by_year.values() if v < 0) >= 1
    # Aggregate drawdown depths preserved.
    assert min(report.drawdown_by_year.values()) <= -100.0
