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

## Writing your first strategy

A minimal example looks like this:

```python
from tradedesk import BaseStrategy

class LogPriceStrategy(BaseStrategy):
    EPICS = ["CS.D.EURUSD.CFD.IP"]

    async def on_price_update(self, epic, price):
        self.logger.info(
            "Price update",
            extra={"epic": epic, "price": price},
        )
```

This strategy:
- Subscribes to EUR/USD
- Logs every incoming price update
- Does **not** place trades

See the `/examples` directory for:
- A simple logging strategy
- A basic trading strategy (with order placement commented out)

---

## Running strategies

Strategies are run using the `run_strategies` entry point:

```python
from tradedesk import run_strategies
from my_strategies import LogPriceStrategy

run_strategies([
    LogPriceStrategy,
])
```

This will:
1. Authenticate with IG
2. Start price streaming
3. Run all strategies concurrently
4. Shut down cleanly on Ctrl-C

---

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
