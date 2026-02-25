# Backtesting Guide

This guide walks through building and running a minimal but functional
backtest using tradedesk.

By the end, you will have:

-   A working strategy
-   A CSV-driven backtest
-   Portfolio + recording enabled
-   A complete runnable script

The same strategy will later run live without modification.

------------------------------------------------------------------------

# 1. Project Structure

Create a minimal structure:

    my_backtest/
        strategy.py
        run_backtest.py
        eurusd_ticks.csv

------------------------------------------------------------------------

# 2. Example Market Data (CSV)

Your CSV should contain timestamped tick data:

    timestamp,bid,ask
    2024-01-01T00:00:00Z,1.1000,1.1002
    2024-01-01T00:00:01Z,1.1001,1.1003

Ticks will be aggregated into candles internally.

------------------------------------------------------------------------

# 3. Implement a Simple Strategy

Create `strategy.py`:

``` python
from tradedesk.strategy.base import Strategy
from tradedesk.types import Instrument


class SimpleMomentumStrategy(Strategy):

    def __init__(self, instrument: Instrument):
        super().__init__(instrument)

    def on_candle_update(self, candle):
        if candle.close > candle.open:
            self.buy(size=1)
        elif candle.close < candle.open:
            self.sell(size=1)
```

Key concepts:

-   `on_candle_update` is your primary decision hook
-   `buy()` and `sell()` emit order requests (they do not directly place
    orders)
-   Execution is handled by the backtest client

------------------------------------------------------------------------

# 4. Create the Backtest Runner

Create `run_backtest.py`:

``` python
from tradedesk.execution.backtest.runner import BacktestRunner
from tradedesk.execution.backtest.client import BacktestClient
from tradedesk.execution.backtest.streamer import CSVBacktestStreamer

from tradedesk.portfolio.runner import PortfolioRunner
from tradedesk.portfolio.config import PortfolioConfig

from tradedesk.recording.recorders import DefaultRecorders

from tradedesk.types import Instrument

from strategy import SimpleMomentumStrategy


def main():

    instrument = Instrument("EURUSD")

    strategy = SimpleMomentumStrategy(instrument)

    streamer = CSVBacktestStreamer(
        instrument=instrument,
        csv_path="eurusd_ticks.csv",
    )

    execution_client = BacktestClient()

    portfolio_config = PortfolioConfig(
        starting_cash=100_000
    )

    portfolio_runner = PortfolioRunner(
        config=portfolio_config
    )

    recorders = DefaultRecorders()

    runner = BacktestRunner(
        strategy=strategy,
        streamer=streamer,
        execution_client=execution_client,
        portfolio_runner=portfolio_runner,
        recorders=recorders,
    )

    runner.run()

    print("Final equity:", portfolio_runner.equity)


if __name__ == "__main__":
    main()
```

------------------------------------------------------------------------

# 5. What Happens Internally

During `runner.run()`:

1.  CSV streamer emits ticks
2.  Aggregation produces candles
3.  Strategy receives `on_candle_update`
4.  Strategy emits order events
5.  Backtest client simulates fills
6.  Portfolio updates positions
7.  Recording subsystem tracks trades and equity

All interactions occur through events.

------------------------------------------------------------------------

# 6. Determinism

Backtests are:

-   Single-threaded
-   Sequential
-   Deterministic given identical input data

This ensures reproducibility.

------------------------------------------------------------------------

# 7. Adding Indicators

``` python
from tradedesk.marketdata.indicators.sma import SMA

self.sma = SMA(period=20)

def on_candle_update(self, candle):
    self.sma.update(candle.close)

    if self.sma.is_ready:
        if candle.close > self.sma.value:
            self.buy(size=1)
```

Indicators are updated explicitly inside your strategy.

------------------------------------------------------------------------

# 8. Accessing Recording Data

After the run completes, recorders contain:

-   Trade history
-   Equity curve
-   Performance metrics

You can generate reports or export metrics as needed.

------------------------------------------------------------------------

# 9. Moving to Live Trading

To switch to live:

-   Replace BacktestRunner with broker runner
-   Replace BacktestClient with broker client
-   Replace CSV streamer with broker price streamer

Your strategy remains unchanged.

------------------------------------------------------------------------

You now have a complete, runnable backtest using tradedesk.
