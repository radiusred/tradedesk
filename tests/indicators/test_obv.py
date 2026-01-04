import pytest

from tradedesk.indicators.obv import OBV
from tradedesk.marketdata import Candle


def candle(ts: str, close: float, vol: float) -> Candle:
    return Candle(
        timestamp=ts,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=vol,
        tick_count=1,
    )


class TestOBV:
    def test_first_candle_returns_none(self) -> None:
        obv = OBV()
        assert obv.update(candle("2020-01-01T00:00:00Z", 10.0, 5.0)) is None
        assert obv.ready() is True  # prev_close exists after first update

    def test_increments_and_decrements(self) -> None:
        obv = OBV()

        assert obv.update(candle("2020-01-01T00:00:00Z", 10.0, 5.0)) is None

        v2 = obv.update(candle("2020-01-01T00:05:00Z", 11.0, 2.0))
        assert v2 == pytest.approx(2.0)

        v3 = obv.update(candle("2020-01-01T00:10:00Z", 9.0, 3.0))
        assert v3 == pytest.approx(-1.0)  # 2 - 3

        v4 = obv.update(candle("2020-01-01T00:15:00Z", 9.0, 10.0))
        assert v4 == pytest.approx(-1.0)  # unchanged on equal close

    def test_rejects_negative_volume(self) -> None:
        obv = OBV()
        with pytest.raises(ValueError):
            obv.update(candle("2020-01-01T00:00:00Z", 10.0, -1.0))

    def test_reset(self) -> None:
        obv = OBV()
        obv.update(candle("2020-01-01T00:00:00Z", 10.0, 5.0))
        obv.update(candle("2020-01-01T00:05:00Z", 11.0, 2.0))
        assert obv.ready() is True

        obv.reset()
        assert obv.ready() is False
        assert obv.update(candle("2020-01-01T00:10:00Z", 20.0, 1.0)) is None

    def test_warmup_periods(self) -> None:
        assert OBV().warmup_periods() == 2
