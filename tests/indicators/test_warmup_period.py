import pytest

from tradedesk.indicators.macd import MACD
from tradedesk.indicators.mfi import MFI
from tradedesk.indicators.williams_r import WilliamsR
from tradedesk.indicators.sma import SMA


class TestIndicatorWarmupPeriods:
    @pytest.mark.parametrize(
        "indicator, expected",
        [
            (WilliamsR(period=14), 14),
            (MFI(period=14), 15),
            (MACD(fast=12, slow=26, signal=9), 26 + 9 - 1),
            (SMA(period=14), 14),
        ],
    )
    def test_warmup_periods_returns_expected(self, indicator, expected):
        assert indicator.warmup_periods() == expected

    def test_williams_r_ready_on_warmup_boundary(self, candle_factory):
        wr = WilliamsR(period=14)
        warmup = wr.warmup_periods()

        for i in range(warmup - 1):
            assert wr.update(candle_factory(i)) is None
            assert wr.ready() is False

        out = wr.update(candle_factory(warmup - 1))
        assert wr.ready() is True
        assert out is not None

    def test_mfi_ready_on_warmup_boundary(self, candle_factory):
        mfi = MFI(period=14)
        warmup = mfi.warmup_periods()

        for i in range(warmup - 1):
            assert mfi.update(candle_factory(i)) is None
            assert mfi.ready() is False

        out = mfi.update(candle_factory(warmup - 1))
        assert mfi.ready() is True
        assert out is not None

    def test_macd_ready_on_warmup_boundary(self, candle_factory):
        macd = MACD(fast=12, slow=26, signal=9)
        warmup = macd.warmup_periods()

        for i in range(warmup - 1):
            out = macd.update(candle_factory(i))
            assert macd.ready() is False
            assert out == {"macd": None, "signal": None, "histogram": None}

        out = macd.update(candle_factory(warmup - 1))
        assert macd.ready() is True
        assert out["macd"] is not None
        assert out["signal"] is not None
        assert out["histogram"] is not None
