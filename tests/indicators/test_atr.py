import pytest

from tradedesk.indicators.atr import ATR
from tradedesk.marketdata import Candle


def candle(h: float, l: float, c: float) -> Candle:
    # open is irrelevant for ATR; set it equal to close for simplicity
    return Candle(
        timestamp="2020-01-01T00:00:00Z",
        open=c,
        high=h,
        low=l,
        close=c,
        volume=1.0,
        tick_count=1,
    )


class TestATR:
    def test_rejects_non_positive_period(self) -> None:
        with pytest.raises(ValueError):
            ATR(period=0)
        with pytest.raises(ValueError):
            ATR(period=-1)

    def test_returns_none_until_ready(self) -> None:
        atr = ATR(period=3)
        assert atr.update(candle(10, 8, 9)) is None
        assert atr.update(candle(11, 9, 10)) is None
        v = atr.update(candle(12, 10, 11))
        assert atr.ready() is True
        assert v is not None

    def test_known_values_wilder_smoothing(self) -> None:
        atr = ATR(period=3)

        # Candle 1: TR = high-low = 2
        assert atr.update(candle(10, 8, 9)) is None
        # Candle 2: prev_close=9, TR=max(2,|11-9|=2,|9-9|=0)=2
        assert atr.update(candle(11, 9, 10)) is None
        # Candle 3: prev_close=10, TR=max(2,|12-10|=2,|10-10|=0)=2
        v3 = atr.update(candle(12, 10, 11))
        assert v3 == pytest.approx(2.0)  # seed SMA of [2,2,2]

        # Candle 4: prev_close=11, TR=max(3,|13-11|=2,|10-11|=1)=3
        v4 = atr.update(candle(13, 10, 12))
        # Wilder: (prev_atr*(p-1)+tr)/p = (2*2+3)/3 = 7/3
        assert v4 == pytest.approx(7.0 / 3.0)

    def test_period_one_is_tr_each_time(self) -> None:
        atr = ATR(period=1)

        v1 = atr.update(candle(5, 3, 4))
        assert v1 == pytest.approx(2.0)

        # prev_close=4, TR=max(1,|6-4|=2,|5-4|=1)=2
        v2 = atr.update(candle(6, 5, 5.5))
        assert v2 == pytest.approx(2.0)

    def test_reset(self) -> None:
        atr = ATR(period=2)
        assert atr.update(candle(10, 8, 9)) is None
        assert atr.update(candle(11, 9, 10)) is not None
        assert atr.ready() is True

        atr.reset()
        assert atr.ready() is False
        assert atr.update(candle(12, 10, 11)) is None

    def test_warmup_periods(self) -> None:
        assert ATR(period=14).warmup_periods() == 14
