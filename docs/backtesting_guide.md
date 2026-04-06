# Backtesting Guide

This guide covers running backtests with tradedesk using candle data loaded from
the Dukascopy cache or supplied in-memory.

By the end you will have:

- A working strategy
- A cache-backed backtest via `run_portfolio`
- A full recorded backtest via `run_backtest` (with metrics and trade output)

The same strategy runs live against a broker without modification.

---

## 1. Project Structure

```
my_backtest/
    strategy.py
    run_backtest.py
```

---

## 2. Dukascopy Cache Input

`BacktestClient.from_dukascopy_cache(...)` reads 1-minute candle files from a
Dukascopy cache directory and aggregates them to the period your strategy
subscribes to.

Required inputs:

- `cache_dir`: root cache directory
- `symbol`: cache symbol folder, for example `GBPUSD`
- `instrument`: instrument identifier used by your strategy
- `period`: target tradedesk period such as `5MINUTE` or `HOUR`
- `date_from` / `date_to`: inclusive date range
- `price_side`: `"bid"` or `"ask"` (`"bid"` is the default)

Example shared cache location used at Radius Red:

```text
/paperclip/tradedesk/marketdata/GBPUSD/2026/00/01_bid.csv.zst
```

---

## 3. Implement a Strategy

```python
# strategy.py
import logging
from tradedesk.strategy import BaseStrategy
from tradedesk.marketdata import CandleClosedEvent, ChartSubscription

log = logging.getLogger(__name__)


class SimpleMomentumStrategy(BaseStrategy):

    SUBSCRIPTIONS = [ChartSubscription("CS.D.GBPUSD.TODAY.IP", "5MINUTE")]

    async def on_candle_close(self, event: CandleClosedEvent) -> None:
        candle = event.candle

        if candle.close > candle.open:
            log.info("Bullish candle — would buy")
            # await self.client.place_market_order(event.instrument, "BUY", size=1.0)
        elif candle.close < candle.open:
            log.info("Bearish candle — would sell")
            # await self.client.place_market_order(event.instrument, "SELL", size=1.0)

        # Store candle in chart history (default behaviour)
        await super().on_candle_close(event)
```

Key points:

- Declare subscriptions via `SUBSCRIPTIONS` (or pass them to `__init__`).
- `on_candle_close` fires for each completed candle.
- Call `super().on_candle_close(event)` to keep chart history up to date.
- `self.client.place_market_order(instrument, "BUY" | "SELL", size)` places orders.

---

## 4. Simple Backtest with `run_portfolio`

Use `run_portfolio` for a quick, no-frills backtest that exercises the same
code path as live trading.

```python
# run_backtest.py
from datetime import date

from tradedesk import SimplePortfolio, run_portfolio
from tradedesk.execution.backtest.client import BacktestClient

from strategy import SimpleMomentumStrategy


def client_factory():
    return BacktestClient.from_dukascopy_cache(
        "/paperclip/tradedesk/marketdata",
        symbol="GBPUSD",
        instrument="CS.D.GBPUSD.TODAY.IP",
        period="5MINUTE",
        date_from=date(2025, 1, 1),
        date_to=date(2025, 1, 31),
        price_side="bid",
    )


run_portfolio(
    portfolio_factory=lambda c: SimplePortfolio(c, SimpleMomentumStrategy(c)),
    client_factory=client_factory,
    setup_logging=True,
)

# Inspect results
client = client_factory()  # create a fresh client to inspect (or capture it above)
```

To capture the client for inspection, use a closure:

```python
created = {}

def client_factory():
    c = BacktestClient.from_dukascopy_cache(
        "/paperclip/tradedesk/marketdata",
        symbol="GBPUSD",
        instrument="CS.D.GBPUSD.TODAY.IP",
        period="5MINUTE",
        date_from=date(2025, 1, 1),
        date_to=date(2025, 1, 31),
    )
    created["client"] = c
    return c

run_portfolio(
    portfolio_factory=lambda c: SimplePortfolio(c, SimpleMomentumStrategy(c)),
    client_factory=client_factory,
)

client = created["client"]
print(f"Trades:       {len(client.trades)}")
print(f"Positions:    {client.positions}")
print(f"Realised PnL: {client.realised_pnl}")
```

---

## 5. In-Memory Backtest

Supply candles directly without a CSV:

```python
from tradedesk.types import Candle

history = {
    ("CS.D.GBPUSD.TODAY.IP", "5MINUTE"): [
        Candle(timestamp="2025-01-01T00:00:00Z", open=1.25, high=1.26, low=1.24, close=1.255),
        Candle(timestamp="2025-01-01T00:05:00Z", open=1.255, high=1.27, low=1.25, close=1.265),
    ]
}

created = {}

def client_factory():
    c = BacktestClient.from_history(history)
    created["client"] = c
    return c

run_portfolio(
    portfolio_factory=lambda c: SimplePortfolio(c, SimpleMomentumStrategy(c)),
    client_factory=client_factory,
)
```

---

## 6. Full Backtest with Metrics (`run_backtest`)

`run_backtest` wraps `run_portfolio` and adds:

- Trade ledger written to CSV
- Equity curve tracking
- Excursion (MAE/MFE) computation
- A `Metrics` object returned with summary statistics

```python
import asyncio
from datetime import date
from pathlib import Path

from tradedesk import SimplePortfolio
from tradedesk.execution.backtest.runner import BacktestSpec, run_backtest

from strategy import SimpleMomentumStrategy


async def main():
    spec = BacktestSpec(
        instrument="CS.D.GBPUSD.TODAY.IP",
        period="5MINUTE",
        cache_dir=Path("/paperclip/tradedesk/marketdata"),
        symbol="GBPUSD",
        date_from=date(2025, 1, 1),
        date_to=date(2025, 1, 31),
        price_side="bid",
        half_spread_adjustment=0.5,  # add half the spread to BID-sourced candles
    )

    metrics = await run_backtest(
        spec=spec,
        out_dir=Path("output"),
        portfolio_factory=lambda c: SimplePortfolio(c, SimpleMomentumStrategy(c)),
    )

    print(f"Trades:          {metrics.trades}")
    print(f"Round trips:     {metrics.round_trips}")
    print(f"Win rate:        {metrics.win_rate:.1%}")
    print(f"Avg hold (min):  {metrics.avg_hold_minutes:.1f}")
    print(f"Final equity:    {metrics.final_equity:.2f}")
    print(f"Max drawdown:    {metrics.max_drawdown:.2f}")
    print(f"Profit factor:   {metrics.profit_factor:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
```

Output artefacts are written to `out_dir/`:

| File | Contents |
|------|----------|
| `trades.csv` | One row per fill: timestamp, direction, price, size |
| `equity.csv` | Equity curve snapshots |

---

## 7. What Happens Internally

When `run_portfolio` (or `portfolio.run()`) executes:

1. `SessionStartedEvent` fires — strategy `warmup()` is called
2. `SessionReadyEvent` fires — post-warmup checks run
3. BacktestStreamer replays candles in sequence
4. Each completed candle calls `portfolio._handle_event(CandleClosedEvent)`:
   - Publishes the event to the dispatcher (recording subscribers react here)
   - Calls `portfolio.on_candle_close(event)` → `strategy.on_candle_close(event)`
5. `BacktestClient.place_market_order` simulates fills at the candle's close price
6. `SessionEndedEvent` fires on completion

---

## 8. Switching to Live Trading

Replace the `client_factory` — the strategy and portfolio are unchanged:

```python
from tradedesk.execution.ig import IGClient

run_portfolio(
    portfolio_factory=lambda c: SimplePortfolio(c, SimpleMomentumStrategy(c)),
    client_factory=IGClient,
)
```

---

## License

Licensed under the Apache License, Version 2.0.
See: https://www.apache.org/licenses/LICENSE-2.0

Copyright 2026 [Radius Red Ltd.](https://github.com/radiusred) | [Contact](mailto:opensource@radiusred.uk)
```
