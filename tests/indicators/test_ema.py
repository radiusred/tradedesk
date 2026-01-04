import pytest

from tradedesk.indicators.ema import EMA
from tradedesk.marketdata import Candle


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


class TestEMA:
    def test_rejects_non_positive_period(self) -> None:
        with pytest.raises(ValueError):
            EMA(period=0)

        with pytest.raises(ValueError):
            EMA(period=-5)

    def test_returns_none_until_ready(self) -> None:
        ema = EMA(period=3)

        assert ema.update(candle(10.0)) is None
        assert ema.ready() is False

        assert ema.update(candle(11.0)) is None
        assert ema.ready() is False

        v = ema.update(candle(12.0))
        assert ema.ready() is True
        assert v is not None

    def test_constant_input_converges_immediately(self) -> None:
        ema = EMA(period=5)

        for _ in range(5):
            v = ema.update(candle(10.0))

        assert ema.ready() is True
        assert v == pytest.approx(10.0)

    def test_known_sequence(self) -> None:
        ema = EMA(period=3)
        alpha = 2.0 / (3.0 + 1.0)

        # Seed
        assert ema.update(candle(10.0)) is None
        assert ema.update(candle(11.0)) is None

        # First ready value
        v = ema.update(candle(12.0))
        # EMA steps:
        # seed = 10
        # ema1 = 10 + a*(11-10)
        # ema2 = ema1 + a*(12-ema1)
        ema1 = 10.0 + alpha * (11.0 - 10.0)
        ema2 = ema1 + alpha * (12.0 - ema1)

        assert v == pytest.approx(ema2)

    def test_reset(self) -> None:
        ema = EMA(period=2)

        ema.update(candle(1.0))
        ema.update(candle(2.0))
        assert ema.ready() is True

        ema.reset()
        assert ema.ready() is False
        assert ema.update(candle(5.0)) is None

    def test_warmup_periods(self) -> None:
        ema = EMA(period=7)
        assert ema.warmup_periods() == 7
