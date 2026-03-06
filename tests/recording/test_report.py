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
