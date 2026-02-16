import pytest
from dataclasses import FrozenInstanceError
from tradedesk.marketdata.instrument import Instrument


def test_valid_instrument_creation() -> None:
    """Tests that a valid instrument is created successfully."""
    aapl = Instrument(
        symbol="AAPL",
        isin="US0378331005",
        asset_class="Equity",
        broker_codes={"ig": "UC.D.AAPL.CASH.IP"},
    )
    assert aapl.symbol == "AAPL"
    assert str(aapl) == "AAPL"
    assert aapl.isin == "US0378331005"


def test_optional_fields_none() -> None:
    """Tests that only the symbol is strictly required."""
    generic = Instrument(symbol="CASH")
    assert generic.symbol == "CASH"
    assert generic.isin is None
    assert generic.broker_codes == {}


# --- ISIN SPECIFIC TESTS ---


def test_isin_invalid_length() -> None:
    """Should fail if ISIN is not 12 characters."""
    with pytest.raises(ValueError, match="Expected 2 letters"):
        Instrument(symbol="FAIL", isin="US037833")


def test_isin_invalid_format() -> None:
    """Should fail if format is wrong (e.g., lowercase)."""
    with pytest.raises(ValueError, match="Invalid ISIN format"):
        Instrument(symbol="FAIL", isin="us0378331005")


def test_isin_checksum_failure() -> None:
    """Should fail if the Luhn checksum digit is incorrect."""
    # Correct is US0378331005. Let's change the last digit to 4.
    with pytest.raises(ValueError, match="ISIN checksum failed"):
        Instrument(symbol="AAPL", isin="US0378331004")


def test_isin_another_valid_example() -> None:
    """Verifies another international ISIN (BAE Systems - GB)."""
    bae = Instrument(symbol="BA.", isin="GB0002634946")
    assert bae.isin == "GB0002634946"


# --- IMMUTABILITY TESTS ---


def test_instrument_is_frozen() -> None:
    """Tests that the object cannot be modified after creation."""
    aapl = Instrument(symbol="AAPL")
    with pytest.raises(FrozenInstanceError):
        # Mypy will also flag this as a type error
        aapl.symbol = "MSFT"  # type: ignore


def test_instrument_no_extra_attributes() -> None:
    """Tests that __slots__ prevents adding new attributes."""
    aapl = Instrument(symbol="AAPL")
    with pytest.raises(FrozenInstanceError):
        aapl.new_field = "unexpected"  # type: ignore
