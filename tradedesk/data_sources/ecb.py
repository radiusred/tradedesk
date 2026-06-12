"""ECB Data Portal (Statistical Data Warehouse) downloader and parser.

Fetches EUR rates from the ECB Data Portal using the free, **keyless**
SDMX CSV endpoint — no API key required::

    https://data-api.ecb.europa.eu/service/data/<FLOW>/<KEY>?format=csvdata

Default series cover the EUR carry + rate-surprise gaps for `RAD-2987`:

==================  ======  ============================================  =========
Label               Flow    Description                                   Frequency
==================  ======  ============================================  =========
``EUR_ESTR``        EST     Euro short-term rate (€STR, overnight)        daily
``EUR_YLD_3M``      YC      AAA gov yield-curve spot rate, 3-month        daily
``EUR_YLD_6M``      YC      AAA gov yield-curve spot rate, 6-month        daily
``EUR_YLD_1Y``      YC      AAA gov yield-curve spot rate, 1-year         daily
``EUR_YLD_2Y``      YC      AAA gov yield-curve spot rate, 2-year         daily
``EUR_YLD_5Y``      YC      AAA gov yield-curve spot rate, 5-year         daily
``EUR_YLD_10Y``     YC      AAA gov yield-curve spot rate, 10-year        daily
``EUR_EURIBOR_1M``  FM      Euribor 1-month, period average               monthly
``EUR_EURIBOR_3M``  FM      Euribor 3-month, period average               monthly
==================  ======  ============================================  =========

Term **OIS** swap fixings (1w/1m/3m) are not published by the ECB as a free
single series.  ``EUR_ESTR`` is the EUR OIS *reference* (overnight) rate and
the short-end yield-curve tenors give the market-implied term structure;
these are the free, no-auth proxies for OIS-implied magnitude.  See the
RAD-2987 thread for the Quanty coordination on which fields a rate-surprise
study needs.

Output of :func:`fetch_ecb_series` / :func:`parse_ecb_csv` is a tidy
:class:`pandas.DataFrame` indexed by observation ``date`` with a single
``value`` column (``float64``).  Monthly ``TIME_PERIOD`` values (``YYYY-MM``)
are anchored to the first calendar day of the month.
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.parse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from ._http import get_text

log = logging.getLogger(__name__)

ECB_BASE_URL = "https://data-api.ecb.europa.eu/service/data"


@dataclass(frozen=True)
class ECBSeries:
    """An ECB Data Portal series addressed by ``flow`` + ``key``."""

    label: str
    """Internal short label used in the macro lake (e.g. ``"EUR_YLD_2Y"``)."""

    flow: str
    """SDMX dataflow ref (e.g. ``"YC"``, ``"EST"``, ``"FM"``)."""

    key: str
    """SDMX series key (dot-separated dimensions)."""

    description: str
    """Human-readable description."""


def _yc_key(tenor: str) -> str:
    """AAA government yield-curve spot-rate key for a maturity ``tenor``."""
    return f"B.U2.EUR.4F.G_N_A.SV_C_YM.SR_{tenor}"


DEFAULT_ECB_SERIES: dict[str, ECBSeries] = {
    "EUR_ESTR": ECBSeries(
        "EUR_ESTR", "EST", "B.EU000A2X2A25.WT", "Euro short-term rate (€STR, overnight)"
    ),
    "EUR_YLD_3M": ECBSeries(
        "EUR_YLD_3M", "YC", _yc_key("3M"), "AAA gov yield-curve spot rate, 3-month"
    ),
    "EUR_YLD_6M": ECBSeries(
        "EUR_YLD_6M", "YC", _yc_key("6M"), "AAA gov yield-curve spot rate, 6-month"
    ),
    "EUR_YLD_1Y": ECBSeries(
        "EUR_YLD_1Y", "YC", _yc_key("1Y"), "AAA gov yield-curve spot rate, 1-year"
    ),
    "EUR_YLD_2Y": ECBSeries(
        "EUR_YLD_2Y", "YC", _yc_key("2Y"), "AAA gov yield-curve spot rate, 2-year"
    ),
    "EUR_YLD_5Y": ECBSeries(
        "EUR_YLD_5Y", "YC", _yc_key("5Y"), "AAA gov yield-curve spot rate, 5-year"
    ),
    "EUR_YLD_10Y": ECBSeries(
        "EUR_YLD_10Y", "YC", _yc_key("10Y"), "AAA gov yield-curve spot rate, 10-year"
    ),
    "EUR_EURIBOR_1M": ECBSeries(
        "EUR_EURIBOR_1M",
        "FM",
        "M.U2.EUR.RT.MM.EURIBOR1MD_.HSTA",
        "Euribor 1-month, period average (monthly)",
    ),
    "EUR_EURIBOR_3M": ECBSeries(
        "EUR_EURIBOR_3M",
        "FM",
        "M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA",
        "Euribor 3-month, period average (monthly)",
    ),
    # ECB daily reference exchange rates (EXR dataflow, ~16:00 CET fixing,
    # EUR base). Added for RAD-4033 FX surrogate: one robust, full-history
    # (1999->present) daily-close source for all four USD pairs via EUR
    # triangulation, sidestepping the FX-tick lake gap (GBPUSD/AUDUSD only
    # 2018+) and the fredgraph CSV 504 on daily series.
    "EUR_USD_REF": ECBSeries(
        "EUR_USD_REF", "EXR", "D.USD.EUR.SP00.A", "ECB reference rate USD per EUR (daily)"
    ),
    "EUR_GBP_REF": ECBSeries(
        "EUR_GBP_REF", "EXR", "D.GBP.EUR.SP00.A", "ECB reference rate GBP per EUR (daily)"
    ),
    "EUR_AUD_REF": ECBSeries(
        "EUR_AUD_REF", "EXR", "D.AUD.EUR.SP00.A", "ECB reference rate AUD per EUR (daily)"
    ),
    "EUR_JPY_REF": ECBSeries(
        "EUR_JPY_REF", "EXR", "D.JPY.EUR.SP00.A", "ECB reference rate JPY per EUR (daily)"
    ),
}


def _ecb_csv_url(series: ECBSeries, date_from: date | None = None) -> str:
    """Build the keyless ECB Data Portal CSV URL for ``series``."""
    params: dict[str, str] = {"format": "csvdata"}
    if date_from is not None:
        params["startPeriod"] = date_from.isoformat()
    query = urllib.parse.urlencode(params)
    return f"{ECB_BASE_URL}/{series.flow}/{series.key}?{query}"


def download_ecb_csv(
    series: ECBSeries,
    *,
    date_from: date | None = None,
    cache_dir: Path | None = None,
    force: bool = False,
    timeout: float = 60.0,
) -> str:
    """Download the raw ECB CSV text for ``series``.

    When ``cache_dir`` is given the raw CSV is cached under
    ``cache_dir/ecb/<label>.csv`` and reused unless ``force`` is set.
    """
    cache_path: Path | None = None
    if cache_dir is not None:
        cache_path = cache_dir / "ecb" / f"{series.label}.csv"
        if cache_path.exists() and not force:
            return cache_path.read_text(encoding="utf-8")

    url = _ecb_csv_url(series, date_from)
    log.info("Downloading ECB series %s from %s", series.label, url)
    text = get_text(url, timeout=timeout)

    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(text, encoding="utf-8")
    return text


def parse_ecb_csv(text: str) -> pd.DataFrame:
    """Parse ECB SDMX CSV text into a date-indexed ``value`` DataFrame.

    The ECB CSV is wide with many SDMX attribute columns; we keep only
    ``TIME_PERIOD`` (the observation date) and ``OBS_VALUE``.  Monthly
    periods (``YYYY-MM``) are anchored to the first day of the month.
    """
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or "OBS_VALUE" not in reader.fieldnames:
        return _empty_value_frame()

    dates: list[pd.Timestamp] = []
    values: list[float] = []
    for row in reader:
        raw_period = (row.get("TIME_PERIOD") or "").strip()
        raw_value = (row.get("OBS_VALUE") or "").strip()
        if not raw_period or not raw_value:
            continue
        try:
            ts = _parse_period(raw_period)
            v = float(raw_value)
        except ValueError:
            log.debug("skipping unparseable ECB row period=%r value=%r", raw_period, raw_value)
            continue
        dates.append(ts)
        values.append(v)

    if not dates:
        return _empty_value_frame()
    df = pd.DataFrame(
        {"value": values},
        index=pd.DatetimeIndex(dates, name="date"),
    )
    df.sort_index(inplace=True)
    return df


def _parse_period(raw_period: str) -> pd.Timestamp:
    """Convert an SDMX ``TIME_PERIOD`` to a Timestamp.

    Handles daily (``YYYY-MM-DD``), monthly (``YYYY-MM``) and annual
    (``YYYY``) periods.  Monthly/annual periods anchor to the first day.
    """
    parts = raw_period.split("-")
    if len(parts) == 3:
        return pd.Timestamp(date.fromisoformat(raw_period))
    if len(parts) == 2:
        return pd.Timestamp(int(parts[0]), int(parts[1]), 1)
    if len(parts) == 1 and parts[0].isdigit():
        return pd.Timestamp(int(parts[0]), 1, 1)
    raise ValueError(f"unsupported TIME_PERIOD: {raw_period!r}")


def fetch_ecb_series(
    series: ECBSeries,
    *,
    date_from: date | None = None,
    cache_dir: Path | None = None,
    force: bool = False,
    timeout: float = 60.0,
) -> pd.DataFrame:
    """Download and parse an ECB series into a date-indexed DataFrame."""
    text = download_ecb_csv(
        series,
        date_from=date_from,
        cache_dir=cache_dir,
        force=force,
        timeout=timeout,
    )
    return parse_ecb_csv(text)


def _empty_value_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {"value": pd.Series(dtype="float64")},
        index=pd.DatetimeIndex([], name="date"),
    )
