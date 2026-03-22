from dataclasses import dataclass, field


@dataclass
class InstrumentOpportunity:
    bars: int = 0
    regime_active_bars: int = 0
    regime_on_count: int = 0
    _last_active: bool | None = None

    def on_bar(self, *, active: bool) -> None:
        self.bars += 1
        if active:
            self.regime_active_bars += 1

        # Count False/None -> True transitions
        if active and (self._last_active is False or self._last_active is None):
            self.regime_on_count += 1

        self._last_active = active

    def regime_active_pct(self) -> float:
        if self.bars <= 0:
            return 0.0
        return float(self.regime_active_bars) / float(self.bars)


@dataclass
class OpportunityRecorder:
    """Opportunity/utilisation counters derived from bar-close snapshots.

    Strategy-agnostic:
      - per-instrument: bars, regime-active bars, regime-on transitions
      - portfolio: k-active series (number of concurrently active regimes)

    Designed to be fed from both backtest and live orchestrators.
    """

    per_instrument: dict[str, InstrumentOpportunity] = field(default_factory=dict)
    _k_active_by_ts: list[tuple[str, int]] = field(default_factory=list)

    def on_instrument_bar(self, *, instrument: str, timestamp: str, active: bool) -> None:
        rec = self.per_instrument.get(instrument)
        if rec is None:
            rec = InstrumentOpportunity()
            self.per_instrument[instrument] = rec
        rec.on_bar(active=active)

    def on_portfolio_snapshot(self, *, timestamp: str, k_active: int) -> None:
        # Coalesce by timestamp to avoid duplicates when the orchestrator is called
        # once per instrument for the same candle timestamp.
        if self._k_active_by_ts and self._k_active_by_ts[-1][0] == timestamp:
            self._k_active_by_ts[-1] = (timestamp, int(k_active))
            return
        self._k_active_by_ts.append((timestamp, int(k_active)))

    def k_active_series(self) -> list[int]:
        return [k for _ts, k in self._k_active_by_ts]

    def avg_k_active(self) -> float:
        ks = self.k_active_series()
        if not ks:
            return 0.0
        return float(sum(ks)) / float(len(ks))

    def p95_k_active(self) -> float:
        ks = sorted(self.k_active_series())
        if not ks:
            return 0.0
        idx = int(round(0.95 * (len(ks) - 1)))
        return float(ks[idx])

    def max_k_active(self) -> int:
        ks = self.k_active_series()
        return int(max(ks)) if ks else 0
