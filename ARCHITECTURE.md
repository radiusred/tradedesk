# Architecture

Overview
- tradedesk is an event-driven trading framework designed to run strategies across both backtesting and live broker environments.
- The codebase is organized into distinct domains:
  - tradedesk.marketdata: ingestion and normalization of price data and events.
  - tradedesk.strategy: base classes and helpers for writing event-driven strategies.
  - tradedesk.portfolio: portfolio management, risk budgeting, and multi-instrument coordination.
  - tradedesk.execution: execution adapters (IG for live trading, Dukascopy paths for backtests).
  - tradedesk.recording: trade lifecycle tracking, metrics, and reporting.
  - tradedesk.research: research helpers such as walk-forward tooling, correlation gates, and public configuration templates.
  - tradedesk.ml: machine-learning building blocks for feature engineering, labels, walk-forward CV, and model wrappers.

Data Flow (high level)
- Market data events flow into strategies via the event system.
- Strategies emit execution requests in response to data events.
- Portfolio layer applies risk budgets and distributes them to active sleeves/instruments.
- Execution layer applies pre-flight gates (for example spread limits or
  portfolio-level order gates), then places orders and emits
  order-completion plus position-lifecycle events.
- Recording layer captures lifecycle events and metrics for reporting.

Live vs Backtest paths
- Live trading uses the IG module (IGClient, IGAuthManager, etc.).
- Backtesting uses the Dukascopy-based data path, converting Dukascopy data to the internal candle/tick format.

Key design decisions
- Pure event-driven components: strategies react to events, not to direct broker state.
- Separate concerns: data handling, strategy logic, risk management, and execution are decoupled to simplify testing and maintenance.
- Deterministic backtesting: same strategy code runs in both backtest and live modes to ensure reproducibility.
- Live parity for recording: when a client does not publish its own
  position-open events, the execution layer emits `PositionOpenedEvent` after a
  confirmed opening fill so recording and downstream observers see the same
  lifecycle boundary in backtest and IG-backed sessions.

See also: `README.md`, `docs/backtesting_guide.md`, `docs/strategy_guide.md`, and `docs/research_methodology_guide.md`.

---

## License

Licensed under the Apache License, Version 2.0.
See: https://www.apache.org/licenses/LICENSE-2.0

Copyright 2026 [Radius Red Ltd.](https://github.com/radiusred) | [Contact](mailto:opensource@radiusred.uk)
