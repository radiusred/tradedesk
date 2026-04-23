import pytest

from tradedesk.marketdata.indicators.williams_r import WilliamsR
from tradedesk.types import Candle


def candle(high: float, low: float, close: float) -> Candle:
    return Candle(
        timestamp="2020-01-01T00:00:00Z",
        open=close,
        high=high,
        low=low,
        close=close,
        volume=1.0,
        tick_count=1,
    )


class TestWilliamsR:
    def test_returns_none_until_ready(self) -> None:
        wr = WilliamsR(period=3)

        assert wr.update(candle(12, 8, 10)) is None
        assert wr.update(candle(13, 9, 11)) is None
        assert not wr.ready()

        v = wr.update(candle(14, 10, 12))
        assert wr.ready() is True
        assert v is not None

    def test_warmup_periods(self) -> None:
        assert WilliamsR(period=14).warmup_periods() == 14
        assert WilliamsR(period=3).warmup_periods() == 3

    def test_close_at_highest_high_returns_zero(self) -> None:
        wr = WilliamsR(period=3)

        wr.update(candle(10, 5, 8))
        wr.update(candle(12, 6, 9))
        v = wr.update(candle(14, 7, 14))

        assert v == pytest.approx(0.0)

    def test_close_at_lowest_low_returns_minus_100(self) -> None:
        wr = WilliamsR(period=3)

        wr.update(candle(10, 5, 8))
        wr.update(candle(12, 6, 9))
        v = wr.update(candle(14, 7, 5))

        # highest_high=14, lowest_low=5, close=5
        # (14-5)/(14-5) * -100 = -100
        assert v == pytest.approx(-100.0)

    def test_close_at_midpoint_returns_minus_50(self) -> None:
        wr = WilliamsR(period=3)

        wr.update(candle(10, 0, 5))
        wr.update(candle(10, 0, 5))
        v = wr.update(candle(10, 0, 5))

        # highest_high=10, lowest_low=0, close=5
        # (10-5)/(10-0) * -100 = -50
        assert v == pytest.approx(-50.0)

    def test_range_minus_100_to_0(self) -> None:
        wr = WilliamsR(period=3)

        prices = [
            (12, 8, 10),
            (15, 9, 13),
            (11, 7, 9),
            (14, 10, 12),
            (10, 6, 8),
        ]
        for h, l, c in prices:
            v = wr.update(candle(h, l, c))
            if v is not None:
                assert -100.0 <= v <= 0.0

    def test_flat_range_returns_minus_50(self) -> None:
        wr = WilliamsR(period=3)

        wr.update(candle(10, 10, 10))
        wr.update(candle(10, 10, 10))
        v = wr.update(candle(10, 10, 10))

        assert v == pytest.approx(-50.0)

    def test_known_sequence(self) -> None:
        wr = WilliamsR(period=3)

        wr.update(candle(10, 5, 8))
        wr.update(candle(12, 6, 9))
        v = wr.update(candle(11, 7, 10))

        # highest_high=12, lowest_low=5, close=10
        # (12-10)/(12-5) * -100 = -28.571...
        expected = ((12 - 10) / (12 - 5)) * -100.0
        assert v == pytest.approx(expected)

    def test_rolling_window(self) -> None:
        wr = WilliamsR(period=3)

        wr.update(candle(10, 5, 8))
        wr.update(candle(12, 6, 9))
        wr.update(candle(11, 7, 10))

        v = wr.update(candle(20, 15, 18))

        # Window now: (12,6,9), (11,7,10), (20,15,18)
        # highest_high=20, lowest_low=6, close=18
        expected = ((20 - 18) / (20 - 6)) * -100.0
        assert v == pytest.approx(expected)

    def test_reset(self) -> None:
        wr = WilliamsR(period=3)

        wr.update(candle(10, 5, 8))
        wr.update(candle(12, 6, 9))
        wr.update(candle(11, 7, 10))
        assert wr.ready() is True

        wr.reset()
        assert wr.ready() is False
        assert wr.update(candle(10, 5, 8)) is None

    def test_default_period(self) -> None:
        wr = WilliamsR()
        assert wr.period == 14

    def test_overbought_zone(self) -> None:
        wr = WilliamsR(period=3)

        wr.update(candle(10, 5, 9))
        wr.update(candle(12, 6, 11))
        v = wr.update(candle(14, 7, 13.5))

        # highest_high=14, lowest_low=5, close=13.5
        # (14-13.5)/(14-5) * -100 = -5.555...
        assert v > -20.0

    def test_oversold_zone(self) -> None:
        wr = WilliamsR(period=3)

        wr.update(candle(10, 5, 6))
        wr.update(candle(12, 6, 7))
        v = wr.update(candle(14, 7, 7.5))

        # highest_high=14, lowest_low=5, close=7.5
        # (14-7.5)/(14-5) * -100 = -72.222...
        assert v < -70.0

    def test_period_one(self) -> None:
        wr = WilliamsR(period=1)

        v = wr.update(candle(10, 5, 7))
        # highest_high=10, lowest_low=5, close=7
        # (10-7)/(10-5) * -100 = -60
        assert v == pytest.approx(-60.0)
