import pytest

from tradedesk.indicators.adx import ADX
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


class TestADX:
    def test_rejects_non_positive_period(self) -> None:
        with pytest.raises(ValueError):
            ADX(period=0)
        with pytest.raises(ValueError):
            ADX(period=-1)

    def test_none_until_di_available(self) -> None:
        adx = ADX(period=2)

        assert adx.update(candle(10, 8, 9)) == {"adx": None, "plus_di": None, "minus_di": None}
        out2 = adx.update(candle(11, 9, 10))  # delta 1 (< period): still none
        assert out2 == {"adx": None, "plus_di": None, "minus_di": None}
        assert adx.ready() is False

    def test_di_then_adx_seed_period_2_monotonic_up(self) -> None:
        # Construct a clean monotonic up series with constant TR and +DM:
        # Each delta: TR=2, +DM=1, -DM=0
        # period=2:
        #  - After candle 3: +DI=50, -DI=0, DX=100, ADX not ready yet
        #  - After candle 4: +DI=50, -DI=0, DX=100, ADX seed = 100
        adx = ADX(period=2)

        adx.update(candle(10, 8, 9))
        adx.update(candle(11, 9, 10))

        out3 = adx.update(candle(12, 10, 11))
        assert out3["plus_di"] == pytest.approx(50.0)
        assert out3["minus_di"] == pytest.approx(0.0)
        assert out3["adx"] is None
        assert adx.ready() is False

        out4 = adx.update(candle(13, 11, 12))
        assert out4["plus_di"] == pytest.approx(50.0)
        assert out4["minus_di"] == pytest.approx(0.0)
        assert out4["adx"] == pytest.approx(100.0)
        assert adx.ready() is True

    def test_tr_zero_path_produces_zero_di_and_zero_dx(self) -> None:
        # Flat candles: high==low==close => TR=0, DM=0, DI=0, DX=0
        adx = ADX(period=2)

        adx.update(candle(10, 10, 10))
        adx.update(candle(10, 10, 10))

        out3 = adx.update(candle(10, 10, 10))
        assert out3["plus_di"] == pytest.approx(0.0)
        assert out3["minus_di"] == pytest.approx(0.0)
        assert out3["adx"] is None

        out4 = adx.update(candle(10, 10, 10))
        # ADX seed over two DX=0 values => 0
        assert out4["adx"] == pytest.approx(0.0)
        assert adx.ready() is True

    def test_reset(self) -> None:
        adx = ADX(period=2)
        adx.update(candle(10, 8, 9))
        adx.update(candle(11, 9, 10))
        adx.update(candle(12, 10, 11))
        adx.update(candle(13, 11, 12))
        assert adx.ready() is True

        adx.reset()
        assert adx.ready() is False
        assert adx.update(candle(10, 8, 9)) == {"adx": None, "plus_di": None, "minus_di": None}

    def test_warmup_periods(self) -> None:
        assert ADX(period=14).warmup_periods() == 28
        assert ADX(period=2).warmup_periods() == 4
        
    def test_adx_wilder_smoothing_after_seed(self) -> None:
        # Same monotonic up series used elsewhere:
        # Each delta: TR=2, +DM=1, -DM=0 => DX always 100 once DI available.
        # period=2:
        #  - ADX seed after candle 4 = 100
        #  - Next candle applies Wilder smoothing:
        #      ADX = (prev_adx*(p-1) + DX) / p = (100*1 + 100) / 2 = 100
        adx = ADX(period=2)

        adx.update(candle(10, 8, 9))
        adx.update(candle(11, 9, 10))
        adx.update(candle(12, 10, 11))
        out4 = adx.update(candle(13, 11, 12))
        assert out4["adx"] == pytest.approx(100.0)
        assert adx.ready() is True

        out5 = adx.update(candle(14, 12, 13))  # triggers ADX smoothing branch
        assert out5["adx"] == pytest.approx(100.0)
        assert out5["plus_di"] == pytest.approx(50.0)
        assert out5["minus_di"] == pytest.approx(0.0)
