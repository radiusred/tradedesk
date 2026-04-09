## Summary

<!-- What does this PR do? Why? -->

## Changes

<!-- Bullet list of key changes -->

---

## PR Review Checklist

### Code quality
- [ ] `uv run ruff check tradedesk` passes
- [ ] `uv run mypy tradedesk` passes
- [ ] Tests pass on all supported Python versions (3.11–3.14)
- [ ] Coverage remains ≥ 85%

### Public-library suitability gate

**This is a public open-source library.** It must work for any user on any deployment platform.

- [ ] No code assumes a specific deployment platform (Cloud Run, Docker, systemd, GCP, AWS, Azure, Kubernetes)
- [ ] No `sys.exit()` or `os._exit()` called for operational reasons (only acceptable in CLI entry points or tests)
- [ ] No log strings or comments reference cloud providers, container runtimes, or orchestrators
- [ ] Resilience logic (reconnection, retries) is self-contained — not delegated to a process supervisor
- [ ] If broker-specific (IG, Dukascopy), the code lives in the correct integration module and is deployment-agnostic

> **Red flags in commit messages and log strings:** Phrases like "Cloud Run", "container restart", "GCP", "AWS"
> in library source files are an automatic review failure. Fix the approach before merging.

### Architecture
- [ ] Cross-domain imports use top-level re-exports only (see `pyproject.toml`)
- [ ] New public symbols are explicitly exported in `__init__.py`

### Documentation
- [ ] Public API changes are reflected in `docs/`
- [ ] Commit message follows Conventional Commits style
