"""FRED (Federal Reserve Economic Data) downloader and parser.

Fetches US rates + volatility series from the St. Louis Fed using the
**keyless** ``fredgraph.csv`` download endpoint — no API key required::

    https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES_ID>

The default series cover the documented gaps for EUR/UK/JP carry tests
(`RAD-2020`) and FOMC-surprise direction (`RAD-2029`):

================  ====================================================  =========
Series id         Description                                           Frequency
================  ====================================================  =========
``DFF``           Effective Federal Funds Rate                          daily
``DGS3MO``        3-Month Treasury constant-maturity yield              daily
``DGS2``          2-Year Treasury constant-maturity yield               daily
``DGS10``         10-Year Treasury constant-maturity yield              daily
``T10Y2Y``        10Y-2Y Treasury yield spread                          daily
``VIXCLS``        CBOE Volatility Index (VIX) close                     daily
================  ====================================================  =========

Output of :func:`fetch_fred_series` / :func:`parse_fred_csv` is a tidy
:class:`pandas.DataFrame` indexed by observation ``date`` with a single
``value`` column (``float64``).  Missing observations (FRED writes ``"."``
for non-publishing days) are dropped.
"""

from __future__ import annotations

import csv
import io
import logging
import urllib.parse
from datetime import date
from pathlib import Path

import pandas as pd

from ._http import get_text

log = logging.getLogger(__name__)

FRED_BASE_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"

# Default ingest set: series id -> human-readable description.
DEFAULT_FRED_SERIES: dict[str, str] = {
    "DFF": "Effective Federal Funds Rate (daily)",
    "DGS3MO": "3-Month Treasury constant-maturity yield (daily)",
    "DGS2": "2-Year Treasury constant-maturity yield (daily)",
    "DGS10": "10-Year Treasury constant-maturity yield (daily)",
    "T10Y2Y": "10Y-2Y Treasury yield spread (daily)",
    "VIXCLS": "CBOE Volatility Index (VIX) close (daily)",
    # OECD MEI call money / interbank overnight policy proxies (monthly, %).
    # Added for RAD-4033 monetary-policy relative-tightening FX surrogate: a
    # single-source, single-frequency, single-lag cross-section over EUR/GBP/
    # AUD/JPY vs USD. The daily fredgraph CSV endpoint 504s on large daily
    # series (IUDSOIA/DFF) and EUR_ESTR only starts 2019-10, so the monthly
    # IRSTCI01 family is the consistent overnight-rate cross-section back to 2009.
    "IRSTCI01USM156N": "US call money/interbank rate, OECD MEI (monthly, %)",
    "IRSTCI01EZM156N": "Euro-area call money/interbank rate, OECD MEI (monthly, %)",
    "IRSTCI01GBM156N": "UK call money/interbank rate, OECD MEI (monthly, %)",
    "IRSTCI01AUM156N": "Australia call money/interbank rate, OECD MEI (monthly, %)",
    "IRSTCI01JPM156N": "Japan call money/interbank rate, OECD MEI (monthly, %)",
}


def _fred_csv_url(series_id: str, date_from: date | None = None) -> str:
    """Build the keyless fredgraph CSV URL for ``series_id``."""
    params: dict[str, str] = {"id": series_id}
    if date_from is not None:
        # ``cosd`` = "change observation start date".
        params["cosd"] = date_from.isoformat()
    return f"{FRED_BASE_URL}?{urllib.parse.urlencode(params)}"


def download_fred_csv(
    series_id: str,
    *,
    date_from: date | None = None,
    cache_dir: Path | None = None,
    force: bool = False,
    timeout: float = 60.0,
) -> str:
    """Download the raw fredgraph CSV text for ``series_id``.

    When ``cache_dir`` is given the raw CSV is cached under
    ``cache_dir/fred/<series_id>.csv`` and reused unless ``force`` is set.
    """
    cache_path: Path | None = None
    if cache_dir is not None:
        cache_path = cache_dir / "fred" / f"{series_id}.csv"
        if cache_path.exists() and not force:
            return cache_path.read_text(encoding="utf-8")

    url = _fred_csv_url(series_id, date_from)
    log.info("Downloading FRED series %s from %s", series_id, url)
    text = get_text(url, timeout=timeout)

    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(text, encoding="utf-8")
    return text


def parse_fred_csv(text: str) -> pd.DataFrame:
    """Parse fredgraph CSV text into a date-indexed ``value`` DataFrame.

    The header is ``observation_date,<SERIES_ID>``.  Rows whose value is the
    FRED missing-data sentinel ``"."`` (or blank) are dropped.
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return _empty_value_frame()
    if len(header) < 2:
        raise ValueError(f"unexpected FRED CSV header: {header!r}")

    dates: list[date] = []
    values: list[float] = []
    for row in reader:
        if len(row) < 2:
            continue
        raw_date, raw_value = row[0].strip(), row[1].strip()
        if not raw_date:
            continue
        if not raw_value or raw_value == ".":
            continue
        try:
            d = date.fromisoformat(raw_date)
            v = float(raw_value)
        except ValueError:
            log.debug("skipping unparseable FRED row %r", row)
            continue
        dates.append(d)
        values.append(v)

    if not dates:
        return _empty_value_frame()
    df = pd.DataFrame(
        {"value": values},
        index=pd.DatetimeIndex(pd.to_datetime(dates), name="date"),
    )
    df.sort_index(inplace=True)
    return df


def fetch_fred_series(
    series_id: str,
    *,
    date_from: date | None = None,
    cache_dir: Path | None = None,
    force: bool = False,
    timeout: float = 60.0,
) -> pd.DataFrame:
    """Download and parse a FRED series into a date-indexed DataFrame."""
    text = download_fred_csv(
        series_id,
        date_from=date_from,
        cache_dir=cache_dir,
        force=force,
        timeout=timeout,
    )
    return parse_fred_csv(text)


def _empty_value_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {"value": pd.Series(dtype="float64")},
        index=pd.DatetimeIndex([], name="date"),
    )
