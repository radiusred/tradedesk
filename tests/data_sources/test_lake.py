"""Tests for the macro Parquet lake (materialize + load access pattern)."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from tradedesk.data_sources import ecb, fred, lake
from tradedesk.data_sources.cot import CFTC_CONTRACTS

# ---------------------------------------------------------------------------
# CFTC TFF fixture (includes the Asset-Manager / Leveraged-Funds buckets)
# ---------------------------------------------------------------------------

_TFF_HEADER = [
    "Market_and_Exchange_Names",
    "As_of_Date_In_Form_YYMMDD",
    "Report_Date_as_YYYY-MM-DD",
    "CFTC_Contract_Market_Code",
    "CFTC_Market_Code",
    "CFTC_Region_Code",
    "CFTC_Commodity_Code",
    "Open_Interest_All",
    "Dealer_Positions_Long_All",
    "Dealer_Positions_Short_All",
    "Asset_Mgr_Positions_Long_All",
    "Asset_Mgr_Positions_Short_All",
    "Lev_Money_Positions_Long_All",
    "Lev_Money_Positions_Short_All",
]


def _tff_zip(path: Path, rows: list[list[str]]) -> None:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_TFF_HEADER)
    for r in rows:
        w.writerow(r + [""] * (len(_TFF_HEADER) - len(r)))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("f_year.txt", buf.getvalue())


def _eurusd_row(report_date: str, oi, dl, ds, al, as_, ll, ls) -> list[str]:
    yymmdd = report_date.replace("-", "")[2:]
    return [
        "EURO FX - CHICAGO MERCANTILE EXCHANGE",
        yymmdd,
        report_date,
        "099741",
        "TFF",
        "00",
        "001",
        str(oi),
        str(dl),
        str(ds),
        str(al),
        str(as_),
        str(ll),
        str(ls),
    ]


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_macro_path_layout(tmp_path: Path):
    p = lake.macro_path("FRED", "DGS10", lake=tmp_path)
    assert p == tmp_path / "macro" / "fred" / "DGS10.parquet"
    assert lake.macro_dir(lake.MacroSource.CFTC, lake=tmp_path) == tmp_path / "macro" / "cftc"


def test_default_lake_env_override(monkeypatch):
    monkeypatch.setenv("TRADEDESK_MARKETDATA", "/data/md")
    assert lake.default_lake() == Path("/data/md")
    monkeypatch.delenv("TRADEDESK_MARKETDATA")
    assert lake.default_lake() == Path("/paperclip/tradedesk/marketdata")


# ---------------------------------------------------------------------------
# Write / load round-trip
# ---------------------------------------------------------------------------


def test_value_series_roundtrip(tmp_path: Path):
    df = pd.DataFrame(
        {"value": [4.39, 4.41]},
        index=pd.DatetimeIndex(
            [pd.Timestamp("2026-05-01"), pd.Timestamp("2026-05-03")], name="date"
        ),
    )
    lake._write_parquet(df, lake.macro_path("FRED", "DGS10", lake=tmp_path))
    loaded = lake.load_macro_series("FRED", "DGS10", lake=tmp_path)
    assert loaded.equals(df)


def test_load_missing_series_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        lake.load_macro_series("FRED", "NOPE", lake=tmp_path)


def test_load_macro_frame_joins_on_date(tmp_path: Path):
    a = pd.DataFrame(
        {"value": [1.0, 2.0]},
        index=pd.DatetimeIndex(["2026-01-01", "2026-01-02"], name="date"),
    )
    b = pd.DataFrame(
        {"value": [9.0]}, index=pd.DatetimeIndex(["2026-01-02"], name="date")
    )
    lake._write_parquet(a, lake.macro_path("FRED", "DGS2", lake=tmp_path))
    lake._write_parquet(b, lake.macro_path("FRED", "VIXCLS", lake=tmp_path))
    wide = lake.load_macro_frame("FRED", ["DGS2", "VIXCLS"], lake=tmp_path)
    assert list(wide.columns) == ["DGS2", "VIXCLS"]
    assert wide.loc["2026-01-01", "DGS2"] == 1.0
    assert pd.isna(wide.loc["2026-01-01", "VIXCLS"])
    assert wide.loc["2026-01-02", "VIXCLS"] == 9.0


def test_available_macro_series(tmp_path: Path):
    lake._write_parquet(
        pd.DataFrame({"value": [1.0]}, index=pd.DatetimeIndex(["2026-01-01"], name="date")),
        lake.macro_path("ECB", "EUR_ESTR", lake=tmp_path),
    )
    avail = lake.available_macro_series(lake=tmp_path)
    assert avail["ecb"] == ["EUR_ESTR"]
    assert avail["fred"] == []


# ---------------------------------------------------------------------------
# CFTC frame from real zip parsing + materialize
# ---------------------------------------------------------------------------


def test_cot_history_frame_exposes_all_buckets(tmp_path: Path):
    (tmp_path / "cftc").mkdir(parents=True)
    _tff_zip(
        tmp_path / "cftc" / "fut_fin_txt_2024.zip",
        rows=[
            _eurusd_row("2024-06-04", 700_000, 100, 200, 300, 50, 400, 250),
            _eurusd_row("2024-06-11", 710_000, 120, 180, 320, 60, 410, 240),
        ],
    )
    df = lake.cot_history_frame(
        CFTC_CONTRACTS["EURUSD"],
        date_from=date(2024, 1, 1),
        date_to=date(2024, 12, 31),
        cache_dir=tmp_path,
        force_refresh_current_year=False,
    )
    assert df.index.name == "date"
    assert len(df) == 2
    first = df.iloc[0]
    assert first["dealer_net"] == 100 - 200
    assert first["asset_mgr_net"] == 300 - 50
    assert first["leveraged_net"] == 400 - 250
    # Dealer bucket mirrored into commercial_* for back-compat.
    assert first["commercial_net"] == 100 - 200
    assert first["release_date"] == pd.Timestamp("2024-06-07")  # Tuesday + 3
    assert df["open_interest"].dtype == "int64"


def test_materialize_cftc_writes_parquet(tmp_path: Path):
    (tmp_path / "cftc").mkdir(parents=True)
    _tff_zip(
        tmp_path / "cftc" / "fut_fin_txt_2024.zip",
        rows=[_eurusd_row("2024-06-04", 700_000, 100, 200, 300, 50, 400, 250)],
    )
    written = lake.materialize_cftc(
        contracts={"EURUSD": CFTC_CONTRACTS["EURUSD"]},
        date_from=date(2024, 1, 1),
        date_to=date(2024, 12, 31),
        lake=tmp_path,
    )
    assert "EURUSD" in written
    loaded = lake.load_macro_series("CFTC", "EURUSD", lake=tmp_path)
    assert loaded.iloc[0]["asset_mgr_long"] == 300


# ---------------------------------------------------------------------------
# Materialize FRED/ECB with mocked fetchers (failures are non-fatal)
# ---------------------------------------------------------------------------


def test_materialize_fred_mocked(monkeypatch, tmp_path: Path):
    def fake_fetch(series_id, **kwargs):
        return pd.DataFrame(
            {"value": [1.0]},
            index=pd.DatetimeIndex(["2026-01-01"], name="date"),
        )

    monkeypatch.setattr(fred, "fetch_fred_series", fake_fetch)
    written = lake.materialize_fred(series={"DGS10": "x"}, lake=tmp_path)
    assert set(written) == {"DGS10"}
    assert lake.load_macro_series("FRED", "DGS10", lake=tmp_path)["value"].iloc[0] == 1.0


def test_materialize_skips_failed_series(monkeypatch, tmp_path: Path):
    import urllib.error

    def boom(series_id, **kwargs):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(fred, "fetch_fred_series", boom)
    written = lake.materialize_fred(series={"DGS10": "x"}, lake=tmp_path)
    assert written == {}


def test_materialize_ecb_mocked(monkeypatch, tmp_path: Path):
    def fake_fetch(series, **kwargs):
        return pd.DataFrame(
            {"value": [2.6]},
            index=pd.DatetimeIndex(["2026-05-04"], name="date"),
        )

    monkeypatch.setattr(ecb, "fetch_ecb_series", fake_fetch)
    written = lake.materialize_ecb(
        series={"EUR_YLD_2Y": ecb.DEFAULT_ECB_SERIES["EUR_YLD_2Y"]}, lake=tmp_path
    )
    assert set(written) == {"EUR_YLD_2Y"}


# ---------------------------------------------------------------------------
# FRED incremental / upsert (RAD-3791)
# ---------------------------------------------------------------------------


def _value_frame(rows: dict[str, float]) -> pd.DataFrame:
    return pd.DataFrame(
        {"value": list(rows.values())},
        index=pd.DatetimeIndex(list(rows.keys()), name="date"),
    )


def _backdate(path: Path, days: int = 2) -> None:
    """Age a file's mtime so the same-day FRED skip guard does not trip."""
    import os

    past = (pd.Timestamp.now() - pd.Timedelta(days=days)).timestamp()
    os.utime(path, (past, past))


def test_merge_fred_appends_only_new_rows():
    existing = _value_frame({"2026-01-03": 1.0, "2026-01-04": 2.0})
    new = _value_frame({"2026-01-04": 9.9, "2026-01-05": 3.0, "2026-01-06": 4.0})
    merged, added = lake._merge_fred(existing, new)
    assert added == 2
    assert list(merged.index) == [
        pd.Timestamp(d) for d in ("2026-01-03", "2026-01-04", "2026-01-05", "2026-01-06")
    ]
    # Existing 2026-01-04 value is preserved, not overwritten by the new 9.9.
    assert merged.loc["2026-01-04", "value"] == 2.0


def test_merge_fred_first_run_uses_all_new_rows():
    new = _value_frame({"2026-01-05": 3.0, "2026-01-04": 2.0})
    merged, added = lake._merge_fred(None, new)
    assert added == 2
    assert list(merged.index) == [pd.Timestamp("2026-01-04"), pd.Timestamp("2026-01-05")]


def test_merge_fred_no_new_rows_is_idempotent():
    existing = _value_frame({"2026-01-04": 2.0, "2026-01-05": 3.0})
    new = _value_frame({"2026-01-05": 3.0})
    merged, added = lake._merge_fred(existing, new)
    assert added == 0
    assert merged is existing


def _recording_fetch(returns):
    """Build a fake fetch_fred_series that records each call's date_from."""
    calls: list[date | None] = []

    def fake(series_id, *, date_from=None, cache_dir=None, force=False):
        calls.append(date_from)
        result = returns[len(calls) - 1] if isinstance(returns, list) else returns
        if isinstance(result, Exception):
            raise result
        return result

    return fake, calls


def test_materialize_fred_incremental_fetches_after_max_date(monkeypatch, tmp_path: Path):
    path = lake.macro_path("FRED", "DGS10", lake=tmp_path)
    lake._write_parquet(_value_frame({"2026-01-04": 2.0, "2026-01-05": 3.0}), path)
    _backdate(path)  # simulate a parquet from a previous day
    fake, calls = _recording_fetch(_value_frame({"2026-01-06": 4.0, "2026-01-07": 5.0}))
    monkeypatch.setattr(fred, "fetch_fred_series", fake)

    written = lake.materialize_fred(series={"DGS10": "x"}, lake=tmp_path)

    assert set(written) == {"DGS10"}
    assert calls == [date(2026, 1, 6)]  # max(existing) + 1 day
    loaded = lake.load_macro_series("FRED", "DGS10", lake=tmp_path)
    assert list(loaded["value"]) == [2.0, 3.0, 4.0, 5.0]


def test_materialize_fred_first_run_uses_trailing_year(monkeypatch, tmp_path: Path):
    fake, calls = _recording_fetch(_value_frame({"2026-01-06": 4.0}))
    monkeypatch.setattr(fred, "fetch_fred_series", fake)

    written = lake.materialize_fred(series={"DGS10": "x"}, lake=tmp_path)

    assert set(written) == {"DGS10"}
    assert calls == [date.today() - timedelta(days=lake.FRED_FIRST_RUN_LOOKBACK_DAYS)]


def test_materialize_fred_first_run_falls_back_to_90d_on_timeout(monkeypatch, tmp_path: Path):
    fake, calls = _recording_fetch(
        [TimeoutError("body never arrived"), _value_frame({"2026-01-06": 4.0})]
    )
    monkeypatch.setattr(fred, "fetch_fred_series", fake)

    written = lake.materialize_fred(series={"DGS10": "x"}, lake=tmp_path)

    assert set(written) == {"DGS10"}
    assert calls == [
        date.today() - timedelta(days=lake.FRED_FIRST_RUN_LOOKBACK_DAYS),
        date.today() - timedelta(days=lake.FRED_FIRST_RUN_FALLBACK_DAYS),
    ]


def test_materialize_fred_rerun_is_idempotent_no_download(monkeypatch, tmp_path: Path):
    # Parquet already current through today -> no fetch should happen.
    lake._write_parquet(
        _value_frame({date.today().isoformat(): 7.0}),
        lake.macro_path("FRED", "DGS10", lake=tmp_path),
    )
    fake, calls = _recording_fetch(_value_frame({}))
    monkeypatch.setattr(fred, "fetch_fred_series", fake)

    written = lake.materialize_fred(series={"DGS10": "x"}, lake=tmp_path)

    assert set(written) == {"DGS10"}  # still reported as materialized
    assert calls == []  # nothing downloaded
    loaded = lake.load_macro_series("FRED", "DGS10", lake=tmp_path)
    assert list(loaded["value"]) == [7.0]


def test_materialize_fred_skips_when_parquet_written_today(monkeypatch, tmp_path: Path):
    # An old-dated parquet, but written today -> same-day skip, no network.
    lake._write_parquet(
        _value_frame({"2025-01-02": 1.0}),
        lake.macro_path("FRED", "DGS10", lake=tmp_path),
    )
    fake, calls = _recording_fetch(_value_frame({"2026-06-01": 9.0}))
    monkeypatch.setattr(fred, "fetch_fred_series", fake)

    written = lake.materialize_fred(series={"DGS10": "x"}, lake=tmp_path)

    assert set(written) == {"DGS10"}
    assert calls == []  # fetched-today guard kept it offline
    loaded = lake.load_macro_series("FRED", "DGS10", lake=tmp_path)
    assert list(loaded["value"]) == [1.0]  # untouched


def test_materialize_fred_explicit_from_overrides_incremental(monkeypatch, tmp_path: Path):
    path = lake.macro_path("FRED", "DGS10", lake=tmp_path)
    lake._write_parquet(_value_frame({"2026-01-04": 2.0}), path)
    # Note: NOT backdated — an explicit --from must fetch even on a same-day re-run.
    fake, calls = _recording_fetch(_value_frame({"2020-01-02": 1.0, "2026-01-05": 3.0}))
    monkeypatch.setattr(fred, "fetch_fred_series", fake)

    written = lake.materialize_fred(
        series={"DGS10": "x"}, date_from=date(2020, 1, 1), lake=tmp_path
    )

    assert set(written) == {"DGS10"}
    assert calls == [date(2020, 1, 1)]  # explicit override wins over incremental
    loaded = lake.load_macro_series("FRED", "DGS10", lake=tmp_path)
    # Older override rows merge in alongside the preserved existing row.
    assert list(loaded["value"]) == [1.0, 2.0, 3.0]


def test_materialize_fred_keeps_prior_data_when_delta_fetch_fails(monkeypatch, tmp_path: Path):
    import urllib.error

    path = lake.macro_path("FRED", "DGS10", lake=tmp_path)
    lake._write_parquet(_value_frame({"2026-01-04": 2.0}), path)
    _backdate(path)  # force the delta fetch (which then fails)

    def boom(series_id, **kwargs):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(fred, "fetch_fred_series", boom)
    written = lake.materialize_fred(series={"DGS10": "x"}, lake=tmp_path)

    assert set(written) == {"DGS10"}  # prior parquet still counts as materialized
    loaded = lake.load_macro_series("FRED", "DGS10", lake=tmp_path)
    assert list(loaded["value"]) == [2.0]


def test_materialize_fred_timeout_skips_one_series(monkeypatch, tmp_path: Path):
    """TimeoutError on one FRED series is non-fatal; the other series still materializes."""

    def fetch(series_id, **kwargs):
        if series_id == "DGS10":
            raise TimeoutError("read timed out")
        return pd.DataFrame(
            {"value": [4.41]},
            index=pd.DatetimeIndex(["2026-05-03"], name="date"),
        )

    monkeypatch.setattr(fred, "fetch_fred_series", fetch)
    written = lake.materialize_fred(series={"DGS10": "x", "DGS2": "x"}, lake=tmp_path)
    assert "DGS10" not in written
    assert "DGS2" in written


def test_materialize_all_survives_dead_source(monkeypatch, tmp_path: Path):
    """When all FRED series raise TimeoutError, ECB results still appear in materialize_all."""

    def fred_timeout(series_id, **kwargs):
        raise TimeoutError("read timed out")

    def ecb_ok(series, **kwargs):
        return pd.DataFrame(
            {"value": [2.6]},
            index=pd.DatetimeIndex(["2026-05-04"], name="date"),
        )

    monkeypatch.setattr(fred, "fetch_fred_series", fred_timeout)
    monkeypatch.setattr(ecb, "fetch_ecb_series", ecb_ok)
    monkeypatch.setattr(lake, "materialize_cftc", lambda **kw: {})
    result = lake.materialize_all(lake=tmp_path)
    assert result[lake.MacroSource.FRED.value] == {}
    assert result[lake.MacroSource.ECB.value] != {}
