# TradeDesk Indicators Reference

This document provides a concise description of each technical indicator implemented in `tradedesk`, followed by a minimal example showing how to **configure and register** the indicator against a `ChartSubscription`.

The focus here is purely on *what the indicator is* and *how it is wired into a strategy*.  
No guidance is given on when or why to use any particular indicator.

All indicators are:

- Stateful
- Updated candle-by-candle via `update(candle)`
- Warmup-aware via `warmup_periods()`
- Resettable via `reset()`

---

## Common registration pattern

```python
from tradedesk.subscriptions import ChartSubscription
from tradedesk.strategy import BaseStrategy

class MyStrategy(BaseStrategy):
    def __init__(self, client):
        sub = ChartSubscription("CS.D.EURUSD.TODAY.IP", "5MINUTE")
        super().__init__(client, subscriptions=[sub])

        # Register indicator instances
        # self.register_indicator(sub, <indicator instance>)
```

---

## ADX — Average Directional Index

**Description:**  
Measures trend strength using Wilder-smoothed directional movement. Produces the ADX value along with positive and negative directional indicators.

**Update output:**  
`{"adx": float | None, "plus_di": float | None, "minus_di": float | None}`

```python
from tradedesk.indicators import ADX
self.register_indicator(sub, ADX(period=14))
```

---

## ATR — Average True Range

**Description:**  
Measures market volatility using true range and Wilder smoothing.

**Update output:**  
`float | None`

```python
from tradedesk.indicators import ATR
self.register_indicator(sub, ATR(period=14))
```

---

## Bollinger Bands

**Description:**  
Calculates a simple moving average with upper and lower bands derived from population standard deviation.

**Update output:**  
`{"middle": float | None, "upper": float | None, "lower": float | None, "std": float | None}`

```python
from tradedesk.indicators import BollingerBands
self.register_indicator(sub, BollingerBands(period=20, k=2.0))
```

---

## CCI — Commodity Channel Index

**Description:**  
Measures deviation of typical price from its moving average using mean deviation.

**Update output:**  
`float | None`

```python
from tradedesk.indicators import CCI
self.register_indicator(sub, CCI(period=20))
```

---

## EMA — Exponential Moving Average

**Description:**  
Exponentially weighted moving average of closing prices.

**Update output:**  
`float | None`

```python
from tradedesk.indicators import EMA
self.register_indicator(sub, EMA(period=14))
```

---

## MACD — Moving Average Convergence Divergence

**Description:**  
Computed from the difference between fast and slow EMAs, with a signal line and histogram.

**Update output:**  
`{"macd": float | None, "signal": float | None, "histogram": float | None}`

```python
from tradedesk.indicators import MACD
self.register_indicator(sub, MACD(fast=12, slow=26, signal=9))
```

---

## MFI — Money Flow Index

**Description:**  
Volume-weighted oscillator derived from typical price and money flow.

**Update output:**  
`float | None`

```python
from tradedesk.indicators import MFI
self.register_indicator(sub, MFI(period=14))
```

---

## OBV — On-Balance Volume

**Description:**  
Cumulative volume series adjusted based on changes in closing price.

**Update output:**  
`float | None`

```python
from tradedesk.indicators import OBV
self.register_indicator(sub, OBV())
```

---

## RSI — Relative Strength Index

**Description:**  
Wilder-smoothed ratio of average gains to average losses, mapped to a 0–100 scale.

**Update output:**  
`float | None`

```python
from tradedesk.indicators import RSI
self.register_indicator(sub, RSI(period=14))
```

---

## SMA — Simple Moving Average

**Description:**  
Arithmetic mean of closing prices over a rolling window.

**Update output:**  
`float | None`

```python
from tradedesk.indicators import SMA
self.register_indicator(sub, SMA(period=20))
```

---

## Stochastic Oscillator

**Description:**  
Compares the closing price to the recent high–low range and applies smoothing.

**Update output:**  
`{"k": float | None, "d": float | None}`

```python
from tradedesk.indicators import Stochastic
self.register_indicator(sub, Stochastic(k_period=14, d_period=3))
```

---

## VWAP — Volume Weighted Average Price

**Description:**  
Cumulative volume-weighted average price, optionally resetting at UTC day boundaries.

**Update output:**  
`float | None`

```python
from tradedesk.indicators import VWAP
self.register_indicator(sub, VWAP(use_typical_price=True, reset_daily_utc=True))
```

---

## Williams %R

**Description:**  
Measures the position of the close relative to the recent high–low range.

**Update output:**  
`float | None`

```python
from tradedesk.indicators import WilliamsR
self.register_indicator(sub, WilliamsR(period=14))
```
