"""Tests for the FRED data source."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from tradedesk.data_sources import fred

_SAMPLE = """observation_date,DGS10
2026-05-01,4.39
2026-05-02,.
2026-05-03,4.41
2026-05-04,
"""


def test_parse_fred_csv_drops_missing_and_sorts():
    df = fred.parse_fred_csv(_SAMPLE)
    # The "." and blank rows are dropped, leaving two observations.
    assert list(df.columns) == ["value"]
    assert df.index.name == "date"
    assert len(df) == 2
    assert df.index.tolist() == [pd.Timestamp("2026-05-01"), pd.Timestamp("2026-05-03")]
    assert df["value"].tolist() == [4.39, 4.41]
    assert df["value"].dtype == "float64"


def test_parse_fred_csv_empty():
    df = fred.parse_fred_csv("observation_date,VIXCLS\n")
    assert df.empty
    assert list(df.columns) == ["value"]


def test_fred_csv_url_includes_id_and_start():
    url = fred._fred_csv_url("DGS2", date(2018, 1, 1))
    assert url.startswith(fred.FRED_BASE_URL)
    assert "id=DGS2" in url
    assert "cosd=2018-01-01" in url


def test_fred_csv_url_without_start_has_no_cosd():
    url = fred._fred_csv_url("VIXCLS")
    assert "id=VIXCLS" in url
    assert "cosd=" not in url


def test_default_series_cover_required_fields():
    # Task requires effective fed funds, 2y/10y treasuries and VIX.
    for required in ("DFF", "DGS2", "DGS10", "VIXCLS"):
        assert required in fred.DEFAULT_FRED_SERIES


def test_fetch_caches_and_reuses(monkeypatch, tmp_path: Path):
    calls: list[str] = []

    def fake_get_text(url: str, *, timeout: float = 60.0) -> str:
        calls.append(url)
        return _SAMPLE

    monkeypatch.setattr(fred, "get_text", fake_get_text)

    df1 = fred.fetch_fred_series("DGS10", cache_dir=tmp_path)
    assert (tmp_path / "fred" / "DGS10.csv").exists()
    assert len(df1) == 2
    # Second call should hit the cache, not the network.
    df2 = fred.fetch_fred_series("DGS10", cache_dir=tmp_path)
    assert len(calls) == 1
    assert df1.equals(df2)
    # force=True re-downloads.
    fred.fetch_fred_series("DGS10", cache_dir=tmp_path, force=True)
    assert len(calls) == 2
