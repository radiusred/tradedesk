"""``tradedesk.execution.backtest`` is not deprecated, and it is loaded lazily.

Importing it emits no ``DeprecationWarning``. Importing ``tradedesk`` (or
``tradedesk.execution``) does not load it, so live users do not import the
backtester; its exports still resolve from ``tradedesk.execution`` on first use.
"""

import importlib
import subprocess
import sys
import warnings

import pytest

import tradedesk.execution as execution


def _run(code: str) -> None:
    """Run ``code`` in a fresh interpreter, so no module is already imported."""
    subprocess.run([sys.executable, "-c", code], check=True)


def test_importing_the_backtest_module_does_not_warn() -> None:
    import tradedesk.execution.backtest as backtest

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        importlib.reload(backtest)
    assert backtest.__doc__ is not None
    doc = " ".join(backtest.__doc__.split())
    assert "run byte-identically in backtest and live" in doc
    assert "tradedesk-backtest" in doc
    assert "research engine for parameter sweeps" in doc
    assert "deprecated" not in doc.lower()


def test_importing_tradedesk_does_not_load_the_backtester() -> None:
    _run(
        "import sys\n"
        "import tradedesk, tradedesk.execution\n"
        "assert 'tradedesk.execution.backtest' not in sys.modules\n"
    )


def test_the_execution_exports_resolve_on_first_use_without_a_warning() -> None:
    _run(
        "import warnings\n"
        "warnings.simplefilter('error', DeprecationWarning)\n"
        "import tradedesk.execution as ex\n"
        "client = ex.BacktestClient\n"
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
