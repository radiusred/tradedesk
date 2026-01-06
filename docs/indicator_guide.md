# Indicators in tradedesk

---

## Purpose of this document

This guide explains what indicators are within the `tradedesk` framework, how they are expected to behave, and the mathematical intuition behind the indicators commonly used by strategies.

The goal is not to provide trading advice, but to ensure that indicator usage is:

* conceptually correct,
* mathematically understood,
* and operationally sound when used in both backtests and live execution.

---

## What an indicator is (in tradedesk)

In `tradedesk`, an *indicator* is a **stateful transformation of market data**. Indicators:

* Consume a stream of prices, candles, or volumes
* Maintain internal state across updates
* Produce derived values (not trading decisions)

Indicators do **not**:

* Place orders
* Know about positions
* Encode strategy logic

They are deliberately isolated from execution concerns.

---

## Indicator lifecycle

All indicators follow the same conceptual lifecycle:

1. **Construction** – parameters are fixed (e.g. lookback periods)
2. **Warmup** – insufficient data to produce reliable output
3. **Ready** – outputs are mathematically meaningful
4. **Steady-state update** – one update per tick or candle

### Warmup and readiness

Most indicators require a minimum amount of historical data before their output stabilises. During warmup:

* Values may be undefined or misleading
* Strategies must assume indicators are *not ready*

In `tradedesk`, readiness is the responsibility of the strategy or its state container (e.g. `EpicState`), not the framework itself.

---

## Tick-driven vs candle-driven indicators

Indicators differ in how frequently they should be updated:

### Tick-driven indicators

* Updated on every price change
* Capture short-term momentum and microstructure
* Sensitive to noise

Example: MACD (as used in `ig_trader`)

### Candle-driven indicators

* Updated once per completed candle
* Capture higher-level structure
* Less sensitive to noise

Examples: ATR, MFI, Williams %R

A strategy may legitimately combine both, but must do so with a clear understanding of update ordering and lag.

---

## Mathematical foundations

This section introduces the indicators commonly used in `tradedesk` strategies. Each indicator is described in three layers:

1. Conceptual meaning
2. Mathematical definition
3. Practical implementation considerations

---

## Average True Range (ATR)

### What ATR measures

ATR measures **price volatility**, not direction. It answers the question:

> “How much does this market typically move per period?”

ATR is commonly used for:

* Position sizing
* Stop distance calculation
* Volatility regime detection

### Mathematical definition

For each candle *t*, the *true range* (TR) is defined as:

* High(t) − Low(t)
* |High(t) − Close(t−1)|
* |Low(t) − Close(t−1)|

The true range is the maximum of these three values.

ATR is then calculated as a moving average of TR over *N* periods (typically using an exponential or Wilder-style smoothing).

### Interpretation

* High ATR → volatile market
* Low ATR → compressed or ranging market

ATR has **no directional bias**.

### Implementation notes in tradedesk

* ATR is candle-driven
* ATR must be fully warmed up before being used for stops
* Strategies should treat a missing or zero ATR as “not ready”

ATR is often used to derive a stop level:

```
stop_distance = atr * multiplier
```

The choice of multiplier is a strategy parameter, not an indicator concern.

---

## Moving Average Convergence Divergence (MACD)

### Conceptual overview

MACD measures **momentum** by comparing two exponential moving averages (EMAs) of price.

It captures:

* Directional bias
* Momentum acceleration and deceleration

### Mathematical definition

Let:

* EMA_fast = EMA(price, fast_period)
* EMA_slow = EMA(price, slow_period)

Then:

* MACD line = EMA_fast − EMA_slow
* Signal line = EMA(MACD line, signal_period)
* Histogram = MACD line − Signal line

### Interpretation

* Positive histogram → bullish momentum
* Negative histogram → bearish momentum
* Histogram crossing zero → momentum regime change

### Implementation notes in tradedesk

* MACD is treated as **tick-driven** in `ig_trader`
* Candle closes may be fed as synthetic ticks during backtests
* MACD requires substantial warmup to stabilise

MACD signals are particularly sensitive to:

* Update frequency
* Price source (mid vs bid/offer)

---

## Money Flow Index (MFI)

### Conceptual overview

MFI is a **volume-weighted momentum oscillator**.

It attempts to detect:

* Accumulation
* Distribution
* Overbought and oversold conditions

### Mathematical definition (high-level)

1. Compute typical price per candle
2. Multiply by volume to obtain raw money flow
3. Separate positive and negative flows
4. Compute the ratio over *N* periods

The final MFI value is normalised to a 0–100 range.

### Interpretation

* MFI > 80 → overbought
* MFI < 20 → oversold

Exact thresholds are strategy-dependent.

### Implementation notes in tradedesk

* MFI is candle-driven
* Volume quality matters
* MFI is undefined until fully warmed up

---

## Williams %R

### Conceptual overview

Williams %R measures where the current close sits within the recent high–low range.

It answers:

> “Is price closing near the top or bottom of its recent range?”

### Mathematical definition

For a lookback window of *N* periods:

```
%R = (HighestHigh − Close) / (HighestHigh − LowestLow) * −100
```

Values range from −100 to 0.

### Interpretation

* Near 0 → price closing near recent highs
* Near −100 → price closing near recent lows

### Implementation notes in tradedesk

* Candle-driven
* Sensitive to lookback choice
* Often used in conjunction with MFI or trend filters

---

## Indicator interactions

Combining indicators introduces non-obvious effects:

* Lag compounds across indicators
* Agreement can occur *after* the move
* Disagreement may still be informative

Strategies should be designed to tolerate disagreement and delayed confirmation.

---

## Practical considerations

### Lookback selection

Longer lookbacks:

* Reduce noise
* Increase lag

Shorter lookbacks:

* Increase responsiveness
* Increase false signals

### Timeframe effects

Indicators behave differently across timeframes. A strategy should not assume that parameters transfer cleanly from one timeframe to another.

### Readiness checks

Strategies should explicitly guard against using indicators before they are ready. A missing or unstable indicator value should prevent entries, not merely degrade signal quality.

---

## Exponential Moving Average (EMA)

### Conceptual overview

An Exponential Moving Average (EMA) is a smoothed average of price that places greater weight on more recent observations.

Compared to a simple moving average, an EMA responds more quickly to recent price changes, making it more suitable for momentum-sensitive indicators.

### Mathematical definition

For a period length *N*, the smoothing factor α is defined as:

```
α = 2 / (N + 1)
```

The EMA is then updated recursively:

```
EMA(t) = α × Price(t) + (1 − α) × EMA(t−1)
```

### Interpretation

* EMA tracks the prevailing price direction
* Shorter periods increase responsiveness but amplify noise
* Longer periods reduce noise but increase lag

### Implementation notes in tradedesk

* EMA is tick-driven or candle-driven depending on usage
* EMA requires warmup equal to its period length
* EMA is commonly used as a building block for MACD

---

## Simple Moving Average (SMA)

### Conceptual overview

A Simple Moving Average (SMA) is the arithmetic mean of the last *N* prices.

It provides a baseline view of trend direction with maximal smoothing and lag.

### Mathematical definition

```
SMA(t) = (P(t) + P(t−1) + … + P(t−N+1)) / N
```

### Interpretation

* SMA smooths price but reacts slowly to change
* Crossovers are often used as trend filters

### Implementation notes in tradedesk

* SMA is candle-driven in most use cases
* SMA must be fully warmed before use

---

## Relative Strength Index (RSI)

### Conceptual overview

RSI is a momentum oscillator that measures the speed and magnitude of recent price changes.

It is commonly used to identify overbought and oversold conditions.

### Mathematical definition

RSI compares average gains to average losses over *N* periods and normalises the result to a 0–100 scale.

### Interpretation

* RSI > 70 → overbought
* RSI < 30 → oversold

Thresholds are context-dependent and should not be treated as absolute.

### Implementation notes in tradedesk

* RSI is candle-driven
* RSI requires sufficient warmup to stabilise averages

---

## Average Directional Index (ADX)

### Conceptual overview

ADX measures **trend strength**, not direction.

It is often used as a filter to determine whether trend-following signals are likely to be effective.

### Mathematical definition

ADX is derived from the directional movement indicators (+DI and −DI), which are based on directional price changes.

### Interpretation

* Low ADX → weak or ranging market
* High ADX → strong trend (direction determined elsewhere)

### Implementation notes in tradedesk

* ADX is candle-driven
* ADX is often used as a gating condition rather than a signal

---

## Bollinger Bands

### Conceptual overview

Bollinger Bands describe price volatility relative to a moving average.

They consist of:

* a middle moving average
* an upper and lower band offset by standard deviation

### Mathematical definition

```
Upper = MA + (k × σ)
Lower = MA − (k × σ)
```

where σ is the standard deviation over the lookback window.

### Interpretation

* Band expansion → increasing volatility
* Band contraction → volatility compression

### Implementation notes in tradedesk

* Bollinger Bands are candle-driven
* Interpretation depends heavily on parameter selection

---

## Commodity Channel Index (CCI)

### Conceptual overview

CCI measures deviation of price from its statistical mean.

It is often used to identify cyclical turning points.

### Mathematical definition

CCI compares typical price to its moving average, scaled by mean deviation.

### Interpretation

* High positive CCI → price above historical norm
* High negative CCI → price below historical norm

### Implementation notes in tradedesk

* CCI is candle-driven
* Sensitive to outliers

---

## Stochastic Oscillator

### Conceptual overview

The stochastic oscillator compares the current close to the recent high–low range.

It assumes momentum precedes price.

### Mathematical definition

```
%K = (Close − LowestLow) / (HighestHigh − LowestLow)
```

### Interpretation

* Values near extremes indicate potential reversals

### Implementation notes in tradedesk

* Candle-driven
* Often smoothed with a moving average

---

## On-Balance Volume (OBV)

### Conceptual overview

OBV accumulates volume based on price direction.

It attempts to detect accumulation and distribution.

### Mathematical definition

* If price closes up → add volume
* If price closes down → subtract volume

### Interpretation

* Rising OBV supports bullish price action
* Divergence may indicate weakening trends

### Implementation notes in tradedesk

* Candle-driven
* Volume quality is critical

---

## Volume Weighted Average Price (VWAP)

### Conceptual overview

VWAP represents the average price traded, weighted by volume.

It is commonly used as an institutional benchmark.

### Mathematical definition

```
VWAP = Σ(price × volume) / Σ(volume)
```

### Interpretation

* Price above VWAP → bullish bias
* Price below VWAP → bearish bias

### Implementation notes in tradedesk

* VWAP is session-dependent
* Requires careful reset semantics

---

## License

Licensed under the Apache License, Version 2.0.
See: [https://www.apache.org/licenses/LICENSE-2.0](https://www.apache.org/licenses/LICENSE-2.0)

Copyright 2026 [Radius Red Ltd.](https://github.com/radiusred)
