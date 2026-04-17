# Backtesting Guide Code Alignment

This note anchors the backtesting guide to the current public implementation.
When the guide describes a runtime behavior, check these modules first:

- Backtest client loading and simulated fills: `tradedesk/execution/backtest/client.py`
- Recorded backtest runner and `BacktestSpec`: `tradedesk/execution/backtest/runner.py`
- Portfolio lifecycle and order-gate wiring: `tradedesk/portfolio/base.py`
- Single-strategy wrapper used in the guide examples: `tradedesk/portfolio/simple.py`
- Strategy lifecycle and chart warmup behavior: `tradedesk/strategy/base.py`
- Subscription types used in the examples: `tradedesk/marketdata/subscriptions.py`

If the guide and code ever diverge, treat these modules as canonical and update
the prose to match the shipped behavior.
