"""The Python references behind two of the Rust backtester's golden fixtures.

``crates/tradedesk-backtest`` pins its indicators and its return and drawdown
statistics to fixtures computed in Python. These tests recompute every expected value
from the inputs in those fixtures, so their provenance is checked here, on every run:

- ``tests/fixtures/indicators/*.csv``: tradedesk's own indicators
  (``tradedesk.marketdata.indicators``) over the input bars in each file, bit for bit
  on CPython 3.12+ (within 1e-9 before it);
- ``tests/fixtures/metrics_golden.json``: pandas and numpy over each case's equity
  series, with the definitions of the crate README's "Metrics" section.
"""

import csv
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from tradedesk import Candle
from tradedesk.marketdata.indicators import (
    ADX,
    ATR,
    EMA,
    MACD,
    RSI,
    SMA,
    VWAP,
    BollingerBands,
)

FIXTURES = Path(__file__).resolve().parents[1] / "crates/tradedesk-backtest/tests/fixtures"
INDICATOR_FILES = sorted((FIXTURES / "indicators").glob("*.csv"))
PERIODS_PER_YEAR = 252
# The indicator fixtures were computed on CPython 3.12+, whose sum() is compensated
# (Neumaier); there they are reproduced bit for bit.
EXACT_SUM = sys.version_info >= (3, 12)


def _fmt(x: float | None) -> str:
    """Shortest round-tripping repr; empty for ``None`` (the fixtures' cell format)."""
    return "" if x is None else repr(float(x))


def test_the_indicator_fixtures_are_found() -> None:
    assert [p.name for p in INDICATOR_FILES] == [
        "synthetic_a.csv",
        "synthetic_b.csv",
        "usa500_1d.csv",
        "xauusd_15m.csv",
    ]


@pytest.mark.parametrize("path", INDICATOR_FILES, ids=lambda p: p.name)
def test_tradedesk_indicators_reproduce_the_rust_indicator_goldens(path: Path) -> None:
    lines = [line for line in path.read_text().splitlines() if not line.startswith("#")]
    header, *rows = list(csv.reader(lines))
    assert header[:6] == ["ts", "open", "high", "low", "close", "volume"]
    sma5, sma20, sma50 = SMA(5), SMA(20), SMA(50)
    ema12, ema50 = EMA(12), EMA(50)
    atr, adx, rsi = ATR(14), ADX(14), RSI(14)
    bb, macd = BollingerBands(20, 2.0), MACD(12, 26, 9)
    vwap_daily, vwap_h7 = VWAP(), VWAP(use_typical_price=False, reset_hour_utc=7)
    for row in rows:
        c = Candle(
            timestamp=row[0].replace("Z", "+00:00"),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        a, b, m = adx.update(c), bb.update(c), macd.update(c)
        outputs = [
            sma5.update(c),
            sma20.update(c),
            sma50.update(c),
            ema12.update(c),
            ema50.update(c),
            atr.update(c),
            a["adx"],
            a["plus_di"],
            a["minus_di"],
            b["middle"],
            b["upper"],
            b["lower"],
            b["std"],
            m["macd"],
            m["signal"],
            m["histogram"],
            rsi.update(c),
            vwap_daily.update(c),
            vwap_h7.update(c),
        ]
        label = f"{path.name} at {row[0]}"
        if EXACT_SUM:
            assert [_fmt(v) for v in outputs] == row[6:], label
        else:
            # Before 3.12, sum() is not compensated: the last bits may differ.
            want = [None if cell == "" else float(cell) for cell in row[6:]]
            assert outputs == pytest.approx(want, rel=1e-9, abs=1e-12), label


def _iso(ts: pd.Timestamp) -> str:
    return str(ts.strftime("%Y-%m-%d"))


def _drawdown(eq: "pd.Series[float]", series: "pd.Series[float]") -> dict[str, Any] | None:
    """The episode at the first minimum of ``series`` (amount or fraction)."""
    if not series.min() < 0.0:
        return None
    trough = series.idxmin()
    peak = eq.loc[:trough].idxmax()
    peak_equity = float(eq.loc[peak])
    after = eq.loc[trough:].iloc[1:]
    recovered = after[after >= peak_equity]
    cummax = eq.cummax()
    return {
        "amount": float(eq.loc[trough] - cummax.loc[trough]),
        "fraction": float(eq.loc[trough] / cummax.loc[trough] - 1.0),
        "peak": _iso(peak),
        "peak_equity": peak_equity,
        "trough": _iso(trough),
        "trough_equity": float(eq.loc[trough]),
        "recovery": _iso(recovered.index[0]) if len(recovered) else None,
    }


def _expected(case: dict[str, Any]) -> dict[str, Any]:
    dates = [pd.Timestamp(case["opening_date"]), *map(pd.Timestamp, case["dates"])]
    eq = pd.Series([case["starting_capital"], *case["equity"]], index=pd.DatetimeIndex(dates))
    r = eq.pct_change().iloc[1:]
    ex = r - case["risk_free_rate"] / PERIODS_PER_YEAR
    mean = float(ex.mean())
    std = float(ex.std(ddof=1))
    downside = math.sqrt(float((np.minimum(ex.to_numpy(), 0.0) ** 2).mean()))
    x = r.to_numpy()
    d = x - x.mean()
    m2 = float((d**2).mean())
    cummax = eq.cummax()
    return {
        "observations": int(len(r)),
        "mean_excess": mean,
        "std": std,
        "sharpe_daily": mean / std,
        "sharpe": math.sqrt(PERIODS_PER_YEAR) * mean / std,
        "volatility": float(r.std(ddof=1)) * math.sqrt(PERIODS_PER_YEAR),
        "sortino": math.sqrt(PERIODS_PER_YEAR) * mean / downside,
        "skewness": float((d**3).mean()) / m2**1.5,
        "kurtosis": float((d**4).mean()) / m2**2,
        "drawdown_by_amount": _drawdown(eq, eq - cummax),
        "drawdown_by_fraction": _drawdown(eq, eq / cummax - 1.0),
    }


def test_pandas_reproduces_the_rust_metrics_golden() -> None:
    golden = json.loads((FIXTURES / "metrics_golden.json").read_text())
    provenance = golden["provenance"]
    assert provenance["periods_per_year"] == PERIODS_PER_YEAR
    assert provenance["std_ddof"] == 1
    cases = golden["cases"]
    assert [c["name"] for c in cases] == [
        "random_walk",
        "random_walk_rf4",
        "distinct_episodes",
    ]
    for case in cases:
        got, want = _expected(case), case["expected"]
        assert got.keys() == want.keys(), case["name"]
        for key, value in want.items():
            label = f"{case['name']}.{key}"
            if isinstance(value, dict):
                assert got[key].keys() == value.keys(), label
                for field, v in value.items():
                    if isinstance(v, float):
                        assert got[key][field] == pytest.approx(v, rel=1e-12, abs=0.0), label
                    else:
                        assert got[key][field] == v, f"{label}.{field}"
            elif value is None or isinstance(value, int):
                assert got[key] == value, label
            else:
                assert got[key] == pytest.approx(value, rel=1e-12, abs=0.0), label
