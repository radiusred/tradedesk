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
* Avoid brittle, over-specified tests
* Use `pytest` not `unittest`

### Communication Style

* Concise, technical, professional
* Minimal narration
* Challenge assumptions - constructive dialog and pushback is expected

---

## Coding Expectations

* Use latest stable Python and dependencies
* Follow current documentation and APIs
* Code should pass `ruff check` and `mypy --strict` requirements
* Create commit messages for git following "Conventional Commits" and the current style of the project's git log
  * Do not add author lines to git commits
* Follow the intentions of the domain architecture encoded in `pyproject.toml`
  * All imports across domains should use top level re-exports. Example: code in `tradedesk.execution` should only import code
    from `tradedesk.marketdata` and never from `tradedesk.marketdata.events` The class or function should be explicitly
    exported in `__init.py__` files if it can be used outside of the domain

When running code or commands:

* Use `uv` instead of `pip`
* Always use the `.venv` directory in the root of the project
