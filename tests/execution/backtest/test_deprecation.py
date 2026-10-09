"""``tradedesk.execution.backtest`` is deprecated in favour of the Rust backtester.

Importing it warns; importing ``tradedesk`` (or ``tradedesk.execution``) does not load
it, so only code that uses the Python backtester sees the warning. Its exports still
resolve from ``tradedesk.execution``.
"""

import importlib
import subprocess
import sys

import pytest

import tradedesk.execution as execution

MESSAGE = "tradedesk.execution.backtest is deprecated"


def _run(code: str) -> None:
    """Run ``code`` in a fresh interpreter, so no module is already imported."""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_importing_the_backtest_module_warns_and_names_the_rust_backtester() -> None:
    import tradedesk.execution.backtest as backtest

    with pytest.warns(DeprecationWarning, match="Rust backtester") as record:
        importlib.reload(backtest)
    assert any(MESSAGE in str(w.message) for w in record)
    assert backtest.__doc__ is not None
    assert "Rust backtester" in backtest.__doc__
    assert "tradedesk-backtest" in backtest.__doc__


def test_importing_tradedesk_does_not_load_the_deprecated_module() -> None:
    _run(
        "import sys, warnings\n"
        f"warnings.filterwarnings('error', message={MESSAGE!r})\n"
        "import tradedesk, tradedesk.execution\n"
        "assert 'tradedesk.execution.backtest' not in sys.modules\n"
    )


def test_the_execution_exports_resolve_on_first_use_and_warn() -> None:
    _run(
        "import warnings\n"
        "import tradedesk.execution as ex\n"
        "with warnings.catch_warnings(record=True) as caught:\n"
        "    warnings.simplefilter('always')\n"
        "    client = ex.BacktestClient\n"
        f"assert any({MESSAGE!r} in str(w.message) for w in caught)\n"
        "from tradedesk.execution.backtest import BacktestClient\n"
        "assert client is BacktestClient\n"
        "assert ex.backtest.BacktestClient is BacktestClient\n"
    )


def test_the_lazy_attribute_hook() -> None:
    from tradedesk.execution.backtest import BacktestClient

    assert execution.__getattr__("BacktestClient") is BacktestClient
    assert execution.__getattr__("backtest").BacktestClient is BacktestClient
    assert "BacktestClient" in execution.__all__
    with pytest.raises(AttributeError, match="no attribute 'nope'"):
        execution.__getattr__("nope")
