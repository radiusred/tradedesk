import pytest

from tradedesk.indicators.stochastic import Stochastic
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


class TestStochastic:
    def test_rejects_invalid_params(self) -> None:
        with pytest.raises(ValueError):
            Stochastic(k_period=0, d_period=3)
        with pytest.raises(ValueError):
            Stochastic(k_period=14, d_period=0)

    def test_returns_none_until_k_ready(self) -> None:
        stoch = Stochastic(k_period=3, d_period=2)

        assert stoch.update(candle(10, 8, 9)) == {"k": None, "d": None}
        assert stoch.update(candle(11, 9, 10)) == {"k": None, "d": None}

        out = stoch.update(candle(12, 10, 11))  # first %K available
        assert out["k"] is not None
        assert out["d"] is None
        assert stoch.ready() is False

    def test_known_values_k_and_d(self) -> None:
        stoch = Stochastic(k_period=3, d_period=2)

        stoch.update(candle(10, 8, 9))
        stoch.update(candle(11, 9, 10))

        # Window [1..3]: highs 10,11,12 => 12 ; lows 8,9,10 => 8 ; close 11
        # %K = 100*(11-8)/(12-8) = 75
        out3 = stoch.update(candle(12, 10, 11))
        assert out3["k"] == pytest.approx(75.0)
        assert out3["d"] is None

        # Window [2..4]: highs 11,12,13 => 13 ; lows 9,10,11 => 9 ; close 12
        # %K = 100*(12-9)/(13-9) = 75 ; %D = avg(75,75) = 75
        out4 = stoch.update(candle(13, 11, 12))
        assert out4["k"] == pytest.approx(75.0)
        assert out4["d"] == pytest.approx(75.0)
        assert stoch.ready() is True

    def test_zero_denominator_sets_k_to_zero(self) -> None:
        stoch = Stochastic(k_period=3, d_period=2)

        stoch.update(candle(10, 10, 10))
        stoch.update(candle(10, 10, 10))

        out3 = stoch.update(candle(10, 10, 10))  # denom=0
        assert out3["k"] == pytest.approx(0.0)
        assert out3["d"] is None

        out4 = stoch.update(candle(10, 10, 10))
        assert out4["k"] == pytest.approx(0.0)
        assert out4["d"] == pytest.approx(0.0)

    def test_reset(self) -> None:
        stoch = Stochastic(k_period=2, d_period=2)

        stoch.update(candle(10, 8, 9))
        stoch.update(candle(11, 9, 10))
        assert stoch.update(candle(12, 10, 11))["k"] is not None

        stoch.reset()
        assert stoch.ready() is False
        assert stoch.update(candle(10, 8, 9)) == {"k": None, "d": None}

    def test_warmup_periods(self) -> None:
        assert Stochastic(k_period=14, d_period=3).warmup_periods() == 16
        assert Stochastic(k_period=3, d_period=2).warmup_periods() == 4
