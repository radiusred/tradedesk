# Writing your first strategy

This guide walks through a minimal-but-realistic strategy using two indicators and shows how to backtest it against a local CSV candle file.

It assumes you have:

- Installed `tradedesk`
- Optionally installed [`tradedesk-dukascopy`](github.com/radiusred/tradedesk-dukascopy) if you need backtest data
- A local candle CSV with columns: `timestamp,open,high,low,close,volume`
- Timestamps in UTC (ISO-8601 recommended)

---

## 1) Strategy outline

We will build a simple **mean-reversion** style strategy:

- Subscribe to a 5-minute candle stream for one instrument (`EPIC`)
- Maintain two indicators on that candle stream:
  - Williams %R (momentum / overbought-oversold)
  - MFI (money flow index, volume-weighted momentum)
- Enter long when both indicate “oversold”
- Exit the long when Williams %R indicates “recovered”

This is intentionally simplistic. The objective is to show framework wiring and backtesting.

---

## 2) Implementation

Create `my_strategy.py`:

```python
import logging

from tradedesk.strategy import BaseStrategy
from tradedesk.subscriptions import ChartSubscription
from tradedesk.marketdata import CandleClose, MarketData

from tradedesk.indicators.williams_r import WilliamsR
from tradedesk.indicators.mfi import MFI

log = logging.getLogger(__name__)


class MeanReversionStrategy(BaseStrategy):
    """
    Example strategy using two indicators on one candle stream.
    """

    CHART = ChartSubscription("EPIC", "5MINUTE")
    SUBSCRIPTIONS = [CHART]

    def __init__(self, client, subscriptions=None):
        super().__init__(client, subscriptions=subscriptions)

        # Create indicators
        self.wr = WilliamsR(period=14)
        self.mfi = MFI(period=14)

        # Register indicators against the chart subscription.
        # This enables warmup planning (and later priming from history where supported).
        self.register_indicator(self.CHART, self.wr)
        self.register_indicator(self.CHART, self.mfi)

        self.in_position = False

    async def on_price_update(self, market_data: MarketData) -> None:
        # Not used in this candle-only example.
        return

    async def on_candle_close(self, candle_close: CandleClose) -> None:
        candle = candle_close.candle

        wr_val = self.wr.update(candle)
        mfi_val = self.mfi.update(candle)

        # Not enough data yet for indicators to be "ready"
        if wr_val is None or mfi_val is None:
            await super().on_candle_close(candle_close)
            return

        # Entry: both indicate oversold
        if (not self.in_position) and wr_val < -80 and mfi_val < 20:
            log.info("ENTER long: wr=%.2f mfi=%.2f @ %s", wr_val, mfi_val, candle_close.ts)
            await self.client.place_market_order(
                epic=candle_close.epic,
                direction="BUY",
                size=1.0,
            )
            self.in_position = True

        # Exit: Williams %R has recovered
        if self.in_position and wr_val > -50:
            log.info("EXIT long: wr=%.2f mfi=%.2f @ %s", wr_val, mfi_val, candle_close.ts)
            await self.client.place_market_order(
                epic=candle_close.epic,
                direction="SELL",
                size=1.0,
            )
            self.in_position = False

        # Always allow the base class to store chart history by default.
        await super().on_candle_close(candle_close)
```

Notes:

- This example uses `ChartSubscription(epic, period)` to receive **completed candles** via `on_candle_close`.
- Indicators are updated explicitly inside `on_candle_close` and return `None` until ready.
- `register_indicator()` is important: it allows `tradedesk` to calculate required warmup bars.

---

## 3) Backtesting from a candle CSV

`tradedesk` includes a backtest client that can replay candle data from a CSV.

Create `run_backtest.py`:

```python
from tradedesk import run_strategies
from tradedesk.providers.backtest.client import BacktestClient

from my_strategy import MeanReversionStrategy


def client_factory():
    # CSV must contain: timestamp,open,high,low,close,volume
    # period string should match the strategy's ChartSubscription.
    return BacktestClient.from_csv("EURUSD_5MIN.csv", epic="EPIC", period="5MINUTE")


if __name__ == "__main__":
    # For a backtest, you usually want deterministic logging.
    run_strategies(
        strategy_specs=[MeanReversionStrategy],
        client_factory=client_factory,
        log_level="INFO",
    )
```

Run it:

```bash
python run_backtest.py
```

The backtest client tracks trades and realised PnL. For programmatic inspection, you can capture the created client in your factory (see `tests/test_backtest_csv.py` in the repo for an example pattern).

---

## 4) Next steps

Once the wiring is clear, typical improvements are:

- Risk controls (position sizing, max loss, stop logic)
- Multi-epic support (multiple `ChartSubscription`s)
- A structured “position state machine” instead of `self.in_position`
- Adding metrics reporting and CSV export of trades/equity

If you want to keep docs lightweight, this guide can remain “the” onboarding page and deeper topics can move into `docs/`.
