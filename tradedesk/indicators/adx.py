"""Average Directional Index (ADX) indicator implementation (Wilder)."""

from tradedesk.marketdata import Candle
from .base import Indicator


class ADX(Indicator):
    """
    ADX (Average Directional Index), Wilder smoothing.

    Computes:
      +DI, -DI, ADX

    True Range (TR):
      TR = max(high - low, abs(high - prev_close), abs(low - prev_close))

    Directional Movement:
      up_move   = high - prev_high
      down_move = prev_low - low

      +DM = up_move   if up_move > down_move and up_move > 0 else 0
      -DM = down_move if down_move > up_move and down_move > 0 else 0

    Wilder smoothing:
      smoothed = prev_smoothed - (prev_smoothed / period) + current

    DX:
      DX = 100 * abs(+DI - -DI) / (+DI + -DI)  (0 if denom == 0)

    ADX:
      - Seed with SMA(DX) over `period` DX values
      - Then Wilder smooth: ADX = (prev_adx*(period-1) + DX) / period
    """

    def __init__(self, period: int = 14):
        if period <= 0:
            raise ValueError("period must be > 0")

        self.period = period

        self._prev_high: float | None = None
        self._prev_low: float | None = None
        self._prev_close: float | None = None

        # Seeding sums (first `period` deltas)
        self._seed_tr_sum: float = 0.0
        self._seed_pdm_sum: float = 0.0
        self._seed_mdm_sum: float = 0.0
        self._delta_count: int = 0

        # Wilder-smoothed values (become valid after seeding)
        self._tr: float | None = None
        self._pdm: float | None = None
        self._mdm: float | None = None

        # DX/ADX seeding and smoothing
        self._dx_seed_sum: float = 0.0
        self._dx_count: int = 0
        self._adx: float | None = None

    def update(self, candle: Candle) -> dict[str, float | None]:
        high = float(candle.high)
        low = float(candle.low)
        close = float(candle.close)

        if self._prev_high is None:
            self._prev_high, self._prev_low, self._prev_close = high, low, close
            return {"adx": None, "plus_di": None, "minus_di": None}

        # TR
        tr = max(
            high - low,
            abs(high - (self._prev_close or close)),
            abs(low - (self._prev_close or close)),
        )

        # DM
        assert self._prev_high is not None and self._prev_low is not None
        up_move = high - self._prev_high
        down_move = self._prev_low - low

        pdm = up_move if (up_move > down_move and up_move > 0.0) else 0.0
        mdm = down_move if (down_move > up_move and down_move > 0.0) else 0.0

        self._delta_count += 1

        # Seeding phase for TR/+DM/-DM (need `period` deltas)
        if self._tr is None:
            self._seed_tr_sum += tr
            self._seed_pdm_sum += pdm
            self._seed_mdm_sum += mdm

            self._prev_high, self._prev_low, self._prev_close = high, low, close

            if self._delta_count < self.period:
                return {"adx": None, "plus_di": None, "minus_di": None}

            # Final seed -> smoothed values start here
            self._tr = self._seed_tr_sum
            self._pdm = self._seed_pdm_sum
            self._mdm = self._seed_mdm_sum

            plus_di, minus_di = self._compute_di(self._tr, self._pdm, self._mdm)
            dx = self._compute_dx(plus_di, minus_di)
            return self._update_adx(dx, plus_di, minus_di)

        # Wilder smoothing phase
        assert self._tr is not None and self._pdm is not None and self._mdm is not None
        self._tr = self._tr - (self._tr / self.period) + tr
        self._pdm = self._pdm - (self._pdm / self.period) + pdm
        self._mdm = self._mdm - (self._mdm / self.period) + mdm

        self._prev_high, self._prev_low, self._prev_close = high, low, close

        plus_di, minus_di = self._compute_di(self._tr, self._pdm, self._mdm)
        dx = self._compute_dx(plus_di, minus_di)
        return self._update_adx(dx, plus_di, minus_di)

    @staticmethod
    def _compute_di(tr: float, pdm: float, mdm: float) -> tuple[float, float]:
        if tr == 0.0:
            return 0.0, 0.0
        return 100.0 * (pdm / tr), 100.0 * (mdm / tr)

    @staticmethod
    def _compute_dx(plus_di: float, minus_di: float) -> float:
        denom = plus_di + minus_di
        if denom == 0.0:
            return 0.0
        return 100.0 * abs(plus_di - minus_di) / denom

    def _update_adx(
        self, dx: float, plus_di: float, minus_di: float
    ) -> dict[str, float | None]:
        # Seed ADX with SMA of first `period` DX values
        if self._adx is None:
            self._dx_seed_sum += dx
            self._dx_count += 1

            if self._dx_count < self.period:
                return {"adx": None, "plus_di": plus_di, "minus_di": minus_di}

            self._adx = self._dx_seed_sum / self.period
            return {"adx": self._adx, "plus_di": plus_di, "minus_di": minus_di}

        # Wilder smoothing for ADX
        self._adx = (self._adx * (self.period - 1) + dx) / self.period
        return {"adx": self._adx, "plus_di": plus_di, "minus_di": minus_di}

    def ready(self) -> bool:
        return self._adx is not None

    def reset(self) -> None:
        self._prev_high = None
        self._prev_low = None
        self._prev_close = None

        self._seed_tr_sum = 0.0
        self._seed_pdm_sum = 0.0
        self._seed_mdm_sum = 0.0
        self._delta_count = 0

        self._tr = None
        self._pdm = None
        self._mdm = None

        self._dx_seed_sum = 0.0
        self._dx_count = 0
        self._adx = None

    def warmup_periods(self) -> int:
        # Need `period` deltas to seed TR/DM, then `period` DX values to seed ADX
        # => first ADX after 2*period candles (period=2 => 4 candles)
        return 2 * self.period
