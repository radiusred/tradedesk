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

4. Install the [pre-commit](https://pre-commit.com/) hooks. These run
   `ruff-format` and a guard that blocks private strategy research from being
   committed to this public repo (paths under `research/`, `killstack/`, or
   anything containing `private`):

```bash
pre-commit install
```

   The guard script lives at `scripts/hooks/block-private-research.sh` and is
   also runnable on demand against the staged tree. If a commit is rejected
   with `Blocked: private research content detected — commit to ig_trader
   instead`, the listed files belong in the private `ig_trader` repo, not here.

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

## Credentials and Release Automation

Local development, CI, and backtests do not require repository secrets.

Live IG execution uses environment variables read by
`tradedesk.execution.ig.settings.Settings`:

- `IG_API_KEY` (required)
- `IG_USERNAME` (required)
- `IG_PASSWORD` (required)
- `IG_ENVIRONMENT` (optional, defaults to `DEMO`; must be `DEMO` or `LIVE`)

Do not commit real IG values in examples, shell history snippets, or tests. The
library negotiates the short-lived IG session headers (`CST` and
`X-SECURITY-TOKEN`) during authentication, so contributors should not try to
store or configure those manually.

Maintainers triggering `.github/workflows/prepare-release.yml` must configure
these repository secrets:

- `RELEASE_APP_ID`
- `RELEASE_APP_PRIVATE_KEY`

The shared reusable release workflow uses those values to mint a GitHub App
token for checkout, version bumping, pushing the release commit, and creating
the GitHub release. `.github/workflows/publish.yml` uses PyPI trusted
publishing via GitHub OIDC (`id-token: write`), so no PyPI API token secret is
expected in this repository.

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

If unsure, open an issue describing the goal and request guidance before implementing.

---

## License

Licensed under the Apache License, Version 2.0.
See: https://www.apache.org/licenses/LICENSE-2.0

Copyright 2026 [Radius Red Ltd.](https://github.com/radiusred) | [Contact](mailto:opensource@radiusred.uk)
