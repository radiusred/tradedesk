import pytest

from tradedesk.indicators.rsi import RSI
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


class TestRSI:
    def test_rejects_non_positive_period(self) -> None:
        with pytest.raises(ValueError):
            RSI(period=0)
        with pytest.raises(ValueError):
            RSI(period=-3)

    def test_not_ready_until_warmup_complete(self) -> None:
        rsi = RSI(period=3)

        assert rsi.update(candle(10.0)) is None  # prev_close
        assert rsi.update(candle(11.0)) is None  # delta 1
        assert rsi.update(candle(12.0)) is None  # delta 2

        v = rsi.update(candle(13.0))             # delta 3 -> first RSI
        assert rsi.ready() is True
        assert v is not None

    def test_all_gains_returns_100(self) -> None:
        rsi = RSI(period=3)

        rsi.update(candle(10.0))
        rsi.update(candle(11.0))
        rsi.update(candle(12.0))
        v = rsi.update(candle(13.0))

        assert v == pytest.approx(100.0)

    def test_all_losses_returns_0(self) -> None:
        rsi = RSI(period=3)

        rsi.update(candle(13.0))
        rsi.update(candle(12.0))
        rsi.update(candle(11.0))
        v = rsi.update(candle(10.0))

        assert v == pytest.approx(0.0)

    def test_known_sequence(self) -> None:
        rsi = RSI(period=3)

        prices = [10.0, 12.0, 11.0, 13.0]
        for p in prices[:-1]:
            rsi.update(candle(p))

        v = rsi.update(candle(prices[-1]))

        # Gains: +2, +0, +2 → avg_gain = 4/3
        # Losses: 0, +1, 0 → avg_loss = 1/3
        rs = (4.0 / 3.0) / (1.0 / 3.0)
        expected = 100.0 - (100.0 / (1.0 + rs))

        assert v == pytest.approx(expected)

    def test_reset(self) -> None:
        rsi = RSI(period=2)

        rsi.update(candle(10.0))
        rsi.update(candle(11.0))
        rsi.update(candle(12.0))
        assert rsi.ready() is True

        rsi.reset()
        assert rsi.ready() is False
        assert rsi.update(candle(20.0)) is None

    def test_warmup_periods(self) -> None:
        assert RSI(period=14).warmup_periods() == 15

    def test_final_seed_branch_executed_on_boundary(self) -> None:
        rsi = RSI(period=3)

        rsi.update(candle(10.0))                 # prev_close
        assert rsi.update(candle(12.0)) is None  # delta 1
        assert rsi.update(candle(11.0)) is None  # delta 2

        v = rsi.update(candle(13.0))             # delta 3 -> first RSI (seeded)
        assert v == pytest.approx(80.0)
        
    def test_wilder_smoothing_step_after_seed(self) -> None:
        rsi = RSI(period=3)

        # Prices produce deltas: +2, -1, +2 (seed), then +1 (smoothing)
        rsi.update(candle(10.0))                 # prev_close
        assert rsi.update(candle(12.0)) is None  # delta 1
        assert rsi.update(candle(11.0)) is None  # delta 2

        v_seed = rsi.update(candle(13.0))        # delta 3 -> seed RSI
        assert v_seed == pytest.approx(80.0)
        assert rsi.ready() is True

        # Next candle triggers Wilder smoothing
        v_next = rsi.update(candle(14.0))        # delta 4 = +1, gain=1 loss=0
        assert v_next is not None

        # Seed averages from first 3 deltas:
        # gains = 2 + 0 + 2 = 4  => avg_gain = 4/3
        # losses = 0 + 1 + 0 = 1 => avg_loss = 1/3
        avg_gain_seed = 4.0 / 3.0
        avg_loss_seed = 1.0 / 3.0

        # Wilder smoothing with period=3 and gain=1, loss=0:
        avg_gain = (avg_gain_seed * 2.0 + 1.0) / 3.0
        avg_loss = (avg_loss_seed * 2.0 + 0.0) / 3.0

        rs = avg_gain / avg_loss
        expected = 100.0 - (100.0 / (1.0 + rs))

        assert v_next == pytest.approx(expected)
