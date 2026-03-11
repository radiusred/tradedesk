# Initialise

FIRST; read https://github.com/radiusred/.github/CLAUDE.md (or the local copy).

---

## Coding Expectations

* Use latest stable Python and dependencies
* Follow current documentation and APIs
* Code should pass `ruff check` and `mypy --strict` requirements
* Create commit messages for git following "Conventional Commits" and the current style of the project's git log
* Follow the intentions of the domain architecture encoded in `pyproject.toml`
  * All imports across domains should use top level re-exports. Example: code in `tradedesk.execution` should only import code
    from `tradedesk.marketdata` and never from `tradedesk.marketdata.events` The class or function should be explicitly
    exported in `__init.py__` files if it can be used outside of the domain

When running code or commands:

* Always use `uv` or the `.venv` directory in the root of the project to run development tools
