"""Tests for portfolio risk allocation policies."""

from tradedesk.portfolio.risk import EqualSplitRiskPolicy
from tradedesk.portfolio.types import Instrument, SleeveId


def test_equal_split_allocates_per_active_sleeve():
    """Test that EqualSplitRiskPolicy divides budget equally across active sleeves."""
    p = EqualSplitRiskPolicy(portfolio_risk_budget=10.0)

    # No active sleeves -> empty allocation
    assert p.allocate({}) == {}

    # One active sleeve -> gets full budget
    a = p.allocate({SleeveId("AdaptiveFade_EURUSD"): Instrument("CS.D.EURUSD.TODAY.IP")})
    assert a[SleeveId("AdaptiveFade_EURUSD")] == 10.0

    # Two active sleeves -> split equally
    ab = p.allocate({
        SleeveId("AdaptiveFade_EURUSD"): Instrument("CS.D.EURUSD.TODAY.IP"),
        SleeveId("BollingerReversion_GBPUSD"): Instrument("CS.D.GBPUSD.TODAY.IP"),
    })
    assert ab[SleeveId("AdaptiveFade_EURUSD")] == 5.0
    assert ab[SleeveId("BollingerReversion_GBPUSD")] == 5.0

    # Two active sleeves on the same instrument -> each gets 5.0 independently
    dual = p.allocate({
        SleeveId("AdaptiveFade_AUDCAD"): Instrument("CS.D.AUDCAD.TODAY.IP"),
        SleeveId("BollingerReversion_AUDCAD"): Instrument("CS.D.AUDCAD.TODAY.IP"),
    })
    assert dual[SleeveId("AdaptiveFade_AUDCAD")] == 5.0
    assert dual[SleeveId("BollingerReversion_AUDCAD")] == 5.0
