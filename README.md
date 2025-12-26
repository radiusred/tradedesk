# tradedesk

`tradedesk` is a **very early-stage Python library for running automated trading strategies against IG**, with a focus on:

- Real-time price streaming
- Simple, explicit strategy code
- Minimal abstractions
- Full control (and responsibility) in user-written logic

It is designed for **traders who are comfortable with markets and risk**, and who want a lightweight Python framework rather than a full trading platform.

⚠️ **This project is experimental and unstable. APIs, behaviour, and structure may change significantly.**

---

## ⚠️ Important Disclaimer

**All trading involves risk.**  
Using this library can result in **financial loss**, including loss of all capital.

- You are solely responsible for any trades placed
- The authors provide **no guarantees, warranties, or safeguards**
- This library does **not** implement risk management, capital controls, or safety checks
- Always test on **IG DEMO accounts** before considering live usage

If you do not understand the risks of automated trading, **do not use this library**.

---

## What this project is (and is not)

### It *is*
- A framework for:
  - Subscribing to live IG prices via Lightstreamer
  - Running one or more async trading strategies
  - Placing trades via the IG REST API
- A way to structure and run **your own trading logic**

### It is *not*
- A trading bot that makes decisions for you
- A backtesting or simulation engine (planned for a future version)
- A portfolio or risk management system
- A stable or production-ready framework

---

## Installation

Eventually this package will be published on PyPI. For now, install directly from source:

```bash
pip install git+https://github.com/davison/tradedesk
```

Or clone the repository and install locally:

```bash
git clone https://github.com/davison/tradedesk
cd tradedesk
pip install -e .
```

---

## Configuration

`tradedesk` is configured via environment variables:

```bash
export IG_API_KEY="your_api_key"
export IG_USERNAME="your_username"
export IG_PASSWORD="your_password"
export IG_ENVIRONMENT="DEMO"  # or LIVE
export LOG_LEVEL="INFO"
```

**Strongly recommended:** start with `IG_ENVIRONMENT=DEMO`.

---

## Core concepts

### Strategies
A strategy is a Python class that:

- Subclasses `BaseStrategy`
- Declares which instruments (EPICs) it wants to subscribe to
- Responds to live price updates

Each strategy runs independently and receives streaming price data.

### Price streaming
Prices are received via IG’s Lightstreamer service and delivered to your strategy as updates.

Your strategy code must be:
- Fast
- Non-blocking
- Defensive against unexpected data

### Trading
Strategies may place trades using the provided `IGClient`.

> In the example strategies, **actual trading calls are commented out** for safety.

---


## Strategy Authoring

A strategy in **tradedesk** is a subclass of `BaseStrategy`. The base class provides:

- Lightstreamer streaming for real-time MARKET + CHART feeds
- REST polling fallback for MARKET feeds
- Candle history storage via `ChartHistory` for CHART subscriptions

### 1) Declare subscriptions (required)

Strategies declare the feeds they need using `SUBSCRIPTIONS`. Each item must be a `Subscription` object such as:

- `MarketSubscription(epic)` for tick-level bid/offer updates
- `ChartSubscription(epic, period)` for completed OHLCV candles

```python
from tradedesk.strategy import BaseStrategy
from tradedesk.subscriptions import MarketSubscription, ChartSubscription

class MyStrategy(BaseStrategy):
    SUBSCRIPTIONS = [
        MarketSubscription("CS.D.GBPUSD.TODAY.IP"),
        ChartSubscription("CS.D.GBPUSD.TODAY.IP", "5MINUTE"),
    ]
```

### 2) Implement your handlers

#### Tick updates (MARKET)

Implement `on_price_update(...)` to react to bid/offer updates from MARKET subscriptions:

```python
async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
    mid = (bid + offer) / 2
    print(epic, mid)
```

#### Completed candles (CHART)

Override `on_candle_update(...)` for candle-based logic. The default implementation stores completed candles in `self.charts[(epic, period)]`.

```python
from tradedesk.indicators.williams_r import WilliamsR

class CandleStrategy(BaseStrategy):
    SUBSCRIPTIONS = [ChartSubscription("CS.D.GBPUSD.TODAY.IP", "5MINUTE")]

    def __init__(self, client, config=None):
        super().__init__(client, config)
        self.wr = WilliamsR(period=14)

    async def on_candle_update(self, epic, period, candle):
        await super().on_candle_update(epic, period, candle)

        wr = self.wr.update(candle)
        if self.wr.ready() and wr is not None and wr < -80:
            print("Oversold")
```

### 3) Chart history

For each `ChartSubscription(epic, period)` the base class creates a `ChartHistory` instance stored at:

```
self.charts[(epic, period)]
```

The maximum stored candle count is controlled by:

```yaml
chart:
  history_length: 200
```

### 4) Indicator warmup using historical candles

Indicators declare how many completed candles they need before becoming ready via:

```
Indicator.warmup_periods()
```

At startup, tradedesk can preload the required number of historical candles per chart subscription and feed them into registered indicators.

#### Register indicators

Indicators must be registered against a `ChartSubscription`:

```python
class MyStrategy(BaseStrategy):
    SUBSCRIPTIONS = [ChartSubscription("CS.D.GBPUSD.TODAY.IP", "1MINUTE")]

    def __init__(self, client, config=None):
        super().__init__(client, config)

        self.sub_1m = self.SUBSCRIPTIONS[0]
        self.wr = WilliamsR(period=14)

        self.register_indicator(self.sub_1m, self.wr)
```

#### Warmup safety

- Warmup feeds historical candles directly into indicators
- `on_candle_update` is **not** called during warmup by default
- Trading logic only runs on live data

## Best practices (strongly recommended)

- **Start with DEMO accounts only**
- Keep `on_price_update`:
  - Fast
  - Non-blocking
  - Free of long computations
- Avoid shared mutable state between strategies
- Use logging, not `print`
- Assume:
  - Network interruptions
  - Reconnects
  - Duplicate or missing updates

This framework makes very few guarantees — defensive coding is essential.

--- 

## Project status & future work

This project is in an **early experimental stage**.

Planned or possible future additions include:
- Backtesting and historical replay
- Improved strategy lifecycle hooks
- Better tooling and documentation

None of these APIs should be considered stable at this time.

---

## Final warning

If you enable live trading:
- Trades may be placed immediately
- Bugs can and will cost real money
- No safeguards exist beyond what you implement yourself

**You assume all risk, at all times.**
