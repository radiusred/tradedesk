"""Tests for candle aggregation."""

import pytest

from tradedesk.types import Candle
from tradedesk.marketdata.aggregation import CandleAggregator, choose_base_period


def test_aggregates_three_5min_into_one_15min() -> None:
    """Test aggregating three 5-minute candles into one 15-minute candle."""
    agg = CandleAggregator(target_period="15MINUTE", base_period="5MINUTE")
    # factor
    assert agg.describe()[2] == 3

    instrument = "EURUSD"

    # Use aligned timestamps starting at 00:15:00 to ensure all 3 base candles fall in same bucket
    # Bucket [00:15:00, 00:30:00) contains candles at 00:20:00, 00:25:00, 00:29:00
    # Next candle at 00:30:00 triggers emission
    base_ts = 1767225600000
    c1 = Candle(timestamp=base_ts + 20*60*1000, open=100, high=105, low=99, close=104, volume=10, tick_count=1)
    c2 = Candle(timestamp=base_ts + 25*60*1000, open=104, high=106, low=103, close=105, volume=20, tick_count=2)
    c3 = Candle(timestamp=base_ts + 29*60*1000, open=105, high=108, low=104, close=107, volume=30, tick_count=3)
    c4 = Candle(timestamp=base_ts + 35*60*1000, open=107, high=109, low=106, close=108, volume=40, tick_count=4)

    assert agg.update(instrument=instrument, candle=c1) is None
    assert agg.update(instrument=instrument, candle=c2) is None
    assert agg.update(instrument=instrument, candle=c3) is None

    # c4 triggers emission of the 15-min bucket containing c1, c2, c3
    out = agg.update(instrument=instrument, candle=c4)
    assert out is not None

    # Emitted timestamp should be the end of the bucket (00:30:00)
    assert out.timestamp == str(base_ts + 30*60*1000)
    assert out.open == pytest.approx(100.0)
    assert out.high == pytest.approx(108.0)
    assert out.low == pytest.approx(99.0)
    assert out.close == pytest.approx(107.0)
    assert out.volume == pytest.approx(60.0)
    assert out.tick_count == 6


def test_state_is_independent_per_instrument() -> None:
    """Test that aggregation state is independent per instrument."""
    agg = CandleAggregator(target_period="10MINUTE", base_period="5MINUTE")
    # factor
    assert agg.describe()[2] == 2

    inst_a = "EURUSD"
    inst_b = "GBPUSD"

    # Use aligned timestamps: bucket [00:10:00, 00:20:00) contains candles at 00:12:00 and 00:17:00
    # Next candle at 00:22:00 triggers emission
    base_ts = 1767225600000  # 2026-01-01 00:00:00 UTC
    a1 = Candle(timestamp=base_ts + 12*60*1000, open=1, high=2, low=0.5, close=1.5)
    b1 = Candle(timestamp=base_ts + 12*60*1000, open=10, high=11, low=9, close=10.5)
    a2 = Candle(timestamp=base_ts + 17*60*1000, open=1.5, high=3, low=1.4, close=2.5)
    b2 = Candle(timestamp=base_ts + 17*60*1000, open=10.5, high=12, low=10, close=11.5)
    a3 = Candle(timestamp=base_ts + 22*60*1000, open=2.5, high=4, low=2.0, close=3.0)
    b3 = Candle(timestamp=base_ts + 22*60*1000, open=11.5, high=13, low=11, close=12.0)

    assert agg.update(instrument=inst_a, candle=a1) is None
    assert agg.update(instrument=inst_b, candle=b1) is None
    assert agg.update(instrument=inst_a, candle=a2) is None
    assert agg.update(instrument=inst_b, candle=b2) is None

    # a3 and b3 trigger emission of the 10-min buckets containing a1+a2 and b1+b2
    out_a = agg.update(instrument=inst_a, candle=a3)
    assert out_a is not None
    assert out_a.open == pytest.approx(1.0)
    assert out_a.close == pytest.approx(2.5)

    out_b = agg.update(instrument=inst_b, candle=b3)
    assert out_b is not None
    assert out_b.open == pytest.approx(10.0)
    assert out_b.close == pytest.approx(11.5)


def test_choose_base_period_prefers_5minute_when_divisible() -> None:
    """Test that choose_base_period prefers 5MINUTE for periods divisible by 5."""
    assert choose_base_period("15MINUTE") == "5MINUTE"
    assert choose_base_period("10MINUTE") == "5MINUTE"
    assert choose_base_period("30MINUTE") == "5MINUTE"


def test_choose_base_period_hour_passthrough() -> None:
    """Test that HOUR period is passed through unchanged."""
    assert choose_base_period("HOUR") == "HOUR"


def test_choose_base_period_falls_back_to_1minute_when_not_divisible_by_5() -> None:
    """Test that choose_base_period falls back to 1MINUTE for periods not divisible by 5."""
    assert choose_base_period("7MINUTE") == "1MINUTE"
    assert choose_base_period("13MINUTE") == "1MINUTE"


def test_init_raises_on_invalid_period_string() -> None:
    """Test that initialization fails for unsupported period strings."""
    with pytest.raises(ValueError, match="Unsupported period"):
        CandleAggregator(target_period="INVALID")


def test_init_raises_when_target_not_multiple_of_base() -> None:
    """Test that initialization fails if target is not a multiple of base."""
    with pytest.raises(ValueError, match="must be a multiple"):
        CandleAggregator(target_period="7MINUTE", base_period="5MINUTE")


def test_choose_base_period_respects_supported_periods() -> None:
    """Test that base period selection respects the supported_periods list."""
    # 10 is divisible by 5, but if 5 not supported, should pick 1
    base = choose_base_period("10MINUTE", supported_periods=["1MINUTE", "HOUR"])
    assert base == "1MINUTE"


def test_choose_base_period_falls_back_to_second() -> None:
    """Test fallback to SECOND if MINUTE is not supported or suitable."""
    # 1MINUTE target, but only SECOND supported
    base = choose_base_period("1MINUTE", supported_periods=["SECOND"])
    assert base == "SECOND"


def test_choose_base_period_raises_when_no_match() -> None:
    """Test that ValueError is raised when no suitable base period is found."""
    with pytest.raises(ValueError, match="Cannot choose base period"):
        # Target 10min, only HOUR supported (which is larger)
        choose_base_period("10MINUTE", supported_periods=["HOUR"])


def test_reset_clears_instrument_state() -> None:
    """Test that reset() clears the aggregation state for an instrument."""
    agg = CandleAggregator(target_period="10MINUTE", base_period="5MINUTE")
    inst = "EURUSD"
    base_ts = 1767225600000  # 00:00:00

    # Add one candle (00:00)
    c1 = Candle(timestamp=base_ts, open=1, high=1, low=1, close=1)
    agg.update(instrument=inst, candle=c1)

    # Reset
    agg.reset(inst)

    # Add another candle in same bucket (00:05)
    # Since we reset, this starts a fresh bucket state. The previous candle c1 is lost.
    c2 = Candle(timestamp=base_ts + 5 * 60 * 1000, open=2, high=2, low=2, close=2)
    out = agg.update(instrument=inst, candle=c2)
    assert out is None  # Still accumulating

    # Trigger emission with next bucket (00:10)
    c3 = Candle(timestamp=base_ts + 10 * 60 * 1000, open=3, high=3, low=3, close=3)
    out = agg.update(instrument=inst, candle=c3)

    assert out is not None
    # The emitted candle should only contain c2 data.
    # If c1 was included, low would be 1.0. Since c1 lost, low is 2.0.
    assert out.low == pytest.approx(2.0)
    assert out.high == pytest.approx(2.0)


def test_aggregates_seconds_into_minute() -> None:
    """Test aggregating SECOND candles into 1MINUTE."""
    agg = CandleAggregator(target_period="1MINUTE", base_period="SECOND", supported_periods=["SECOND"])
    assert agg.describe()[2] == 60  # factor

    inst = "EURUSD"
    base_ts = 1767225600000  # 00:00:00

    # 00:00:00
    c1 = Candle(timestamp=base_ts, open=10, high=10, low=10, close=10)
    assert agg.update(instrument=inst, candle=c1) is None

    # 00:00:59
    c2 = Candle(timestamp=base_ts + 59000, open=11, high=12, low=11, close=12)
    assert agg.update(instrument=inst, candle=c2) is None

    # 00:01:00 - triggers emission of [00:00:00, 00:01:00)
    c3 = Candle(timestamp=base_ts + 60000, open=13, high=13, low=13, close=13)
    out = agg.update(instrument=inst, candle=c3)

    assert out is not None
    assert out.timestamp == str(base_ts + 60000)  # End of bucket
    assert out.open == pytest.approx(10.0)
    assert out.close == pytest.approx(12.0)
    assert out.high == pytest.approx(12.0)
