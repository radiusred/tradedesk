# tradedesk

👋 `tradedesk` is a **very early-stage Python library for running automated trading strategies against IG Markets**, with a focus on:

- Real-time price streaming via Lightstreamer
- OHLCV candle data with historical warmup
- Technical indicators with stateful tracking
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
- **No warranty is provided**: see [LICENSE.md](LICENSE.md) for full terms

If you do not understand the risks of automated trading, **do not use this library**.

---

## What this project is (and is not)

### It *is*
- A framework for:
  - Subscribing to live IG prices via Lightstreamer
  - Receiving tick-level market updates (bid/offer)
  - Receiving completed OHLCV candles at various timeframes
  - Warming up indicators with historical data
  - Running one or more async trading strategies
  - Placing trades via the IG REST API
- A way to structure and run **your own trading logic**
- A backtesting or simulation engine

### It is *not*
- A trading bot that makes decisions for you
- A portfolio or risk management system
- A stable or production-ready framework
- Suitable for high-frequency trading

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

### Development Installation

For development with testing dependencies:

```bash
pip install -e ".[dev]"
pytest
```

---

## Configuration

`tradedesk` itself is configured via environment variables (or a `.env` file) for the **provider layer** (IG):

```bash
export IG_API_KEY="your_api_key"
export IG_USERNAME="your_username"
export IG_PASSWORD="your_password"
export IG_ENVIRONMENT="DEMO"  # or LIVE
```

**Strongly recommended:** start with `IG_ENVIRONMENT=DEMO`.

Alternate example `.env` file:
```env
IG_API_KEY=your_api_key
IG_USERNAME=your_username
IG_PASSWORD=your_password
IG_ENVIRONMENT=DEMO
```

---

## ⏩ Quick Start

### Simple Price Logging Strategy

```python
from tradedesk import BaseStrategy, run_strategies
from tradedesk.providers.ig.client import IGClient
from tradedesk.subscriptions import MarketSubscription

class LogPriceStrategy(BaseStrategy):
    """Logs every price update for GBP/USD."""

    SUBSCRIPTIONS = [
        MarketSubscription("CS.D.GBPUSD.TODAY.IP")
    ]

    async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
        mid = (bid + offer) / 2
        print(f"{timestamp}: {epic} = {mid:.5f}")

if __name__ == "__main__":
    run_strategies(
        strategy_specs=[LogPriceStrategy],
        client_factory=IGClient,
    )
```

For more complex strategies, subscriptions are typically constructed dynamically inside `__init__`.

---

## 📑 Core Concepts

### Strategies

A strategy is a Python class that:
- Subclasses `BaseStrategy`
- Declares which instruments and data types to subscribe to
- Responds to live price updates and/or completed candles

Strategies may declare subscriptions in two ways:

- **Static strategies** define a class-level `SUBSCRIPTIONS` list.
- **Configured strategies** build subscriptions dynamically inside `__init__` and pass them to `BaseStrategy`.

The strategy `__init__` method is treated as the **strategy configuration block**.

If `subscriptions` are passed to `BaseStrategy.__init__`, they override the class-level `SUBSCRIPTIONS`.

---

### Subscriptions

Strategies declare their data needs using subscription objects.

#### MarketSubscription

Subscribe to tick-level bid/offer updates:

```python
from tradedesk.subscriptions import MarketSubscription

SUBSCRIPTIONS = [
    MarketSubscription("CS.D.GBPUSD.TODAY.IP")
]

async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
    # Called on every tick
    pass
```

#### ChartSubscription

Subscribe to completed OHLCV candles:

```python
from tradedesk.subscriptions import ChartSubscription

SUBSCRIPTIONS = [
    ChartSubscription("CS.D.GBPUSD.TODAY.IP", "5MINUTE"),
    ChartSubscription("CS.D.EURUSD.TODAY.IP", "1HOUR")
]

async def on_candle_update(self, epic, period, candle):
    print(f"Close: {candle.close}, Volume: {candle.volume}")
```

Supported periods:
`"1MINUTE"`, `"5MINUTE"`, `"15MINUTE"`, `"30MINUTE"`, `"HOUR"`, `"4HOUR"`, `"DAY"`, `"WEEK"`

#### Dynamic subscription example

```python
class MultiMarketStrategy(BaseStrategy):
    def __init__(self, client):
        epics = ["CS.D.GBPUSD.TODAY.IP", "CS.D.EURUSD.TODAY.IP"]
        timeframe = "5MINUTE"

        subs = []
        for epic in epics:
            subs.append(MarketSubscription(epic))
            subs.append(ChartSubscription(epic, timeframe))

        super().__init__(client, subscriptions=subs)
```

---

### Data Streaming

- **Primary mode**: Lightstreamer (IG) or other real-time streaming
- **Fallback mode**: REST API polling

The framework automatically selects the appropriate mode based on authentication type.

---

### Technical Indicators

Built-in indicators with stateful tracking:

```python
from tradedesk.indicators import WilliamsR, MFI, MACD

class IndicatorStrategy(BaseStrategy):
    SUBSCRIPTIONS = [
        ChartSubscription("CS.D.GBPUSD.TODAY.IP", "5MINUTE")
    ]

    def __init__(self, client):
        super().__init__(client)

        self.wr = WilliamsR(period=14)
        self.mfi = MFI(period=14)
        self.macd = MACD(fast=12, slow=26, signal=9)

        sub = self.SUBSCRIPTIONS[0]
        self.register_indicator(sub, self.wr)
        self.register_indicator(sub, self.mfi)
        self.register_indicator(sub, self.macd)
```

**Available indicators:**
- `WilliamsR`
- `MFI`
- `MACD`

More indicators will be added in future framework versions.
---

### Chart History

For each `ChartSubscription`, the framework maintains a rolling history of the **most recent 200 candles**.

```python
chart = self.charts[("CS.D.GBPUSD.TODAY.IP", "5MINUTE")]

recent_candles = chart.get_candles(count=20)
latest = chart.latest
```

The history length is currently fixed in the framework.

---

### Indicator Warmup

Indicators require historical data before producing valid signals. The framework handles this automatically:

- Indicators declare how many periods they require
- The framework fetches sufficient historical candles at startup
- Indicators are warmed up before live candles are delivered

Check indicator readiness:

```python
if self.wr.ready():
    value = self.wr.update(candle)
```

---

## Strategy Configuration Model

`tradedesk` does **not** load strategy configuration files.

Strategies are configured directly in Python code, typically inside `__init__`.  
If you wish to use configuration files (YAML, JSON, etc.), you should load them yourself inside the strategy.

This design is intentional:
- Keeps the framework minimal and explicit
- Avoids opinionated configuration formats
- Gives strategy authors full control

---

## Running Multiple Strategies

```python
run_strategies(
    [StrategyA, StrategyB, StrategyC],
    client_factory=IGClient,
)
```

Each strategy:
- Runs independently
- Shares the same IG client connection
- Receives only its subscribed data feeds

---

## Trading (Placing Orders)

Strategies can place trades via `self.client`:

```python
async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
    if self.should_buy():
        result = await self.client.place_market_order(
            epic=epic,
            direction="BUY",
            size=1.0,
            currency="USD"
        )
        print(f"Order placed: {result['dealReference']}")
```

> ⚠️ **In the example strategies, actual trading calls are commented out for safety.**

Available client methods:
- `get_market_snapshot(epic)` - Get current prices
- `get_price_ticks(epic)` - Get recent price history
- `place_market_order(epic, direction, size, ...)` - Place market order

---

## Best Practices (Strongly Recommended)

### Safety
- **Start with DEMO accounts only**
- Test thoroughly before considering live trading
- Never deploy untested code to live accounts
- Implement your own risk management and position limits

### Performance
- Keep `on_price_update()` **fast** - it runs on every tick
- Avoid blocking operations (network calls, heavy computation)
- Use async/await properly
- Don't share mutable state between strategies

### Reliability
- Use logging, not `print` statements
- Implement proper error handling
- Assume network interruptions will happen
- Handle reconnections gracefully
- Expect duplicate or missing updates

### Code Quality
- Write tests for your strategy logic
- Use type hints
- Document your trading logic
- Version control your strategies
- Keep strategies focused and simple


---

## Examples

See the [examples/](examples/) directory:

- `log_price_strategy.py` - Basic price logging
- `momentum_strategy.py` - Simple momentum detection

---

## Known Limitations

### Current State (v0.1.0)
- **Experimental**: APIs will change
- **No backtesting**: Historical simulation not yet implemented
- **Limited error recovery**: Network failures require restart
- **No order management**: No built-in position tracking or stop losses
- **Single account**: Cannot trade multiple accounts simultaneously
- **No rate limiting**: Your code must respect IG's API limits

### Not Suitable For
- High-frequency trading (HFT)
- Sub-second decision making
- Production trading without extensive testing
- Users unfamiliar with trading risks

---

## Testing

Run the test suite:

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=tradedesk --cov-report=html

# Run specific test file
pytest tests/test_strategy.py -v
```

Current test coverage: ~65%

Areas needing more tests:
- OAuth token refresh flows
- Lightstreamer connection failure scenarios
- Concurrent strategy execution
- Network error recovery

---

## Troubleshooting

### "Lightstreamer not available"
- Install: `pip install lightstreamer-client-lib`
- Check your IG API version supports Lightstreamer
- DEMO accounts use V2 API (supports streaming)
- Some LIVE accounts use V3 OAuth (polling fallback)

### "Rate limit exceeded"
- IG has strict API rate limits
- Use Lightstreamer streaming instead of polling
- Reduce polling frequency if using fallback mode
- Wait a few minutes before retrying

### "No price updates"
- Check IG market hours (forex 24/5, indices have specific hours)
- Verify EPIC codes are correct
- Check your account permissions
- Review logs for authentication errors

### Strategy not receiving candles
- Ensure `ChartSubscription` is used (not `MarketSubscription`)
- Check period format: `"5MINUTE"` not `"5M"`
- Verify candle data is available for that instrument/timeframe
- Historical warmup may delay first live candle

---

## Contributing

Contributions are welcome! This project is in early development.

Areas that need work:
- Additional technical indicators
- Backtesting framework
- Better error handling and recovery
- More comprehensive tests
- Documentation and examples

Please open an issue before starting major work.

---

## Project Status & Roadmap

### Current: v0.1.0 (Experimental)
- ✅ Basic strategy framework
- ✅ Lightstreamer streaming
- ✅ REST API polling fallback
- ✅ Technical indicators (3 implemented)
- ✅ Chart history management
- ✅ Indicator warmup with historical data
- ✅ Backtesting engine

### Planned
- [ ] Historical data replay
- [ ] More technical indicators (RSI, Bollinger Bands, etc.)
- [ ] Strategy lifecycle hooks (`on_start`, `on_stop`, `on_error`)

None of these APIs should be considered stable at this time.

---

## License

MIT License — see [LICENSE.md](LICENSE.md)

**THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.**

You assume all risk when using this library. The authors are not responsible for any financial losses.

---

## Final Warning

If you enable live trading:
- Trades may be placed immediately
- Bugs can and will cost real money
- No safeguards exist beyond what you implement yourself
- You are solely responsible for all outcomes

**You assume all risk, at all times.**

Before using with real money:
1. Test extensively on DEMO accounts
2. Start with minimal position sizes
3. Implement your own risk controls
4. Monitor actively during initial runs
5. Understand that losses can exceed deposits

If you are not comfortable with these risks, **do not use this library for live trading**.

---

## Support

- **Issues**: [GitHub Issues](https://github.com/davison/tradedesk/issues)
- **Discussions**: [GitHub Discussions](https://github.com/davison/tradedesk/discussions)

This is an open-source project maintained by volunteers. No official support is provided.
