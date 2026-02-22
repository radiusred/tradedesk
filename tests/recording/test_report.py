import csv
from pathlib import Path

from tradedesk.recording import report


def _write_csv(path: Path, headers: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


def test_prepare_monthly_data_values_are_iterable():
    rt_rows = [
        {
            "instrument": "com.example.inst1",
            "exit_ts": "2026-01-15T12:00:00Z",
            "pnl": "10.50",
            "entry_ts": "2026-01-15T11:00:00Z",
            "hold_minutes": "60",
            "direction": "long",
        },
        {
            "instrument": "com.example.inst1",
            "exit_ts": "2026-01-20T12:00:00Z",
            "pnl": "-5.25",
            "entry_ts": "2026-01-20T11:00:00Z",
            "hold_minutes": "60",
            "direction": "short",
        },
    ]

    inst_data = {"com.example.inst1": rt_rows}
    monthly = report._prepare_monthly_data(rt_rows, inst_data)

    assert hasattr(monthly, "rows")
    assert isinstance(monthly.rows, list)
    assert len(monthly.rows) >= 1

    row = monthly.rows[0]
    assert hasattr(row, "values")
    assert isinstance(row.values, list)
    assert all(isinstance(v, str) for v in row.values)

    assert hasattr(monthly, "totals")
    assert isinstance(monthly.totals.values, list)


def test_generate_analysis_report_creates_file(tmp_path: Path):
    out = tmp_path
    metrics_path = out / "metrics.csv"
    round_trips_path = out / "round_trips.csv"

    metrics_headers = [
        "instrument",
        "final_equity",
        "max_dd",
        "win_rate",
        "profit_factor",
        "expectancy",
        "round_trips",
        "fills",
        "avg_hold_min",
    ]

    metrics_rows = [
        {
            "instrument": "PORTFOLIO",
            "final_equity": "1500",
            "max_dd": "-50",
            "win_rate": "0.6",
            "profit_factor": "1.5",
            "expectancy": "0.5",
            "round_trips": "2",
            "fills": "10",
            "avg_hold_min": "30",
        },
        {
            "instrument": "com.example.inst1",
            "final_equity": "500",
            "max_dd": "-20",
            "win_rate": "0.55",
            "profit_factor": "1.2",
            "expectancy": "0.3",
            "round_trips": "2",
            "fills": "5",
            "avg_hold_min": "45",
        },
    ]

    _write_csv(metrics_path, metrics_headers, metrics_rows)

    rt_headers = [
        "instrument",
        "exit_ts",
        "pnl",
        "entry_ts",
        "hold_minutes",
        "direction",
        "mfe_pnl",
        "mae_pnl",
        "exit_reason",
    ]

    rt_rows = [
        {
            "instrument": "com.example.inst1",
            "exit_ts": "2026-01-15T12:00:00Z",
            "pnl": "10.5",
            "entry_ts": "2026-01-15T11:00:00Z",
            "hold_minutes": "60",
            "direction": "long",
            "mfe_pnl": "15",
            "mae_pnl": "-2",
            "exit_reason": "",
        },
        {
            "instrument": "com.example.inst1",
            "exit_ts": "2026-01-20T12:00:00Z",
            "pnl": "-5.25",
            "entry_ts": "2026-01-20T11:00:00Z",
            "hold_minutes": "60",
            "direction": "short",
            "mfe_pnl": "8",
            "mae_pnl": "-3",
            "exit_reason": "market_order",
        },
    ]

    _write_csv(round_trips_path, rt_headers, rt_rows)

    # Should not raise
    report.generate_analysis_report(out)

    report_file = out / "analysis.md"
    assert report_file.exists()
    content = report_file.read_text()
    # basic sanity checks
    assert "Monthly" in content or "Months" in content or "2026-01" in content
