"""Tests for tradedesk.recording.ledger – TradeLedger."""

import csv

import pytest

from tradedesk.recording.excursions import CandleIndex, build_candle_index
from tradedesk.recording.ledger import TradeLedger, trade_rows_from_trades
from tradedesk.recording.types import EquityRecord, RecordingMode, TradeRecord
from tradedesk.types import Candle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trade(
    instrument="USDJPY",
    direction="BUY",
    price=150.0,
    size=1.0,
    ts="2025-01-15T12:00:00Z",
    reason="",
):
    return TradeRecord(
        timestamp=ts,
        instrument=instrument,
        direction=direction,
        size=size,
        price=price,
        reason=reason,
    )


def _equity(ts="2025-01-15T12:00:00Z", eq=10000.0):
    return EquityRecord(timestamp=ts, equity=eq)


def _read_csv(path):
    with path.open() as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# trade_rows_from_trades
# ---------------------------------------------------------------------------


class TestTradeRowsFromTrades:
    def test_converts_trade_records(self):
        trades = [_trade(price=150.0, size=1.5)]
        rows = trade_rows_from_trades(trades)
        assert len(rows) == 1
        assert rows[0]["instrument"] == "USDJPY"
        assert rows[0]["price"] == "150.0"
        assert rows[0]["size"] == "1.5"


# ---------------------------------------------------------------------------
# TradeLedger – backtest mode
# ---------------------------------------------------------------------------


class TestTradeLedgerBacktest:
    def test_record_trade(self):
        ledger = TradeLedger()
        ledger.record_trade(_trade())
        assert len(ledger.trades) == 1

    def test_record_equity(self):
        ledger = TradeLedger()
        ledger.record_equity(_equity())
        assert len(ledger.equity) == 1

    def test_record_equity_coalesces_same_timestamp(self):
        ledger = TradeLedger()
        ledger.record_equity(_equity(ts="t1", eq=100.0))
        ledger.record_equity(_equity(ts="t1", eq=200.0))
        assert len(ledger.equity) == 1
        assert ledger.equity[0].equity == 200.0

    def test_record_equity_different_timestamps(self):
        ledger = TradeLedger()
        ledger.record_equity(_equity(ts="t1"))
        ledger.record_equity(_equity(ts="t2"))
        assert len(ledger.equity) == 2

    def test_write_trades_csv(self, tmp_path):
        ledger = TradeLedger()
        ledger.record_trade(_trade(reason="entry"))
        path = tmp_path / "trades.csv"
        ledger.write_trades_csv(path)
        rows = _read_csv(path)
        assert len(rows) == 1
        assert rows[0]["instrument"] == "USDJPY"
        assert rows[0]["reason"] == "entry"

    def test_write_equity_csv(self, tmp_path):
        ledger = TradeLedger()
        ledger.record_equity(_equity(ts="2025-01-15T12:00:00Z", eq=10500.0))
        path = tmp_path / "equity.csv"
        ledger.write_equity_csv(path)
        rows = _read_csv(path)
        assert len(rows) == 1
        assert rows[0]["equity"] == "10500.0"

    def test_write_round_trips_csv(self, tmp_path):
        ledger = TradeLedger()
        # Entry
        ledger.record_trade(_trade(direction="BUY", price=150.0, ts="2025-01-15T12:00:00Z"))
        # Exit
        ledger.record_trade(
            _trade(
                direction="SELL",
                price=152.0,
                ts="2025-01-15T13:00:00Z",
                reason="take_profit",
            )
        )
        path = tmp_path / "round_trips.csv"
        ledger.write_round_trips_csv(path)
        rows = _read_csv(path)
        assert len(rows) == 1
        assert rows[0]["direction"] == "LONG"
        assert rows[0]["exit_reason"] == "take_profit"

    def test_write_equity_daily_csv_empty(self, tmp_path):
        ledger = TradeLedger()
        path = tmp_path / "equity_daily.csv"
        ledger.write_equity_daily_csv(path)
        rows = _read_csv(path)
        assert len(rows) == 0

    def test_write_equity_daily_csv_fills_gaps(self, tmp_path):
        ledger = TradeLedger()
        # Monday
        ledger.record_equity(_equity(ts="2025-01-13T12:00:00Z", eq=10000.0))
        # Wednesday (skip Tuesday)
        ledger.record_equity(_equity(ts="2025-01-15T12:00:00Z", eq=10200.0))
        path = tmp_path / "equity_daily.csv"
        ledger.write_equity_daily_csv(path)
        rows = _read_csv(path)
        # Should have 3 rows: Mon, Tue (forward-filled), Wed
        assert len(rows) == 3
        # Tuesday should be forward-filled with Monday's equity
        assert rows[1]["date"] == "2025-01-14"
        assert rows[1]["equity"] == "10000.0"

    def test_write_exposure_csv(self, tmp_path):
        ledger = TradeLedger()
        ledger.record_trade(_trade(direction="BUY", price=150.0, ts="2025-01-15T12:00:00Z"))
        ledger.record_trade(_trade(direction="SELL", price=152.0, ts="2025-01-15T13:00:00Z"))
        ledger.record_equity(_equity(ts="2025-01-15T12:30:00Z"))
        path = tmp_path / "exposure.csv"
        ledger.write_exposure_csv(path)
        rows = _read_csv(path)
        assert len(rows) == 1
        # During the round trip, 1 position was open
        assert int(rows[0]["open_positions"]) == 1

    def test_write_opportunity_csv_empty(self, tmp_path):
        ledger = TradeLedger()
        path = tmp_path / "opportunity.csv"
        ledger.write_opportunity_csv(path)
        rows = _read_csv(path)
        assert len(rows) == 0

    def test_write_opportunity_csv_with_data(self, tmp_path):
        ledger = TradeLedger()
        ledger.opportunity.on_instrument_bar(instrument="USDJPY", timestamp="t1", active=True)
        ledger.opportunity.on_portfolio_snapshot(timestamp="t1", k_active=1)
        path = tmp_path / "opportunity.csv"
        ledger.write_opportunity_csv(path)
        rows = _read_csv(path)
        assert len(rows) == 1
        assert rows[0]["instrument"] == "USDJPY"

    def test_write_batch(self, tmp_path):
        ledger = TradeLedger()
        ledger.record_trade(_trade(direction="BUY", price=150.0, ts="2025-01-15T12:00:00Z"))
        ledger.record_trade(_trade(direction="SELL", price=152.0, ts="2025-01-15T13:00:00Z"))
        ledger.record_equity(_equity(ts="2025-01-15T12:30:00Z"))
        out = tmp_path / "results"
        ledger.write(out)
        assert (out / "trades.csv").exists()
        assert (out / "round_trips.csv").exists()
        assert (out / "metrics.csv").exists()
        assert (out / "equity.csv").exists()
        assert (out / "equity_daily.csv").exists()
        assert (out / "exposure.csv").exists()
        assert (out / "opportunity.csv").exists()

    def test_write_round_trips_csv_with_excursions(self, tmp_path):
        """MFE/MAE columns populated when candle_indices provided."""
        ledger = TradeLedger()
        ledger.record_trade(_trade(direction="BUY", price=150.0, ts="2025-01-15T12:00:00Z"))
        ledger.record_trade(_trade(direction="SELL", price=155.0, ts="2025-01-15T13:00:00Z"))
        candles = [
            Candle(timestamp="2025-01-15T12:00:00Z", open=150, high=158, low=149, close=152),
            Candle(timestamp="2025-01-15T13:00:00Z", open=152, high=156, low=151, close=155),
        ]
        ledger.candle_indices["USDJPY"] = build_candle_index(candles)

        path = tmp_path / "round_trips.csv"
        ledger.write_round_trips_csv(path)
        rows = _read_csv(path)
        assert len(rows) == 1
        assert rows[0]["mfe_pnl"] != ""
        assert float(rows[0]["mfe_points"]) > 0

    def test_write_round_trips_csv_without_excursions(self, tmp_path):
        """MFE/MAE columns blank when no candle_indices."""
        ledger = TradeLedger()
        ledger.record_trade(_trade(direction="BUY", price=150.0, ts="2025-01-15T12:00:00Z"))
        ledger.record_trade(_trade(direction="SELL", price=155.0, ts="2025-01-15T13:00:00Z"))
        path = tmp_path / "round_trips.csv"
        ledger.write_round_trips_csv(path)
        rows = _read_csv(path)
        assert len(rows) == 1
        # 14-column header present
        assert "mfe_points" in rows[0]
        assert "mae_pnl" in rows[0]
        # Values blank
        assert rows[0]["mfe_pnl"] == ""
        assert rows[0]["mae_pnl"] == ""

    def test_write_round_trips_csv_partial_excursions(self, tmp_path):
        """Only instruments with candle_indices get MFE/MAE data."""
        ledger = TradeLedger()
        # Instrument A: entry + exit
        ledger.record_trade(
            _trade(instrument="AAA", direction="BUY", price=100.0, ts="2025-01-15T12:00:00Z")
        )
        ledger.record_trade(
            _trade(instrument="AAA", direction="SELL", price=110.0, ts="2025-01-15T13:00:00Z")
        )
        # Instrument B: entry + exit
        ledger.record_trade(
            _trade(instrument="BBB", direction="BUY", price=200.0, ts="2025-01-15T12:00:00Z")
        )
        ledger.record_trade(
            _trade(instrument="BBB", direction="SELL", price=210.0, ts="2025-01-15T13:00:00Z")
        )
        # Only provide candle index for AAA
        candles_a = [
            Candle(timestamp="2025-01-15T12:00:00Z", open=100, high=115, low=98, close=105),
            Candle(timestamp="2025-01-15T13:00:00Z", open=105, high=112, low=104, close=110),
        ]
        ledger.candle_indices["AAA"] = build_candle_index(candles_a)

        path = tmp_path / "round_trips.csv"
        ledger.write_round_trips_csv(path)
        rows = _read_csv(path)
        assert len(rows) == 2
        aaa = next(r for r in rows if r["instrument"] == "AAA")
        bbb = next(r for r in rows if r["instrument"] == "BBB")
        assert aaa["mfe_pnl"] != ""
        assert bbb["mfe_pnl"] == ""

    def test_write_metrics_csv_basic(self, tmp_path):
        """Metrics CSV has PORTFOLIO row and per-instrument row."""
        ledger = TradeLedger()
        ledger.record_equity(_equity(ts="2025-01-15T12:00:00Z", eq=0.0))
        ledger.record_trade(_trade(direction="BUY", price=150.0, ts="2025-01-15T12:00:00Z"))
        ledger.record_trade(_trade(direction="SELL", price=160.0, ts="2025-01-15T13:00:00Z"))
        ledger.record_equity(_equity(ts="2025-01-15T13:00:00Z", eq=10.0))

        path = tmp_path / "metrics.csv"
        ledger.write_metrics_csv(path)
        rows = _read_csv(path)
        instruments = [r["instrument"] for r in rows]
        assert "PORTFOLIO" in instruments
        assert "USDJPY" in instruments
        assert len(rows) == 2

    def test_write_metrics_csv_multiple_instruments(self, tmp_path):
        """Metrics CSV has PORTFOLIO + one row per instrument."""
        ledger = TradeLedger()
        ledger.record_equity(_equity(ts="2025-01-15T12:00:00Z", eq=0.0))
        for inst in ["AAA", "BBB"]:
            ledger.record_trade(
                _trade(instrument=inst, direction="BUY", price=100.0, ts="2025-01-15T12:00:00Z")
            )
            ledger.record_trade(
                _trade(instrument=inst, direction="SELL", price=105.0, ts="2025-01-15T13:00:00Z")
            )
        ledger.record_equity(_equity(ts="2025-01-15T13:00:00Z", eq=10.0))

        path = tmp_path / "metrics.csv"
        ledger.write_metrics_csv(path)
        rows = _read_csv(path)
        assert len(rows) == 3
        instruments = {r["instrument"] for r in rows}
        assert instruments == {"PORTFOLIO", "AAA", "BBB"}

    def test_write_metrics_csv_empty_ledger(self, tmp_path):
        """Empty ledger produces metrics.csv with header and PORTFOLIO row."""
        ledger = TradeLedger()
        path = tmp_path / "metrics.csv"
        ledger.write_metrics_csv(path)
        rows = _read_csv(path)
        assert len(rows) == 1
        assert rows[0]["instrument"] == "PORTFOLIO"

    def test_write_metrics_csv_broker_mode(self, tmp_path):
        """BROKER mode subtracts initial_balance from PORTFOLIO final_equity."""
        ledger = TradeLedger(mode=RecordingMode.BROKER, out_dir=tmp_path, initial_balance=10000.0)
        # Record trades
        ledger.record_trade(
            _trade(direction="BUY", price=100.0, size=1.0, ts="2025-01-15T12:00:00Z")
        )
        ledger.record_trade(
            _trade(direction="SELL", price=110.0, size=1.0, ts="2025-01-15T13:00:00Z")
        )
        # In broker mode, record_equity is ignored, but we need equity for compute_metrics.
        # The ledger computes synthetic equity internally. For write_metrics_csv,
        # we add equity rows manually to test the subtraction logic.
        ledger.equity.append(_equity(ts="2025-01-15T12:00:00Z", eq=10000.0))
        ledger.equity.append(_equity(ts="2025-01-15T13:00:00Z", eq=10010.0))

        path = tmp_path / "metrics.csv"
        ledger.write_metrics_csv(path)
        rows = _read_csv(path)
        portfolio = next(r for r in rows if r["instrument"] == "PORTFOLIO")
        # PnL should be 10 (10010 - 10000), not 10010
        assert float(portfolio["final_equity"]) == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# TradeLedger – broker mode
# ---------------------------------------------------------------------------


class TestTradeLedgerBroker:
    def test_broker_mode_requires_out_dir(self):
        with pytest.raises(ValueError, match="out_dir required"):
            TradeLedger(mode=RecordingMode.BROKER)

    def test_broker_mode_creates_trades_csv_on_init(self, tmp_path):
        TradeLedger(mode=RecordingMode.BROKER, out_dir=tmp_path)
        assert (tmp_path / "trades.csv").exists()
        rows = _read_csv(tmp_path / "trades.csv")
        assert len(rows) == 0  # header only

    def test_broker_record_trade_appends_to_csv(self, tmp_path):
        ledger = TradeLedger(mode=RecordingMode.BROKER, out_dir=tmp_path, initial_balance=10000.0)
        ledger.record_trade(_trade(ts="2025-01-15T12:00:00Z"))
        rows = _read_csv(tmp_path / "trades.csv")
        assert len(rows) == 1

    def test_broker_record_equity_ignored(self, tmp_path):
        ledger = TradeLedger(mode=RecordingMode.BROKER, out_dir=tmp_path)
        ledger.record_equity(_equity())
        assert len(ledger.equity) == 0

    def test_broker_synthetic_equity_pnl(self, tmp_path):
        ledger = TradeLedger(mode=RecordingMode.BROKER, out_dir=tmp_path, initial_balance=10000.0)
        # Open long
        ledger.record_trade(
            _trade(direction="BUY", price=150.0, size=1.0, ts="2025-01-15T12:00:00Z")
        )
        assert ledger._current_balance == 10000.0  # no P&L yet

        # Close long at profit
        ledger.record_trade(
            _trade(direction="SELL", price=155.0, size=1.0, ts="2025-01-15T13:00:00Z")
        )
        assert ledger._current_balance == pytest.approx(10005.0)

    def test_broker_short_trade_pnl(self, tmp_path):
        ledger = TradeLedger(mode=RecordingMode.BROKER, out_dir=tmp_path, initial_balance=10000.0)
        # Open short
        ledger.record_trade(
            _trade(direction="SELL", price=150.0, size=1.0, ts="2025-01-15T12:00:00Z")
        )
        # Close short at profit
        ledger.record_trade(
            _trade(direction="BUY", price=145.0, size=1.0, ts="2025-01-15T13:00:00Z")
        )
        assert ledger._current_balance == pytest.approx(10005.0)

    def test_broker_adds_to_position(self, tmp_path):
        ledger = TradeLedger(mode=RecordingMode.BROKER, out_dir=tmp_path, initial_balance=10000.0)
        ledger.record_trade(
            _trade(direction="BUY", price=100.0, size=1.0, ts="2025-01-15T12:00:00Z")
        )
        ledger.record_trade(
            _trade(direction="BUY", price=110.0, size=1.0, ts="2025-01-15T12:01:00Z")
        )
        pos = ledger._open_positions["USDJPY"]
        assert pos["size"] == pytest.approx(2.0)
        assert pos["price"] == pytest.approx(105.0)  # average price

    def test_broker_partial_close(self, tmp_path):
        ledger = TradeLedger(mode=RecordingMode.BROKER, out_dir=tmp_path, initial_balance=10000.0)
        ledger.record_trade(
            _trade(direction="BUY", price=100.0, size=2.0, ts="2025-01-15T12:00:00Z")
        )
        # Partial close
        ledger.record_trade(
            _trade(direction="SELL", price=110.0, size=1.0, ts="2025-01-15T13:00:00Z")
        )
        # P&L: (110 - 100) * 1 = 10
        assert ledger._current_balance == pytest.approx(10010.0)
        # 1.0 remaining
        assert ledger._open_positions["USDJPY"]["size"] == pytest.approx(1.0)

    def test_broker_daily_equity_csv(self, tmp_path):
        ledger = TradeLedger(mode=RecordingMode.BROKER, out_dir=tmp_path, initial_balance=10000.0)
        # Trade on day 1
        ledger.record_trade(_trade(direction="BUY", price=100.0, ts="2025-01-15T12:00:00Z"))
        # Trade on day 2 (triggers daily equity write)
        ledger.record_trade(_trade(direction="SELL", price=110.0, ts="2025-01-16T12:00:00Z"))
        assert (tmp_path / "equity_daily.csv").exists()

    def test_broker_write_is_noop(self, tmp_path):
        """In broker mode, write() should not raise."""
        ledger = TradeLedger(mode=RecordingMode.BROKER, out_dir=tmp_path)
        out = tmp_path / "results"
        ledger.write(out)
