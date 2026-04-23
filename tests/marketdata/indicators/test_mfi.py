import pytest

from tradedesk.marketdata.indicators.mfi import MFI
from tradedesk.types import Candle


def candle(
    high: float, low: float, close: float, volume: float = 1000.0
) -> Candle:
    return Candle(
        timestamp="2020-01-01T00:00:00Z",
        open=close,
        high=high,
        low=low,
        close=close,
        volume=volume,
        tick_count=10,
    )


class TestMFI:
    def test_returns_none_until_ready(self) -> None:
        mfi = MFI(period=3)

        assert mfi.update(candle(12, 8, 10)) is None
        assert mfi.update(candle(13, 9, 11)) is None
        assert mfi.update(candle(14, 10, 12)) is None

        v = mfi.update(candle(15, 11, 13))
        assert mfi.ready() is True
        assert v is not None

    def test_warmup_periods(self) -> None:
        assert MFI(period=14).warmup_periods() == 15
        assert MFI(period=3).warmup_periods() == 4

    def test_all_rising_typical_prices_returns_100(self) -> None:
        mfi = MFI(period=3)

        mfi.update(candle(12, 8, 10))
        mfi.update(candle(15, 11, 13))
        mfi.update(candle(18, 14, 16))
        v = mfi.update(candle(21, 17, 19))

        assert v == pytest.approx(100.0)

    def test_all_falling_typical_prices_returns_0(self) -> None:
        mfi = MFI(period=3)

        mfi.update(candle(21, 17, 19))
        mfi.update(candle(18, 14, 16))
        mfi.update(candle(15, 11, 13))
        v = mfi.update(candle(12, 8, 10))

        assert v == pytest.approx(0.0)

    def test_flat_typical_prices_returns_50(self) -> None:
        mfi = MFI(period=3)

        mfi.update(candle(12, 8, 10))
        mfi.update(candle(12, 8, 10))
        mfi.update(candle(12, 8, 10))
        v = mfi.update(candle(12, 8, 10))

        assert v == pytest.approx(50.0)

    def test_range_0_to_100(self) -> None:
        mfi = MFI(period=3)

        prices = [
            (12, 8, 10),
            (15, 9, 13),
            (11, 7, 9),
            (14, 10, 12),
            (10, 6, 8),
            (16, 12, 14),
        ]
        for h, low, c in prices:
            v = mfi.update(candle(h, low, c))
            if v is not None:
                assert 0.0 <= v <= 100.0

    def test_known_sequence(self) -> None:
        mfi = MFI(period=3)

        # candle 0: tp = (12+8+10)/3 = 10.0, no flow comparison yet
        mfi.update(candle(12, 8, 10, volume=100))
        # candle 1: tp = (15+9+12)/3 = 12.0, up -> positive = 12*100 = 1200
        mfi.update(candle(15, 9, 12, volume=100))
        # candle 2: tp = (10+6+8)/3 = 8.0, down -> negative = 8*100 = 800
        mfi.update(candle(10, 6, 8, volume=100))
        # candle 3: tp = (14+10+12)/3 = 12.0, up -> positive = 12*100 = 1200
        v = mfi.update(candle(14, 10, 12, volume=100))

        # positive_mf = 1200 + 0 + 1200 = 2400
        # negative_mf = 0 + 800 + 0 = 800
        # ratio = 2400/800 = 3
        # MFI = 100 - (100 / (1+3)) = 75
        assert v == pytest.approx(75.0)

    def test_uses_tick_count_when_volume_zero(self) -> None:
        mfi = MFI(period=3)

        c = Candle(
            timestamp="2020-01-01T00:00:00Z",
            open=10.0,
            high=12.0,
            low=8.0,
            close=10.0,
            volume=0.0,
            tick_count=50,
        )
        mfi.update(c)

        assert len(mfi.volumes) == 1
        assert mfi.volumes[0] == 50.0

    def test_reset(self) -> None:
        mfi = MFI(period=3)

        mfi.update(candle(12, 8, 10))
        mfi.update(candle(15, 11, 13))
        mfi.update(candle(18, 14, 16))
        mfi.update(candle(21, 17, 19))
        assert mfi.ready() is True

        mfi.reset()
        assert mfi.ready() is False
        assert mfi.update(candle(12, 8, 10)) is None

    def test_rolling_window(self) -> None:
        mfi = MFI(period=3)

        mfi.update(candle(12, 8, 10))
        mfi.update(candle(15, 11, 13))
        mfi.update(candle(18, 14, 16))
        mfi.update(candle(21, 17, 19))

        v1 = mfi.update(candle(24, 20, 22))
        assert v1 is not None

        v2 = mfi.update(candle(27, 23, 25))
        assert v2 is not None

    def test_default_period(self) -> None:
        mfi = MFI()
        assert mfi.period == 14

    def test_volume_weighting_affects_result(self) -> None:
        mfi_low_vol = MFI(period=3)
        mfi_high_vol = MFI(period=3)

        mfi_low_vol.update(candle(12, 8, 10, volume=100))
        mfi_low_vol.update(candle(15, 11, 13, volume=100))
        mfi_low_vol.update(candle(10, 6, 8, volume=100))
        v_low = mfi_low_vol.update(candle(14, 10, 12, volume=100))

        mfi_high_vol.update(candle(12, 8, 10, volume=100))
        mfi_high_vol.update(candle(15, 11, 13, volume=100))
        mfi_high_vol.update(candle(10, 6, 8, volume=9999))
        v_high = mfi_high_vol.update(candle(14, 10, 12, volume=100))

        assert v_low != pytest.approx(v_high)
        assert v_high < v_low
