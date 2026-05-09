"""CSV reading helpers for report inputs."""

import csv
from pathlib import Path


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        return list(reader)


def _ensure_instrument_field(rows: list[dict[str, str]]) -> None:
    """Ensure each row has an 'instrument' key (backcompat with old CSVs using 'epic')."""
    for r in rows:
        if "epic" in r and "instrument" not in r:
            r["instrument"] = r["epic"]
