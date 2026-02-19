## Project Overview

**tradedesk** is the shared research and orchestration layer that underpins multiple trading implementations. It provides common abstractions for data handling, backtesting, portfolio orchestration, analytics, and experiment management.

This codebase is **not broker-specific**. It exists to:

* Enable fast, repeatable strategy research
* Provide realistic portfolio-level backtesting
* Act as the canonical home for shared logic and metrics

Downstream projects should depend on *tradedesk*; *tradedesk* should not depend on them.

---

## Primary Responsibilities

Agents should treat *tradedesk* as responsible for:

* Backtest orchestration and experiment flow
* Strategy-agnostic portfolio logic
* Trade recording, ledgers, and metrics
* Naming, configuration, and reproducibility conventions
* Research-grade correctness over execution convenience

If logic is reusable across brokers or execution environments, it likely belongs here.

---

## Current Objectives

Typical work includes:

* Closing meaningful unit test coverage gaps
* Hardening portfolio and analytics logic
* Simplifying or clarifying orchestration flows
* Removing legacy or leaked execution concerns
* Ensuring deterministic, explainable backtest results

Backward compatibility is **not required** unless explicitly stated.

---

## Preferred Way of Working

### Incremental and Focused

* Make small, well-scoped changes
* One concern per change set
* Avoid cascading refactors without agreement

### Code-First Reasoning

* Inspect files before discussing them
* Do not assume abstractions or intent
* Respect that some modules are intentionally small or narrow

### Pragmatic Testing

* Tests should defend correctness, not chase coverage
* Prioritise:

  * Portfolio aggregation logic
  * Metrics and statistics
  * Edge cases and boundary conditions
* Avoid brittle, over-specified tests

### Communication Style

* Concise, technical, professional
* Minimal narration
* Challenge assumptions where appropriate

---

## Coding Expectations

* Use latest stable Python and dependencies
* Follow current documentation and APIs
* No `from __future__ import ...` unless warranted
* Prefer clarity and correctness over abstraction
* Avoid premature generalisation
* Code should meet `ruff check` and `mypy --strict` requirements
* Create commit messages for git following "Conventional Commits" and the current style of the project's git log
  * Do not add author lines to git commits
* Follow the intentions of the domain architecture encoded in `pyproject.toml`
  * All imports across domains should use top level re-exports. Example: code in `tradedesk.execution` should only import code
    from `tradedesk.marketdata` and never from `tradedesk.marketdata.events` The class or function should be explicitly
    exported in `__init.py__` files if it can be used outside of the domain

When running code or commands:

* Use the `.venv` directory in the root of the relevant project

---

## Domain Assumptions

Agents are expected to understand:

* Backtesting mechanics and common biases
* Portfolio-level statistics and drawdown analysis
* Event-driven strategy evaluation
* The difference between research code and execution code

If a design choice trades realism for speed or simplicity, it should be explicit.

---

## What to Avoid

* Broker- or API-specific logic
* Leaking execution assumptions into research layers
* Over-engineering for hypothetical future needs
* Large rewrites without alignment

---

## Success Criteria

An agent is succeeding if it:

* Increases confidence in backtest results
* Improves clarity of portfolio and metrics logic
* Makes research workflows faster and safer
* Keeps the codebase cleanly reusable downstream
