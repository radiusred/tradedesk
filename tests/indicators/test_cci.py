import pytest

from tradedesk.indicators.cci import CCI
from tradedesk.marketdata import Candle


def candle(h: float, l: float, c: float) -> Candle:
    return Candle(
        timestamp="2020-01-01T00:00:00Z",
        open=c,
        high=h,
        low=l,
        close=c,
        volume=1.0,
        tick_count=1,
    )


class TestCCI:
    def test_rejects_non_positive_period(self) -> None:
        with pytest.raises(ValueError):
            CCI(period=0)
        with pytest.raises(ValueError):
            CCI(period=-5)

    def test_returns_none_until_ready(self) -> None:
        cci = CCI(period=3)

        assert cci.update(candle(10, 8, 9)) is None
        assert cci.update(candle(11, 9, 10)) is None

        v = cci.update(candle(12, 10, 11))
        assert cci.ready() is True
        assert v is not None

    def test_known_values(self) -> None:
        # Typical prices:
        # (12+6+9)/3=9
        # (15+9+12)/3=12
        # (18+12+15)/3=15
        # mean=12
        # mean deviation=(3+0+3)/3=2
        # CCI(last)=(15-12)/(0.015*2)=100
        cci = CCI(period=3)

        cci.update(candle(12, 6, 9))
        cci.update(candle(15, 9, 12))
        v = cci.update(candle(18, 12, 15))

        assert v == pytest.approx(100.0)

    def test_zero_mean_deviation_returns_zero(self) -> None:
        cci = CCI(period=3)

        cci.update(candle(10, 10, 10))
        cci.update(candle(10, 10, 10))
        v = cci.update(candle(10, 10, 10))

        assert v == pytest.approx(0.0)

    def test_rolls_window(self) -> None:
        cci = CCI(period=3)

        cci.update(candle(10, 8, 9))
        cci.update(candle(11, 9, 10))
        cci.update(candle(12, 10, 11))
        v = cci.update(candle(13, 11, 12))  # window rolls

        assert v is not None

    def test_reset(self) -> None:
        cci = CCI(period=2)

        cci.update(candle(10, 8, 9))
        assert cci.update(candle(11, 9, 10)) is not None

        cci.reset()
        assert cci.ready() is False
        assert cci.update(candle(20, 18, 19)) is None

    def test_warmup_periods(self) -> None:
        assert CCI(period=20).warmup_periods() == 20
