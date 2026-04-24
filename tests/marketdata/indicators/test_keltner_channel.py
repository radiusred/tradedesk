"""Tests for KeltnerChannel indicator."""

import pytest

from tradedesk import Candle
from tradedesk.marketdata.indicators import KeltnerChannel


def _candle(o: float, h: float, lo: float, c: float) -> Candle:
    return Candle(
        timestamp="2024-01-01T00:00:00Z",
        open=o, high=h, low=lo, close=c, volume=1.0, tick_count=1,
    )


class TestKeltnerChannelInit:
    def test_defaults(self) -> None:
        kc = KeltnerChannel()
        assert kc.period == 20
        assert kc.mult == 1.5

    def test_custom_params(self) -> None:
        kc = KeltnerChannel(period=10, mult=2.0)
        assert kc.period == 10
        assert kc.mult == 2.0

    def test_invalid_period(self) -> None:
        with pytest.raises(ValueError, match="period must be > 0"):
            KeltnerChannel(period=0)

    def test_invalid_mult(self) -> None:
        with pytest.raises(ValueError, match="mult must be > 0"):
            KeltnerChannel(mult=0)


class TestKeltnerChannelUpdate:
    def test_not_ready_returns_nones(self) -> None:
        kc = KeltnerChannel(period=5)
        result = kc.update(_candle(100, 102, 98, 101))
        assert result == {"middle": None, "upper": None, "lower": None}
        assert not kc.ready()

    def test_ready_after_enough_candles(self) -> None:
        kc = KeltnerChannel(period=5, mult=1.5)
        for _ in range(5):
            result = kc.update(_candle(100, 102, 98, 100))
        assert kc.ready()
        assert result["middle"] is not None
        assert result["upper"] is not None
        assert result["lower"] is not None

    def test_bands_symmetric_around_middle(self) -> None:
        """With constant candles, upper and lower should be equidistant from middle."""
        kc = KeltnerChannel(period=5, mult=1.5)
        for _ in range(5):
            result = kc.update(_candle(100, 104, 96, 100))

        middle = result["middle"]
        upper = result["upper"]
        lower = result["lower"]
        assert middle is not None and upper is not None and lower is not None
        assert upper > middle > lower
        assert abs((upper - middle) - (middle - lower)) < 1e-10

    def test_wider_mult_produces_wider_bands(self) -> None:
        """Larger mult should produce wider bands."""
        candles = [_candle(100, 104, 96, 100) for _ in range(10)]

        kc_narrow = KeltnerChannel(period=5, mult=1.0)
        kc_wide = KeltnerChannel(period=5, mult=2.0)

        for c in candles:
            r_narrow = kc_narrow.update(c)
            r_wide = kc_wide.update(c)

        assert r_narrow["upper"] is not None and r_wide["upper"] is not None
        narrow_width = r_narrow["upper"] - r_narrow["lower"]  # type: ignore[operator]
        wide_width = r_wide["upper"] - r_wide["lower"]  # type: ignore[operator]
        assert wide_width > narrow_width

    def test_reset_clears_state(self) -> None:
        kc = KeltnerChannel(period=5)
        for _ in range(5):
            kc.update(_candle(100, 102, 98, 100))
        assert kc.ready()
        kc.reset()
        assert not kc.ready()

    def test_warmup_periods(self) -> None:
        kc = KeltnerChannel(period=20)
        assert kc.warmup_periods() == 20

    def test_middle_tracks_ema(self) -> None:
        """Middle line should be the EMA of close prices."""
        from tradedesk.marketdata.indicators import EMA

        kc = KeltnerChannel(period=5, mult=1.5)
        ema = EMA(period=5)
        candles = [
            _candle(100, 103, 97, 101),
            _candle(101, 104, 98, 102),
            _candle(102, 105, 99, 103),
            _candle(103, 106, 100, 104),
            _candle(104, 107, 101, 105),
            _candle(105, 108, 102, 106),
        ]
        for c in candles:
            kc_result = kc.update(c)
            ema_val = ema.update(c)

        assert kc_result["middle"] is not None and ema_val is not None
        assert abs(kc_result["middle"] - ema_val) < 1e-10
