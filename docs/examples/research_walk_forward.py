"""Research walk-forward harness — minimal end-to-end example.

Wraps :func:`tradedesk.execution.backtest.run_backtest` in a 2yr-train /
6mo-OOS rolling loop and prints the aggregate IS/OOS Sharpe pair plus the
OOS/IS degradation ratio. Intended to replace the manual
"cut the date range up by hand" step in Stage 1 of the research workflow.

Usage::

    python docs/examples/research_walk_forward.py \\
        --cache /paperclip/tradedesk/marketdata \\
        --instrument EURUSD --period HOUR --symbol EURUSD \\
        --date-from 2018-01-01 --date-to 2024-01-01

The portfolio factory plugged in below is intentionally trivial — replace it
with your candidate strategy. The ``phase`` argument is ``"train"`` for the
in-sample slice and ``"oos"`` for the held-out slice; use it to fit
parameters on the train slice and freeze them for the OOS slice.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime
from pathlib import Path

from tradedesk.execution.backtest import BacktestClient
from tradedesk.portfolio.base import BasePortfolio
from tradedesk.research import (
    WalkForwardSpec,
    WalkForwardWindow,
    run_walk_forward,
)


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--instrument", default="EURUSD")
    p.add_argument("--period", default="HOUR")
    p.add_argument("--symbol", default="EURUSD")
    p.add_argument("--date-from", type=parse_date, required=True)
    p.add_argument("--date-to", type=parse_date, required=True)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    def factory(
        client: BacktestClient,
        window: WalkForwardWindow,  # noqa: ARG001
        phase: str,  # noqa: ARG001
    ) -> BasePortfolio:
        # Replace with your candidate. The window/phase arguments let you fit
        # on train and evaluate on oos with frozen params.
        from docs.examples.momentum_strategy import build_momentum_portfolio

        return build_momentum_portfolio(client)

    spec = WalkForwardSpec(
        portfolio_factory=factory,
        instrument=args.instrument,
        period=args.period,
        cache_dir=args.cache,
        symbol=args.symbol,
        date_from=args.date_from,
        date_to=args.date_to,
    )

    report = asyncio.run(run_walk_forward(spec=spec, out_dir=args.out))
    print(f"Windows: {len(report.windows)}")
    print(f"IS Sharpe: {report.is_sharpe:.3f}")
    print(f"OOS Sharpe: {report.oos_sharpe:.3f}")
    print(f"OOS/IS degradation: {report.degradation_ratio:.3f}")
    if report.drawdown_by_year:
        print("Per-year drawdown (chained OOS):")
        for year in sorted(report.drawdown_by_year):
            print(f"  {year}: {report.drawdown_by_year[year]:.2f}")


if __name__ == "__main__":
    main()
