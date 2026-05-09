"""CFTC Commitment of Traders (COT) data downloader and parser.

Fetches the weekly COT history from cftc.gov and exposes it as a sequence of
:class:`COTRow` records keyed by contract.  Two CFTC report families are
supported:

* **Disaggregated Futures-Only** — physical commodities (gold, silver, oil,
  natural gas, etc.).  The "commercials" bucket here is
  ``Producer/Merchant/Processor/User``.
* **Traders in Financial Futures (TFF)** — equity indices and treasuries.
  TFF has no producer bucket; the dealer/intermediary bucket is the closest
  analogue to "commercials" and is what we expose in :attr:`COTRow.commercial_long`
  / :attr:`COTRow.commercial_short`.

URL conventions (verified 2026-05-09):

* Disaggregated annual: ``https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip``
* Disaggregated 2006-2016 archive: ``fut_disagg_txt_hist_2006_2016.zip``
* TFF annual: ``https://www.cftc.gov/files/dea/history/fut_fin_txt_{year}.zip``
* TFF 2006-2016 archive: ``fin_fut_txt_2006_2016.zip``

The CFTC publishes these reports each Friday at 15:30 ET, with effective
as-of date the prior Tuesday close.  See :func:`cot_release_date` for the
release-date offset used by strategies that key entries off publication time.
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from pathlib import Path
from typing import Iterable, Iterator

log = logging.getLogger(__name__)


# CFTC publishes reports each Friday at 15:30 ET, three days after the
# Tuesday as-of date.  ``cot_release_date`` always returns this scheduled
# date even when the actual publication slips to Monday for a Friday
# federal holiday — strategies should use the release date plus the
# next-trading-day rule on the price series, which absorbs that slip.
_RELEASE_OFFSET_DAYS = 3


class CFTCReport(str, Enum):
    """CFTC report family."""

    DISAGGREGATED = "disagg"
    """Disaggregated futures-only — physical commodities + softs + energy."""

    TFF = "tff"
    """Traders in Financial Futures — equity indices, treasuries, FX."""


@dataclass(frozen=True)
class CFTCContract:
    """Identifier for a CFTC-tracked contract."""

    label: str
    """Internal short label used in tradedesk configs (e.g. ``"GOLD"``)."""

    code: str
    """CFTC contract market code (e.g. ``"088691"``)."""

    report: CFTCReport
    """Which COT report contains this contract."""


# RAD-1078 — initial mapping for the COT commercials-extreme strategy.
# All seven contracts have been verified against the 2024 annual zip on 2026-05-09.
CFTC_CONTRACTS: dict[str, CFTCContract] = {
    "GOLD": CFTCContract("GOLD", "088691", CFTCReport.DISAGGREGATED),
    "SILVER": CFTCContract("SILVER", "084691", CFTCReport.DISAGGREGATED),
    "WTI": CFTCContract("WTI", "067651", CFTCReport.DISAGGREGATED),
    "BRENT": CFTCContract("BRENT", "06765T", CFTCReport.DISAGGREGATED),
    "NATGAS": CFTCContract("NATGAS", "023651", CFTCReport.DISAGGREGATED),
    # SP500 + 10Y T-Note are financial futures and live in the TFF report.
    # The "Dealer/Intermediary" bucket is the closest TFF analogue of the
    # disaggregated "Producer/Merchant" bucket; it is exposed via the
    # ``commercial_*`` fields on :class:`COTRow`.
    "SP500": CFTCContract("SP500", "13874A", CFTCReport.TFF),
    "TNOTE10": CFTCContract("TNOTE10", "043602", CFTCReport.TFF),
}


@dataclass(frozen=True)
class COTRow:
    """One weekly COT observation for a single contract.

    ``commercial_*`` semantics depend on the report family:

    * Disaggregated → Producer/Merchant/Processor/User positions.
    * TFF → Dealer/Intermediary positions (closest analogue).

    The :class:`COTRow.report` field records which family produced the row
    so callers can tell the two apart.
    """

    report_date_tuesday: date
    release_date_friday: date
    cftc_code: str
    market_name: str
    report: CFTCReport
    open_interest: int
    commercial_long: int
    commercial_short: int
    commercial_net: int


def cot_release_date(report_date_tuesday: date) -> date:
    """Return the Friday on which the CFTC publishes a Tuesday-as-of report.

    The CFTC schedule is **fixed at three calendar days after the as-of
    Tuesday**.  When the Friday falls on a US federal holiday the actual
    publication may slip to Monday, but Stage 1 backtests do not encode
    that calendar — instead they enter on the first trading day strictly
    after the returned date, which absorbs holiday slip on the price side.
    """
    return report_date_tuesday + timedelta(days=_RELEASE_OFFSET_DAYS)


# ---------------------------------------------------------------------------
# Disk cache + download
# ---------------------------------------------------------------------------


_CFTC_BASE_URL = "https://www.cftc.gov/files/dea/history"


def _cache_subdir(cache_dir: Path) -> Path:
    """Location of CFTC zip archives inside ``cache_dir``.

    Mirrors the ``cache_dir/cftc/`` convention used elsewhere
    (Dukascopy keeps its raw ticks under ``cache_dir/dukascopy/``).
    """
    return cache_dir / "cftc"


def _zip_filename(report: CFTCReport, year: int | None) -> str:
    """Filename of the zip on cftc.gov.

    ``year`` is ``None`` for the consolidated 2006-2016 archive.
    """
    if report is CFTCReport.DISAGGREGATED:
        if year is None:
            return "fut_disagg_txt_hist_2006_2016.zip"
        return f"fut_disagg_txt_{year}.zip"
    if report is CFTCReport.TFF:
        if year is None:
            # NB. the historical TFF archive flips the leading tokens
            # (``fin_fut_…`` rather than ``fut_fin_…``).  This mirrors the
            # CFTC's own filename and is verified against their listing.
            return "fin_fut_txt_2006_2016.zip"
        return f"fut_fin_txt_{year}.zip"
    raise ValueError(f"unsupported report family: {report!r}")


def _zip_url(report: CFTCReport, year: int | None) -> str:
    return f"{_CFTC_BASE_URL}/{_zip_filename(report, year)}"


def download_cot_zip(
    cache_dir: Path,
    report: CFTCReport,
    year: int | None,
    *,
    force: bool = False,
) -> Path:
    """Download a single CFTC zip into ``cache_dir/cftc/`` and return its path.

    Existing files are reused; pass ``force=True`` to re-download (e.g. to
    refresh the current calendar year as new weeks are published).
    """
    out_dir = _cache_subdir(cache_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    fname = _zip_filename(report, year)
    out_path = out_dir / fname
    if out_path.exists() and not force:
        return out_path

    url = _zip_url(report, year)
    log.info("Downloading CFTC archive %s", url)
    tmp_path = out_path.with_suffix(out_path.suffix + ".part")
    with urllib.request.urlopen(url) as resp, open(tmp_path, "wb") as fh:
        # Stream in chunks so 12 MB historical zips do not blow up memory.
        while True:
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            fh.write(chunk)
    tmp_path.rename(out_path)
    return out_path


def _years_for_range(date_from: date, date_to: date) -> tuple[bool, list[int]]:
    """Return ``(needs_2006_2016_archive, [annual_year, …])`` for a date range.

    The CFTC publishes:

    * one consolidated 2006-2016 archive
    * one zip per calendar year from 2010 onwards (2010 partial)

    For a backtest spanning 2006-01-01 → 2026-01-01 we need the consolidated
    archive plus annuals 2017..2026.
    """
    needs_archive = date_from.year < 2017
    first_annual = max(date_from.year, 2017)
    last_annual = date_to.year
    annuals = list(range(first_annual, last_annual + 1))
    return needs_archive, annuals


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _to_int(value: str) -> int:
    """Parse a CFTC integer field, treating blanks and dashes as zero.

    CFTC text reports occasionally use ``"."`` or empty strings for
    suppressed/insufficient-positions cells.  We coerce to ``0`` rather
    than error out so callers can decide whether the row is usable.
    """
    s = value.strip()
    if not s or s in {".", "-"}:
        return 0
    # CFTC stores positions as plain integers; some files use a decimal
    # point e.g. "12345.0", so be lenient.
    return int(float(s.replace(",", "")))


def _parse_zip(
    zip_path: Path,
    report: CFTCReport,
    contract_codes: set[str] | None,
) -> Iterator[COTRow]:
    """Yield :class:`COTRow` objects for ``contract_codes`` from a CFTC zip.

    The zip contains a single tab- or comma-delimited text file (CFTC uses
    CSV with quoted strings since ~2010).  ``contract_codes=None`` yields
    every contract in the archive.
    """
    if report is CFTCReport.DISAGGREGATED:
        long_col = "Prod_Merc_Positions_Long_All"
        short_col = "Prod_Merc_Positions_Short_All"
    elif report is CFTCReport.TFF:
        long_col = "Dealer_Positions_Long_All"
        short_col = "Dealer_Positions_Short_All"
    else:
        raise ValueError(f"unsupported report family: {report!r}")

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if not names:
            raise ValueError(f"empty CFTC archive: {zip_path}")
        for name in names:
            with zf.open(name) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
                reader = csv.DictReader(text)
                for row in reader:
                    code = (row.get("CFTC_Contract_Market_Code") or "").strip()
                    if not code:
                        continue
                    if contract_codes is not None and code not in contract_codes:
                        continue
                    report_date_str = (row.get("Report_Date_as_YYYY-MM-DD") or "").strip()
                    if not report_date_str:
                        continue
                    try:
                        rdate = date.fromisoformat(report_date_str)
                    except ValueError:
                        log.debug("skipping unparseable report_date=%r", report_date_str)
                        continue
                    long_v = _to_int(row.get(long_col) or "")
                    short_v = _to_int(row.get(short_col) or "")
                    oi = _to_int(row.get("Open_Interest_All") or "")
                    yield COTRow(
                        report_date_tuesday=rdate,
                        release_date_friday=cot_release_date(rdate),
                        cftc_code=code,
                        market_name=(row.get("Market_and_Exchange_Names") or "").strip(),
                        report=report,
                        open_interest=oi,
                        commercial_long=long_v,
                        commercial_short=short_v,
                        commercial_net=long_v - short_v,
                    )


def iter_cot_rows(
    cache_dir: Path,
    report: CFTCReport,
    *,
    date_from: date,
    date_to: date,
    contract_codes: Iterable[str] | None = None,
    force_refresh_current_year: bool = False,
) -> Iterator[COTRow]:
    """Yield COT rows for ``date_from..date_to`` from the named report family.

    Downloads any missing zips into ``cache_dir/cftc/`` first.  When
    ``force_refresh_current_year`` is true, the calendar-year zip for
    ``date_to.year`` is re-downloaded so newly published weeks appear.
    Rows are yielded in archive order; callers should sort by
    ``report_date_tuesday`` if order matters.
    """
    codes = set(contract_codes) if contract_codes is not None else None
    needs_archive, annuals = _years_for_range(date_from, date_to)
    current_year = date.today().year

    if needs_archive:
        zp = download_cot_zip(cache_dir, report, None)
        for row in _parse_zip(zp, report, codes):
            if date_from <= row.report_date_tuesday <= date_to:
                yield row

    for year in annuals:
        force = force_refresh_current_year and year == current_year
        zp = download_cot_zip(cache_dir, report, year, force=force)
        for row in _parse_zip(zp, report, codes):
            if date_from <= row.report_date_tuesday <= date_to:
                yield row


def load_contract_history(
    cache_dir: Path,
    contract: CFTCContract,
    *,
    date_from: date,
    date_to: date,
    force_refresh_current_year: bool = False,
) -> list[COTRow]:
    """Load and sort the full COT history for one contract.

    Convenience wrapper around :func:`iter_cot_rows` that filters by
    ``contract.code`` and returns the rows sorted by the as-of Tuesday
    date (ascending).  Strategies should call this once during warmup.
    """
    rows = list(
        iter_cot_rows(
            cache_dir,
            contract.report,
            date_from=date_from,
            date_to=date_to,
            contract_codes={contract.code},
            force_refresh_current_year=force_refresh_current_year,
        )
    )
    rows.sort(key=lambda r: r.report_date_tuesday)
    return rows
