from dataclasses import dataclass

from tradedesk.execution.broker import Direction

from .client import BacktestClient


def compute_unrealised_pnl(client: BacktestClient) -> float:
    """Compute unrealised PnL for all open positions using the latest mark price."""
    unreal = 0.0
    for instrument, pos in client.positions.items():
        mark = client.get_mark_price(instrument)
        if mark is None:
            raise RuntimeError(
                f"No mark price available for {instrument} (no data replayed yet)"
            )

        if pos.direction == Direction.LONG:
            unreal += (mark - pos.entry_price) * pos.size
        elif pos.direction == Direction.SHORT:
            unreal += (pos.entry_price - mark) * pos.size
        else:
            raise ValueError(f"Unknown position direction: {pos.direction!r}")

    return float(unreal)


def compute_equity(client: BacktestClient) -> float:
    """Equity = realised PnL + unrealised PnL."""
    return float(client.realised_pnl + compute_unrealised_pnl(client))


@dataclass(frozen=True)
class EquityPoint:
    timestamp: str
    equity: float
