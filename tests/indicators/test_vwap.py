import pytest

from tradedesk.indicators.vwap import VWAP
from tradedesk.marketdata import Candle


def candle(ts: str, h: float, l: float, c: float, v: float) -> Candle:
    return Candle(
        timestamp=ts,
        open=c,
        high=h,
        low=l,
        close=c,
        volume=v,
        tick_count=1,
    )


class TestVWAP:
    def test_returns_none_until_nonzero_volume(self) -> None:
        vwap = VWAP()
        assert vwap.update(candle("2020-01-01T00:00:00Z", 10, 10, 10, 0)) is None
        assert vwap.ready() is False

        out = vwap.update(candle("2020-01-01T00:05:00Z", 10, 10, 10, 2))
        assert out == pytest.approx(10.0)
        assert vwap.ready() is True

    def test_rejects_negative_volume(self) -> None:
        vwap = VWAP()
        with pytest.raises(ValueError):
            vwap.update(candle("2020-01-01T00:00:00Z", 10, 9, 9.5, -1))

    def test_known_values_typical_price(self) -> None:
        # Candle1 typical=(12+6+9)/3=9, vol=2 => pv=18
        # Candle2 typical=(15+9+12)/3=12, vol=1 => pv=12
        # VWAP=(18+12)/(2+1)=30/3=10
        vwap = VWAP(use_typical_price=True, reset_daily_utc=False)

        out1 = vwap.update(candle("2020-01-01T00:00:00Z", 12, 6, 9, 2))
        assert out1 == pytest.approx(9.0)

        out2 = vwap.update(candle("2020-01-01T00:05:00Z", 15, 9, 12, 1))
        assert out2 == pytest.approx(10.0)

    def test_known_values_close_price(self) -> None:
        # Close-only VWAP over two candles:
        # (10*2 + 20*1) / 3 = 40/3
        vwap = VWAP(use_typical_price=False, reset_daily_utc=False)

        vwap.update(candle("2020-01-01T00:00:00Z", 10, 10, 10, 2))
        out = vwap.update(candle("2020-01-01T00:05:00Z", 20, 20, 20, 1))

        assert out == pytest.approx(40.0 / 3.0)

    def test_resets_on_utc_day_change_by_default(self) -> None:
        vwap = VWAP(use_typical_price=False, reset_daily_utc=True)

        out1 = vwap.update(candle("2020-01-01T23:55:00Z", 10, 10, 10, 2))
        assert out1 == pytest.approx(10.0)

        # New day => reset, so VWAP becomes the new candle price
        out2 = vwap.update(candle("2020-01-02T00:00:00Z", 20, 20, 20, 1))
        assert out2 == pytest.approx(20.0)

    def test_does_not_reset_when_disabled(self) -> None:
        vwap = VWAP(use_typical_price=False, reset_daily_utc=False)

        vwap.update(candle("2020-01-01T23:55:00Z", 10, 10, 10, 2))
        out = vwap.update(candle("2020-01-02T00:00:00Z", 20, 20, 20, 1))

        assert out == pytest.approx((10.0 * 2.0 + 20.0 * 1.0) / 3.0)

    def test_reset_method(self) -> None:
        vwap = VWAP(use_typical_price=False, reset_daily_utc=False)

        vwap.update(candle("2020-01-01T00:00:00Z", 10, 10, 10, 2))
        assert vwap.ready() is True

        vwap.reset()
        assert vwap.ready() is False
        assert vwap.update(candle("2020-01-01T00:05:00Z", 20, 20, 20, 1)) == pytest.approx(20.0)

    def test_warmup_periods(self) -> None:
        assert VWAP().warmup_periods() == 1
