"""Tests for the CFTC COT data source."""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

from tradedesk.data_sources.cot import (
    CFTC_CONTRACTS,
    CFTCReport,
    cot_release_date,
    iter_cot_rows,
    load_contract_history,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_DISAGG_HEADER = [
    "Market_and_Exchange_Names",
    "As_of_Date_In_Form_YYMMDD",
    "Report_Date_as_YYYY-MM-DD",
    "CFTC_Contract_Market_Code",
    "CFTC_Market_Code",
    "CFTC_Region_Code",
    "CFTC_Commodity_Code",
    "Open_Interest_All",
    "Prod_Merc_Positions_Long_All",
    "Prod_Merc_Positions_Short_All",
]

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
]


def _write_zip(
    path: Path,
    *,
    header: list[str],
    rows: list[list[str]],
    inner_name: str = "f_year.txt",
) -> None:
    """Write a CFTC-format zip with one CSV file inside."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        # Pad to header width with empty strings.
        padded = row + [""] * (len(header) - len(row))
        writer.writerow(padded)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(inner_name, buf.getvalue())


def _disagg_row(report_date: str, code: str, oi: int, long_v: int, short_v: int) -> list[str]:
    yymmdd = report_date.replace("-", "")[2:]
    return [
        f"{code} - TEST EXCHANGE",
        yymmdd,
        report_date,
        code,
        "TST",
        "00",
        "001",
        str(oi),
        str(long_v),
        str(short_v),
    ]


def _tff_row(report_date: str, code: str, oi: int, long_v: int, short_v: int) -> list[str]:
    yymmdd = report_date.replace("-", "")[2:]
    return [
        f"{code} - TEST FINANCIAL EXCHANGE",
        yymmdd,
        report_date,
        code,
        "TFF",
        "00",
        "001",
        str(oi),
        str(long_v),
        str(short_v),
    ]


# ---------------------------------------------------------------------------
# Release-date offset: critical no-look-ahead test (RAD-1078 acceptance)
# ---------------------------------------------------------------------------


def test_cot_release_is_friday_three_days_after_tuesday():
    # 2026-01-06 is a Tuesday; the report should release Friday 2026-01-09.
    tuesday = date(2026, 1, 6)
    assert tuesday.weekday() == 1  # 0=Mon, 1=Tue
    release = cot_release_date(tuesday)
    assert release == date(2026, 1, 9)
    assert release.weekday() == 4  # 0=Mon, 4=Fri


@pytest.mark.parametrize(
    "tuesday_iso,friday_iso",
    [
        # Standard publication dates across multiple weeks
        ("2024-12-31", "2025-01-03"),
        ("2025-01-07", "2025-01-10"),
        ("2025-12-30", "2026-01-02"),
        ("2026-04-28", "2026-05-01"),
    ],
)
def test_cot_release_offset_handles_year_boundary(tuesday_iso: str, friday_iso: str):
    """Release is always Tuesday + 3 days, even across year boundaries."""
    rdate = date.fromisoformat(tuesday_iso)
    assert cot_release_date(rdate) == date.fromisoformat(friday_iso)


def test_cot_release_offset_drives_strategy_entry_with_no_look_ahead():
    """A strategy that enters after release must wait at least until next Monday.

    This is the core no-look-ahead invariant for the COT strategy: the
    earliest legal entry is the first trading day strictly after the
    Friday release, which for a Tuesday-Friday publication cycle is the
    following Monday.
    """
    tuesday = date(2026, 1, 6)
    friday_release = cot_release_date(tuesday)
    next_monday = friday_release + (
        # Move to following Monday: Friday=4 → +3, Saturday=+2, Sunday=+1.
        # Here friday is 4 so +3 lands on Monday=0 of next week.
        __import__("datetime").timedelta(days=3)
    )
    assert next_monday.weekday() == 0  # Monday
    assert (next_monday - tuesday).days == 6


# ---------------------------------------------------------------------------
# Parsing & filtering
# ---------------------------------------------------------------------------


def test_iter_cot_rows_filters_by_contract_and_date(tmp_path: Path):
    """Disaggregated parsing keeps only the requested codes and date range."""
    cache = tmp_path
    archive = cache / "cftc"
    archive.mkdir(parents=True)
    # Use the consolidated 2006-2016 archive filename so the loader picks
    # this up via the historic-archive code path.
    zp = archive / "fut_disagg_txt_hist_2006_2016.zip"
    _write_zip(
        zp,
        header=_DISAGG_HEADER,
        rows=[
            _disagg_row("2010-01-05", "088691", 100_000, 50_000, 30_000),  # GOLD
            _disagg_row("2010-01-05", "067651", 200_000, 80_000, 90_000),  # WTI
            _disagg_row("2010-01-12", "088691", 110_000, 55_000, 25_000),  # GOLD
            # GOLD (out of date range)
            _disagg_row("2017-01-03", "088691", 120_000, 60_000, 20_000),
        ],
    )

    rows = list(
        iter_cot_rows(
            cache,
            CFTCReport.DISAGGREGATED,
            date_from=date(2010, 1, 1),
            date_to=date(2010, 12, 31),
            contract_codes={"088691"},
        )
    )

    assert len(rows) == 2, [r.market_name for r in rows]
    assert all(r.cftc_code == "088691" for r in rows)
    # commercial_net = long − short
    assert rows[0].commercial_net == 50_000 - 30_000
    assert rows[1].commercial_net == 55_000 - 25_000
    # Release date is the Friday after the as-of Tuesday.
    assert rows[0].release_date_friday == date(2010, 1, 8)
    assert rows[0].report is CFTCReport.DISAGGREGATED


def test_iter_cot_rows_uses_dealer_bucket_for_tff(tmp_path: Path):
    """TFF rows expose Dealer positions in the commercial_* fields."""
    cache = tmp_path
    archive = cache / "cftc"
    archive.mkdir(parents=True)
    zp = archive / "fut_fin_txt_2024.zip"
    _write_zip(
        zp,
        header=_TFF_HEADER,
        rows=[
            _tff_row("2024-06-04", "13874A", 2_500_000, 800_000, 950_000),  # SP500
            _tff_row("2024-06-11", "13874A", 2_550_000, 850_000, 900_000),  # SP500
            _tff_row("2024-06-04", "043602", 4_500_000, 1_500_000, 1_400_000),  # 10Y
        ],
    )

    rows = list(
        iter_cot_rows(
            cache,
            CFTCReport.TFF,
            date_from=date(2024, 1, 1),
            date_to=date(2024, 12, 31),
            contract_codes={"13874A"},
        )
    )

    assert len(rows) == 2
    assert all(r.cftc_code == "13874A" for r in rows)
    assert all(r.report is CFTCReport.TFF for r in rows)
    assert rows[0].commercial_long == 800_000
    assert rows[0].commercial_short == 950_000
    assert rows[0].commercial_net == 800_000 - 950_000  # net short


def test_load_contract_history_sorts_by_report_date(tmp_path: Path):
    cache = tmp_path
    archive = cache / "cftc"
    archive.mkdir(parents=True)
    zp = archive / "fut_disagg_txt_hist_2006_2016.zip"
    _write_zip(
        zp,
        header=_DISAGG_HEADER,
        rows=[
            # Out of order on purpose
            _disagg_row("2010-01-19", "088691", 100, 50, 30),
            _disagg_row("2010-01-05", "088691", 100, 50, 30),
            _disagg_row("2010-01-12", "088691", 100, 50, 30),
        ],
    )
    contract = CFTC_CONTRACTS["GOLD"]
    rows = load_contract_history(
        cache,
        contract,
        date_from=date(2010, 1, 1),
        date_to=date(2010, 12, 31),
    )
    assert [r.report_date_tuesday for r in rows] == [
        date(2010, 1, 5),
        date(2010, 1, 12),
        date(2010, 1, 19),
    ]


def test_iter_cot_rows_handles_blank_and_dot_integer_fields(tmp_path: Path):
    """CFTC suppresses some cells with '.' or blanks; we treat these as 0."""
    cache = tmp_path
    archive = cache / "cftc"
    archive.mkdir(parents=True)
    zp = archive / "fut_disagg_txt_2024.zip"
    _write_zip(
        zp,
        header=_DISAGG_HEADER,
        rows=[
            ["GOLD - TEST", "240603", "2024-06-04", "088691", "TST", "00", "001", "100", ".", ""],
        ],
    )
    rows = list(
        iter_cot_rows(
            cache,
            CFTCReport.DISAGGREGATED,
            date_from=date(2024, 1, 1),
            date_to=date(2024, 12, 31),
            contract_codes={"088691"},
        )
    )
    assert len(rows) == 1
    assert rows[0].commercial_long == 0
    assert rows[0].commercial_short == 0
    assert rows[0].commercial_net == 0


# ---------------------------------------------------------------------------
# RAD-2987 — CME FX contracts + TFF Asset-Manager / Leveraged-Funds buckets
# ---------------------------------------------------------------------------


_TFF_FULL_HEADER = [
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


def test_fx_currency_contracts_registered():
    """EUR/JPY/GBP CME currency futures are in the TFF report."""
    for label in ("EURUSD", "JPYUSD", "GBPUSD"):
        assert label in CFTC_CONTRACTS
        assert CFTC_CONTRACTS[label].report is CFTCReport.TFF
    assert CFTC_CONTRACTS["EURUSD"].code == "099741"
    assert CFTC_CONTRACTS["JPYUSD"].code == "097741"
    assert CFTC_CONTRACTS["GBPUSD"].code == "096742"


def test_tff_exposes_asset_manager_and_leveraged_buckets(tmp_path: Path):
    archive = tmp_path / "cftc"
    archive.mkdir(parents=True)
    zp = archive / "fut_fin_txt_2024.zip"
    _write_zip(
        zp,
        header=_TFF_FULL_HEADER,
        rows=[
            [
                "EURO FX - CHICAGO MERCANTILE EXCHANGE",
                "240604",
                "2024-06-04",
                "099741",
                "TFF",
                "00",
                "001",
                "700000",  # OI
                "100",  # dealer long
                "200",  # dealer short
                "300",  # asset-mgr long
                "50",  # asset-mgr short
                "400",  # leveraged long
                "250",  # leveraged short
            ],
        ],
    )
    rows = list(
        iter_cot_rows(
            tmp_path,
            CFTCReport.TFF,
            date_from=date(2024, 1, 1),
            date_to=date(2024, 12, 31),
            contract_codes={"099741"},
        )
    )
    assert len(rows) == 1
    r = rows[0]
    assert (r.dealer_long, r.dealer_short, r.dealer_net) == (100, 200, -100)
    assert (r.asset_mgr_long, r.asset_mgr_short, r.asset_mgr_net) == (300, 50, 250)
    assert (r.leveraged_long, r.leveraged_short, r.leveraged_net) == (400, 250, 150)
    # Dealer bucket mirrored into commercial_* (back-compat).
    assert r.commercial_net == -100


def test_tff_historic_archive_us_datetime_dates(tmp_path: Path):
    """The 2006-2016 TFF archive stores dates as US ``M/D/YYYY HH:MM:SS AM``.

    Regression for the silent drop of all pre-2017 TFF history.
    """
    archive = tmp_path / "cftc"
    archive.mkdir(parents=True)
    # Consolidated historic TFF archive filename.
    zp = archive / "fin_fut_txt_2006_2016.zip"
    _write_zip(
        zp,
        header=_TFF_FULL_HEADER,
        rows=[
            [
                "EURO FX - CHICAGO MERCANTILE EXCHANGE",
                "100105",
                "1/5/2010 12:00:00 AM",  # US datetime, not ISO
                "099741",
                "TFF",
                "00",
                "001",
                "500000",
                "100",
                "200",
                "300",
                "50",
                "400",
                "250",
            ],
        ],
    )
    rows = list(
        iter_cot_rows(
            tmp_path,
            CFTCReport.TFF,
            date_from=date(2010, 1, 1),
            date_to=date(2010, 12, 31),
            contract_codes={"099741"},
        )
    )
    assert len(rows) == 1
    assert rows[0].report_date_tuesday == date(2010, 1, 5)
    assert rows[0].asset_mgr_net == 250


def test_parse_report_date_handles_both_formats():
    from tradedesk.data_sources.cot import _parse_report_date

    assert _parse_report_date("2024-06-04") == date(2024, 6, 4)
    assert _parse_report_date("12/27/2016 12:00:00 AM") == date(2016, 12, 27)
    assert _parse_report_date("1/5/2010 12:00:00 AM") == date(2010, 1, 5)
    assert _parse_report_date("") is None
    assert _parse_report_date("garbage") is None


def test_disaggregated_rows_have_zero_tff_buckets(tmp_path: Path):
    archive = tmp_path / "cftc"
    archive.mkdir(parents=True)
    zp = archive / "fut_disagg_txt_2024.zip"
    _write_zip(
        zp,
        header=_DISAGG_HEADER,
        rows=[_disagg_row("2024-06-04", "088691", 100, 60, 20)],
    )
    rows = list(
        iter_cot_rows(
            tmp_path,
            CFTCReport.DISAGGREGATED,
            date_from=date(2024, 1, 1),
            date_to=date(2024, 12, 31),
            contract_codes={"088691"},
        )
    )
    assert len(rows) == 1
    r = rows[0]
    assert r.commercial_net == 40  # producer/merchant
    assert r.dealer_net == 0
    assert r.asset_mgr_net == 0
    assert r.leveraged_net == 0


# ---------------------------------------------------------------------------
# Mapping sanity
# ---------------------------------------------------------------------------


def test_seven_target_contracts_are_registered():
    """RAD-1078 acceptance: the seven Stage-1 instruments must all map."""
    expected = {"GOLD", "SILVER", "WTI", "BRENT", "NATGAS", "SP500", "TNOTE10"}
    assert expected.issubset(CFTC_CONTRACTS.keys())


def test_commodities_use_disaggregated_financials_use_tff():
    assert CFTC_CONTRACTS["GOLD"].report is CFTCReport.DISAGGREGATED
    assert CFTC_CONTRACTS["WTI"].report is CFTCReport.DISAGGREGATED
    assert CFTC_CONTRACTS["BRENT"].report is CFTCReport.DISAGGREGATED
    assert CFTC_CONTRACTS["NATGAS"].report is CFTCReport.DISAGGREGATED
    assert CFTC_CONTRACTS["SILVER"].report is CFTCReport.DISAGGREGATED
    assert CFTC_CONTRACTS["SP500"].report is CFTCReport.TFF
    assert CFTC_CONTRACTS["TNOTE10"].report is CFTCReport.TFF
