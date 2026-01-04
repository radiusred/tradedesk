import pytest

from tradedesk.marketdata import Candle
from tradedesk.indicators.sma import SMA


def candle(close: float) -> Candle:
    return Candle(
        timestamp="2020-01-01T00:00:00Z",
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
        tick_count=1,
    )


class TestSMA:
    def test_rejects_non_positive_period(self) -> None:
        with pytest.raises(ValueError):
            SMA(period=0)

        with pytest.raises(ValueError):
            SMA(period=-1)

    def test_returns_none_until_ready(self) -> None:
        sma = SMA(period=3)
        assert sma.ready() is False

        assert sma.update(candle(10.0)) is None
        assert sma.ready() is False

        assert sma.update(candle(11.0)) is None
        assert sma.ready() is False

        v = sma.update(candle(12.0))
        assert sma.ready() is True
        assert v == pytest.approx((10.0 + 11.0 + 12.0) / 3.0)

    def test_rolls_window(self) -> None:
        sma = SMA(period=3)
        sma.update(candle(1.0))
        sma.update(candle(2.0))
        sma.update(candle(3.0))
        assert sma.update(candle(4.0)) == pytest.approx((2.0 + 3.0 + 4.0) / 3.0)

    def test_reset(self) -> None:
        sma = SMA(period=2)
        assert sma.update(candle(1.0)) is None
        assert sma.update(candle(3.0)) == pytest.approx(2.0)

        sma.reset()
        assert sma.ready() is False
        assert sma.update(candle(5.0)) is None

    def test_warmup_periods(self) -> None:
        sma = SMA(period=7)
        assert sma.warmup_periods() == 7
