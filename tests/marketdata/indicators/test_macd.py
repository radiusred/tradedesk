import pytest

from tradedesk.marketdata.indicators.macd import MACD
from tradedesk.types import Candle


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


class TestMACD:
    def test_returns_none_until_ready(self) -> None:
        macd = MACD(fast=3, slow=5, signal=3)

        for i in range(4):
            result = macd.update(candle(10.0 + i))
            assert result == {"macd": None, "signal": None, "histogram": None}
            assert macd.ready() is False

    def test_ready_after_warmup(self) -> None:
        macd = MACD(fast=3, slow=5, signal=3)

        for i in range(macd.warmup_periods()):
            macd.update(candle(10.0 + i))

        assert macd.ready() is True

    def test_warmup_periods(self) -> None:
        assert MACD(fast=12, slow=26, signal=9).warmup_periods() == 34
        assert MACD(fast=3, slow=5, signal=3).warmup_periods() == 7

    def test_output_keys(self) -> None:
        macd = MACD(fast=3, slow=5, signal=3)

        for i in range(10):
            result = macd.update(candle(10.0 + i))

        assert "macd" in result
        assert "signal" in result
        assert "histogram" in result

    def test_histogram_is_macd_minus_signal(self) -> None:
        macd = MACD(fast=3, slow=5, signal=3)

        for i in range(10):
            result = macd.update(candle(10.0 + i))

        assert result["histogram"] == pytest.approx(
            result["macd"] - result["signal"]
        )

    def test_flat_prices_produce_zero_macd(self) -> None:
        macd = MACD(fast=3, slow=5, signal=3)

        for _ in range(20):
            result = macd.update(candle(100.0))

        assert result["macd"] == pytest.approx(0.0)
        assert result["signal"] == pytest.approx(0.0)
        assert result["histogram"] == pytest.approx(0.0)

    def test_rising_prices_positive_macd(self) -> None:
        macd = MACD(fast=3, slow=5, signal=3)

        for i in range(20):
            result = macd.update(candle(100.0 + i * 2.0))

        assert result["macd"] > 0

    def test_falling_prices_negative_macd(self) -> None:
        macd = MACD(fast=3, slow=5, signal=3)

        for i in range(20):
            result = macd.update(candle(200.0 - i * 2.0))

        assert result["macd"] < 0

    def test_known_sequence_slow_5_fast_3_signal_3(self) -> None:
        macd = MACD(fast=3, slow=5, signal=3)

        prices = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0]
        results = []
        for p in prices:
            results.append(macd.update(candle(p)))

        last = results[-1]
        assert last["macd"] is not None
        assert last["signal"] is not None
        assert last["histogram"] is not None
        assert last["macd"] > 0

    def test_ema_multipliers(self) -> None:
        macd = MACD(fast=12, slow=26, signal=9)

        assert macd.fast_multiplier == pytest.approx(2 / 13)
        assert macd.slow_multiplier == pytest.approx(2 / 27)
        assert macd.signal_multiplier == pytest.approx(2 / 10)

    def test_reset(self) -> None:
        macd = MACD(fast=3, slow=5, signal=3)

        for i in range(10):
            macd.update(candle(10.0 + i))
        assert macd.ready() is True

        macd.reset()
        assert macd.ready() is False
        result = macd.update(candle(50.0))
        assert result == {"macd": None, "signal": None, "histogram": None}

    def test_default_parameters(self) -> None:
        macd = MACD()
        assert macd.fast_period == 12
        assert macd.slow_period == 26
        assert macd.signal_period == 9

    def test_custom_parameters(self) -> None:
        macd = MACD(fast=8, slow=21, signal=5)
        assert macd.fast_period == 8
        assert macd.slow_period == 21
        assert macd.signal_period == 5

    def test_repr(self) -> None:
        macd = MACD(fast=3, slow=5, signal=3)
        assert "MACD" in repr(macd)
        assert "fast=3" in repr(macd)
        assert "slow=5" in repr(macd)
        assert "signal=3" in repr(macd)
        assert "ready=False" in repr(macd)

    def test_repr_after_ready(self) -> None:
        macd = MACD(fast=3, slow=5, signal=3)
        for i in range(10):
            macd.update(candle(10.0 + i))
        assert "ready=True" in repr(macd)

    def test_fast_ema_initialised_before_slow(self) -> None:
        macd = MACD(fast=3, slow=5, signal=3)

        for i in range(3):
            macd.update(candle(10.0 + i))

        assert macd.fast_ema is not None
        assert macd.slow_ema is None

    def test_signal_not_ready_until_enough_macd_values(self) -> None:
        macd = MACD(fast=3, slow=5, signal=3)

        for i in range(5):
            result = macd.update(candle(10.0 + i))

        assert macd.fast_ema is not None
        assert macd.slow_ema is not None
        assert result == {"macd": None, "signal": None, "histogram": None}

    def test_crossover_detection(self) -> None:
        macd = MACD(fast=3, slow=5, signal=3)

        for i in range(20):
            macd.update(candle(100.0 + i))

        prev_hist = None
        sign_changes = 0
        for i in range(20, 60):
            p = 120.0 + 5.0 * ((-1) ** i)
            result = macd.update(candle(p))
            if result["histogram"] is not None and prev_hist is not None:
                if (result["histogram"] > 0) != (prev_hist > 0):
                    sign_changes += 1
            if result["histogram"] is not None:
                prev_hist = result["histogram"]

        assert sign_changes > 0
