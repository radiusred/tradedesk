import math

import pytest

from tradedesk.indicators.bollinger_bands import BollingerBands
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


class TestBollingerBands:
    def test_rejects_invalid_params(self) -> None:
        with pytest.raises(ValueError):
            BollingerBands(period=0)
        with pytest.raises(ValueError):
            BollingerBands(period=-1)
        with pytest.raises(ValueError):
            BollingerBands(period=20, k=0)
        with pytest.raises(ValueError):
            BollingerBands(period=20, k=-2)

    def test_returns_none_until_ready(self) -> None:
        bb = BollingerBands(period=3, k=2.0)

        assert bb.update(candle(10.0)) == {"middle": None, "upper": None, "lower": None, "std": None}
        assert bb.update(candle(11.0)) == {"middle": None, "upper": None, "lower": None, "std": None}

        out = bb.update(candle(12.0))
        assert bb.ready() is True
        assert out["middle"] is not None
        assert out["upper"] is not None
        assert out["lower"] is not None
        assert out["std"] is not None

    def test_known_values_period_2_k_2(self) -> None:
        # closes: [10, 14]
        # mean = 12
        # var = ((-2)^2 + (2)^2) / 2 = 4
        # std = 2
        # upper/lower = 12 +/- 2*2 => 16, 8
        bb = BollingerBands(period=2, k=2.0)

        assert bb.update(candle(10.0))["middle"] is None
        out = bb.update(candle(14.0))

        assert out["middle"] == pytest.approx(12.0)
        assert out["std"] == pytest.approx(2.0)
        assert out["upper"] == pytest.approx(16.0)
        assert out["lower"] == pytest.approx(8.0)

    def test_uses_population_std_ddof_zero(self) -> None:
        # closes: [0, 1, 2]
        # mean = 1
        # var (ddof=0) = (1 + 0 + 1) / 3 = 2/3
        # std = sqrt(2/3)
        bb = BollingerBands(period=3, k=1.0)

        bb.update(candle(0.0))
        bb.update(candle(1.0))
        out = bb.update(candle(2.0))

        expected_std = math.sqrt(2.0 / 3.0)
        assert out["middle"] == pytest.approx(1.0)
        assert out["std"] == pytest.approx(expected_std)
        assert out["upper"] == pytest.approx(1.0 + expected_std)
        assert out["lower"] == pytest.approx(1.0 - expected_std)

    def test_rolls_window(self) -> None:
        bb = BollingerBands(period=3, k=2.0)
        bb.update(candle(1.0))
        bb.update(candle(2.0))
        bb.update(candle(3.0))
        out = bb.update(candle(4.0))  # window now [2,3,4]

        mean = (2.0 + 3.0 + 4.0) / 3.0
        var = ((2 - mean) ** 2 + (3 - mean) ** 2 + (4 - mean) ** 2) / 3.0
        std = math.sqrt(var)

        assert out["middle"] == pytest.approx(mean)
        assert out["std"] == pytest.approx(std)
        assert out["upper"] == pytest.approx(mean + 2.0 * std)
        assert out["lower"] == pytest.approx(mean - 2.0 * std)

    def test_reset(self) -> None:
        bb = BollingerBands(period=2, k=2.0)
        bb.update(candle(10.0))
        assert bb.update(candle(14.0))["middle"] is not None

        bb.reset()
        assert bb.ready() is False
        assert bb.update(candle(1.0))["middle"] is None

    def test_warmup_periods(self) -> None:
        assert BollingerBands(period=20, k=2.0).warmup_periods() == 20
