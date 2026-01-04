# Technical Indicators in TradeDesk<!-- omit from toc -->

This document provides a brief, practical description of each technical indicator implemented in `tradedesk`.  
All indicators are **stateful**, **incrementally updated per candle**, and expose:

- `update(candle)` → value (or structured value) / `None`
- `ready()` → whether the indicator is usable
- `reset()` → clear internal state
- `warmup_periods()` → minimum candles required

---

## ADX — Average Directional Index

**Purpose:** Trend strength (not direction)  
**Type:** Trend strength

Quantifies how strong a trend is, regardless of direction.

**Outputs**
- `adx`
- `plus_di`
- `minus_di`

**Notes**
- ADX > ~20–25 indicates a trending market
- Useful as a regime filter

---

## ATR — Average True Range

**Purpose:** Volatility measurement  
**Type:** Volatility

Measures average price range using true range and Wilder smoothing.

**Notes**
- Direction-agnostic
- Commonly used for position sizing and stop placement

---

## Bollinger Bands

**Purpose:** Volatility expansion / contraction  
**Type:** Volatility / mean reversion

Bands plotted at a multiple of standard deviation around a moving average.

**Outputs**
- `middle` (SMA)
- `upper`
- `lower`
- `std`

**Notes**
- Bands widen during high volatility
- Often combined with mean-reversion logic

---

## CCI — Commodity Channel Index

**Purpose:** Mean reversion and momentum extremes  
**Type:** Momentum / mean reversion

Measures deviation of typical price from its moving average.

**Notes**
- Uses mean deviation (not standard deviation)
- Common thresholds: ±100

---

## EMA — Exponential Moving Average

**Purpose:** Faster trend detection  
**Type:** Trend / momentum

Like SMA, but weights recent prices more heavily using exponential smoothing.

**Notes**
- More responsive than SMA
- Commonly used in trend-following systems

---

## MACD — Moving Average Convergence Divergence

**Purpose:** Momentum and trend changes  
**Type:** Momentum / trend

Computed from the difference between fast and slow EMAs, plus a signal line.

**Outputs**
- `macd`
- `signal`
- `histogram`

**Notes**
- Captures momentum shifts
- Sensitive to parameter choice

---

## MFI — Money Flow Index

**Purpose:** Volume-weighted momentum  
**Type:** Momentum / volume

RSI-like oscillator that incorporates volume.

**Range:** 0–100  

**Notes**
- Highlights volume-confirmed overbought/oversold conditions

---

## OBV — On-Balance Volume

**Purpose:** Volume trend confirmation  
**Type:** Volume / momentum

Cumulates volume based on whether price closes up or down.

**Notes**
- Directional volume indicator
- Often used for divergence analysis

---

## RSI — Relative Strength Index

**Purpose:** Overbought / oversold detection  
**Type:** Momentum / mean reversion

Measures the ratio of recent gains to recent losses using Wilder smoothing.

**Range:** 0–100  
**Typical levels:** 30 (oversold), 70 (overbought)

---

## SMA — Simple Moving Average

**Purpose:** Trend direction and smoothing  
**Type:** Trend / baseline

Calculates the arithmetic mean of closing prices over a rolling window.

**Notes**
- Slow to react to price changes
- Often used as a baseline or trend filter

---

## Stochastic Oscillator

**Purpose:** Momentum and turning points  
**Type:** Momentum

Compares the closing price to the recent high–low range.

**Outputs**
- `%K` (fast line)
- `%D` (smoothed signal)

**Notes**
- Good for timing entries
- Noisy in strong trends

---

## VWAP — Volume Weighted Average Price

**Purpose:** Intraday fair value  
**Type:** Volume / price

Calculates the average price weighted by traded volume.

**Notes**
- Resets daily (UTC) by default
- Commonly used as a dynamic support/resistance reference

---

## Williams %R

**Purpose:** Overbought / oversold (price location)  
**Type:** Momentum

Shows where the close sits relative to the recent high–low range.

**Range:** -100 to 0  

**Notes**
- Similar to stochastic %K
- Very responsive

---

## Usage Notes

- All indicators are **purely candle-driven** (no lookahead).
- Warmup semantics are explicit via `warmup_periods()`.
- Indicators are designed to be composed cleanly in strategies.

For examples, see the strategy and indicator tests in the repository.
