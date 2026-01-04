# tests/test_indicators.py
import math

import pytest

from tradedesk.marketdata import Candle
from tradedesk.indicators import MACD, MFI, WilliamsR, SMA

def candle(
    open_: float,
    high: float,
    low: float,
    close: float,
    *,
    volume: float = 1.0,
    tick_count: int = 1,
    timestamp: str = "1970-01-01T00:00:00Z",
) -> Candle:
    return Candle(
        timestamp=timestamp,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        tick_count=tick_count,
    )

class TestIndicators:
    # -------------------------
    # Williams %R
    # -------------------------
    def test_williams_r_not_ready_until_period(self) -> None:
        wr = WilliamsR(period=3)

        assert wr.update(candle(1, 2, 0, 1)) is None
        assert wr.ready() is False

        assert wr.update(candle(1, 3, 0, 2)) is None
        assert wr.ready() is False

        v = wr.update(candle(2, 4, 1, 3))
        assert wr.ready() is True
        assert isinstance(v, float)


    def test_williams_r_known_value(self) -> None:
        # period=3
        # highs: [2, 3, 4] -> HH=4
        # lows:  [0, 0, 1] -> LL=0
        # close: last close=3
        # %R = -100 * (HH - close) / (HH - LL) = -100 * (1/4) = -25
        wr = WilliamsR(period=3)
        assert wr.update(candle(1, 2, 0, 1)) is None
        assert wr.update(candle(1, 3, 0, 2)) is None
        v = wr.update(candle(2, 4, 1, 3))
        assert v == pytest.approx(-25.0)


    def test_williams_r_flat_range_returns_minus_50(self) -> None:
        wr = WilliamsR(period=3)

        # All highs == lows == 10 -> HH==LL -> defined as -50.0
        assert wr.update(candle(10, 10, 10, 10)) is None
        assert wr.update(candle(10, 10, 10, 10)) is None
        v = wr.update(candle(10, 10, 10, 10))
        assert v == pytest.approx(-50.0)


    def test_williams_r_reset(self) -> None:
        wr = WilliamsR(period=2)
        assert wr.update(candle(1, 2, 0, 1)) is None
        v = wr.update(candle(1, 2, 0, 1))
        assert v is not None
        assert wr.ready()

        wr.reset()
        assert wr.ready() is False
        assert wr.update(candle(1, 2, 0, 1)) is None


    # -------------------------
    # MFI
    # -------------------------
    def test_mfi_not_ready_until_period_classified_flows(self) -> None:
        # MFI needs a previous typical price to classify the first flow,
        # so ready only after period classified flows.
        mfi = MFI(period=3)

        # 1st candle -> no prev TP => None
        assert mfi.update(candle(1, 2, 0, 1, volume=10)) is None
        assert mfi.ready() is False

        # 2nd candle -> 1 classified flow, still not ready
        assert mfi.update(candle(1, 3, 0, 2, volume=10)) is None
        assert mfi.ready() is False

        # 3rd candle -> 2 classified flows, still not ready
        assert mfi.update(candle(2, 4, 1, 3, volume=10)) is None
        assert mfi.ready() is False

        # 4th candle -> 3 classified flows => ready
        v = mfi.update(candle(3, 5, 2, 4, volume=10))
        assert mfi.ready() is True
        assert isinstance(v, float)


    def test_mfi_known_value_simple_sequence(self) -> None:
        """
        Construct a small, deterministic sequence with period=3 where we can compute
        expected MFI exactly.

        Use constant volume=1.
        Candle typical prices:
        tp1=1, tp2=2, tp3=3, tp4=2
        Classified flows (rmf = tp * vol):
        from tp1->tp2: +2
        tp2->tp3: +3
        tp3->tp4: -2
        Over last 3 flows: pos=2+3=5, neg=2
        ratio=2.5
        MFI = 100 - 100/(1+2.5) = 100 - 100/3.5 = 71.428571...
        """
        mfi = MFI(period=3)

        # Make candles such that typical_price equals the close:
        # choose high=low=close so tp=(h+l+c)/3 = close
        assert mfi.update(candle(1, 1, 1, 1, volume=1)) is None  # tp=1
        assert mfi.update(candle(2, 2, 2, 2, volume=1)) is None  # +2
        assert mfi.update(candle(3, 3, 3, 3, volume=1)) is None  # +3
        v = mfi.update(candle(2, 2, 2, 2, volume=1))             # -2 => ready
        assert v == pytest.approx(100.0 - (100.0 / (1.0 + (5.0 / 2.0))), rel=1e-12)


    def test_mfi_volume_fallback_to_tick_count(self) -> None:
        # If volume is 0, MFI uses tick_count as volume surrogate.
        mfi = MFI(period=2)

        # First candle: no prev TP
        assert mfi.update(candle(1, 1, 1, 1, volume=0, tick_count=10)) is None
        # Second candle: classify one flow (+)
        assert mfi.update(candle(2, 2, 2, 2, volume=0, tick_count=10)) is None
        # Third candle: classify second flow (+) => ready => neg=0 pos>0 => 100
        v = mfi.update(candle(3, 3, 3, 3, volume=0, tick_count=10))
        assert v == pytest.approx(100.0)


    def test_mfi_neutral_when_no_flow(self) -> None:
        # With your patch: if pos==0 and neg==0 => return 50.0
        mfi = MFI(period=2)

        # constant typical_price: no positive/negative flows
        assert mfi.update(candle(1, 1, 1, 1, volume=1)) is None
        assert mfi.update(candle(1, 1, 1, 1, volume=1)) is None
        v = mfi.update(candle(1, 1, 1, 1, volume=1))
        assert v == pytest.approx(50.0)


    def test_mfi_reset(self) -> None:
        mfi = MFI(period=2)
        assert mfi.update(candle(1, 1, 1, 1, volume=1)) is None
        assert mfi.update(candle(2, 2, 2, 2, volume=1)) is None
        v = mfi.update(candle(3, 3, 3, 3, volume=1))
        assert v is not None
        assert mfi.ready()

        mfi.reset()
        assert mfi.ready() is False
        assert mfi.update(candle(1, 1, 1, 1, volume=1)) is None


    # -------------------------
    # MACD
    # -------------------------
    def test_macd_returns_stable_dict_shape_during_warmup(self) -> None:
        macd = MACD(fast=3, slow=5, signal=2)

        out1 = macd.update(candle(1, 1, 1, 1))
        assert set(out1.keys()) == {"macd", "signal", "histogram"}
        assert out1["macd"] is None
        assert out1["signal"] is None
        assert out1["histogram"] is None
        assert macd.ready() is False

        # Still warming up until both EMAs exist
        out2 = macd.update(candle(2, 2, 2, 2))
        assert set(out2.keys()) == {"macd", "signal", "histogram"}


    def test_macd_strict_ready_only_when_signal_available(self) -> None:
        # With SMA seeding:
        # - fast EMA initialises after fast candles
        # - slow EMA initialises after slow candles
        # - signal EMA initialises after signal macd values are collected
        macd = MACD(fast=3, slow=5, signal=2)

        # Provide enough candles to exceed warm-up comfortably
        outputs = []
        for price in [1, 2, 3, 4, 5, 6, 7, 8]:
            outputs.append(macd.update(candle(price, price, price, price)))

        # Eventually must be ready
        assert any(o["macd"] is not None for o in outputs)
        assert macd.ready() is True

        last = outputs[-1]
        assert last["macd"] is not None
        assert last["signal"] is not None
        assert last["histogram"] is not None
        # Histogram identity
        assert last["histogram"] == pytest.approx(last["macd"] - last["signal"], rel=1e-12)


    def test_macd_reset(self) -> None:
        macd = MACD(fast=3, slow=5, signal=2)

        for price in [1, 2, 3, 4, 5, 6, 7]:
            macd.update(candle(price, price, price, price))

        assert macd.ready() is True

        macd.reset()
        assert macd.ready() is False
        out = macd.update(candle(1, 1, 1, 1))
        assert out == {"macd": None, "signal": None, "histogram": None}
