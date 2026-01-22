"""Money Flow Index (MFI) indicator implementation."""

from collections import deque

from tradedesk.marketdata import Candle
from .base import Indicator


class MFI(Indicator):
    """Money Flow Index - volume-weighted momentum (range: 0 to 100)."""

    def __init__(self, period: int = 14):
        self.period = period
        self.typical_prices: deque[float] = deque(maxlen=period + 1)
        self.volumes: deque[float] = deque(maxlen=period + 1)
        self.positive_flows: deque[float] = deque(maxlen=period)
        self.negative_flows: deque[float] = deque(maxlen=period)

    def update(self, candle: Candle) -> float | None:
        typical_price = candle.typical_price
        volume = candle.volume if candle.volume > 0 else float(candle.tick_count)

        self.typical_prices.append(typical_price)
        self.volumes.append(volume)

        if len(self.typical_prices) < 2:
            return None

        raw_money_flow = typical_price * volume

        if typical_price > self.typical_prices[-2]:
            self.positive_flows.append(raw_money_flow)
            self.negative_flows.append(0.0)
        elif typical_price < self.typical_prices[-2]:
            self.positive_flows.append(0.0)
            self.negative_flows.append(raw_money_flow)
        else:
            self.positive_flows.append(0.0)
            self.negative_flows.append(0.0)

        if not self.ready():
            return None

        positive_mf = sum(self.positive_flows)
        negative_mf = sum(self.negative_flows)

        if negative_mf == 0.0:
            # If there is no flow at all, treat as neutral.
            if positive_mf == 0.0:
                return 50.0
            return 100.0

        money_flow_ratio = positive_mf / negative_mf
        mfi = 100 - (100 / (1 + money_flow_ratio))

        return mfi

    def ready(self) -> bool:
        return len(self.positive_flows) >= self.period

    def reset(self) -> None:
        self.typical_prices.clear()
        self.volumes.clear()
        self.positive_flows.clear()
        self.negative_flows.clear()

    def warmup_periods(self) -> int:
        return self.period + 1
