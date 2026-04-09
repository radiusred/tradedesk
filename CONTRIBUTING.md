# Contributing to tradedesk

We welcome contributions to `tradedesk`. This guide covers the standards and
workflow you need to contribute successfully.

## Getting Started

1. Fork the repository and create a feature branch from `main`.
2. Clone locally and install the dev dependencies:

```bash
pip install -e '.[dev]'
```

3. We use [`uv`](https://docs.astral.sh/uv/) for dependency management. If you
   have `uv` installed you can use it directly:

```bash
uv sync --extra dev
```

## Code Standards

### Python Version

Python 3.11+ is required.

### Style and Linting

We use [ruff](https://docs.astral.sh/ruff/) for linting and formatting:

```bash
ruff check .
ruff format --check .
```

Rules enforced: `E`, `F`, `I` (import sorting), `TID` (tidy imports).
Line length is 100 characters.

### Type Checking

We use [mypy](https://mypy.readthedocs.io/) in strict mode:

```bash
mypy .
```

All contributions must pass with zero type errors.

### Import Conventions

Cross-domain imports must use top-level re-exports. For example, code in
`tradedesk.execution` should import from `tradedesk.marketdata`, never from
`tradedesk.marketdata.events`. Classes and functions intended for use outside
their domain must be explicitly exported in `__init__.py`.

## Testing

We use [pytest](https://docs.pytest.org/):

```bash
pytest
```

- All new and existing tests must pass following any code change.
- Include tests for new functionality or bug fixes.
- Code coverage must not decrease.

## Pull Requests

- Keep changes small, well-scoped, and documented in PR descriptions.
- Open PRs against `main`.
- Rebase your branch onto `main` before submitting — we do not use merge commits.
- Provide a brief rationale and expected impact in the PR description.
- Do not include secrets or internal data.

## Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat(strategy): add trailing stop support
fix(portfolio): correct position sizing on partial fills
docs: update backtesting guide
```

## Reporting Issues

If you find a bug or have a feature request, open an issue describing the goal
and expected behaviour. Include steps to reproduce for bugs.

If unsure about scope or approach, open an issue to discuss before implementing.

---

## License

Licensed under the Apache License, Version 2.0.
See: https://www.apache.org/licenses/LICENSE-2.0

Copyright 2026 [Radius Red Ltd.](https://github.com/radiusred)
