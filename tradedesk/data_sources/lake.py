"""Macro-data lake: materialize free feeds to Parquet and load them back.

This is the **data access pattern** Quanty uses in backtest scripts.  Macro
series are slow (daily/weekly/monthly) tabular time series, so each series is
stored as a single typed Parquet file under the existing market-data lake::

    <lake>/macro/fred/<SERIES_ID>.parquet     # e.g. DGS10.parquet
    <lake>/macro/ecb/<LABEL>.parquet          # e.g. EUR_YLD_2Y.parquet
    <lake>/macro/cftc/<LABEL>.parquet         # e.g. EURUSD.parquet, GOLD.parquet

``<lake>`` defaults to the Dukascopy market-data root (``$TRADEDESK_MARKETDATA``
or ``/paperclip/tradedesk/marketdata``), the same location used for OHLCV.

Frame conventions
-----------------
* **FRED / ECB** — ``DatetimeIndex`` named ``date`` + a single ``value``
  (``float64``) column.  The index is the observation date.
* **CFTC** — ``DatetimeIndex`` named ``date`` = report as-of Tuesday, plus
  ``release_date`` (the Friday publication date, the earliest no-look-ahead
  usable date), ``open_interest`` and the long/short/net columns for the
  ``commercial``, ``dealer``, ``asset_mgr`` and ``leveraged`` buckets.

Load examples
-------------
>>> from tradedesk.data_sources import load_macro_series, load_macro_frame
>>> dgs10 = load_macro_series("FRED", "DGS10")          # date / value
>>> eur = load_macro_series("CFTC", "EURUSD")           # COT positioning frame
>>> rates = load_macro_frame("FRED", ["DGS2", "DGS10", "VIXCLS"])  # wide
"""

from __future__ import annotations

import logging
import os
from datetime import date
from enum import Enum
from pathlib import Path

import pandas as pd

from . import ecb as _ecb
from . import fred as _fred
from .cot import CFTC_CONTRACTS, CFTCContract, load_contract_history

log = logging.getLogger(__name__)

_DEFAULT_LAKE = Path("/paperclip/tradedesk/marketdata")

# Common history start for on-demand / weekly ingest.  Earlier than the COT
# disaggregated archive (2006) is pointless; 2010 gives a clean common window
# across all three sources while keeping files small.
DEFAULT_HISTORY_START = date(2010, 1, 1)

_CFTC_NUMERIC_COLS = [
    "open_interest",
    "commercial_long",
    "commercial_short",
    "commercial_net",
    "dealer_long",
    "dealer_short",
    "dealer_net",
    "asset_mgr_long",
    "asset_mgr_short",
    "asset_mgr_net",
    "leveraged_long",
    "leveraged_short",
    "leveraged_net",
]


class MacroSource(str, Enum):
    """Macro data-source families stored in the lake."""

    FRED = "fred"
    ECB = "ecb"
    CFTC = "cftc"


def default_lake() -> Path:
    """Return the market-data lake root (env ``TRADEDESK_MARKETDATA`` wins)."""
    env = os.environ.get("TRADEDESK_MARKETDATA")
    return Path(env) if env else _DEFAULT_LAKE


def _resolve_lake(lake: Path | str | None) -> Path:
    return default_lake() if lake is None else Path(lake)


def _source(source: MacroSource | str) -> MacroSource:
    return source if isinstance(source, MacroSource) else MacroSource(str(source).lower())


def macro_dir(source: MacroSource | str, *, lake: Path | str | None = None) -> Path:
    """Directory holding ``source`` Parquet files inside the lake."""
    return _resolve_lake(lake) / "macro" / _source(source).value


def macro_path(
    source: MacroSource | str, label: str, *, lake: Path | str | None = None
) -> Path:
    """Path of the Parquet file for one ``(source, label)`` series."""
    return macro_dir(source, lake=lake) / f"{label}.parquet"


# ---------------------------------------------------------------------------
# Write / load
# ---------------------------------------------------------------------------


def _write_parquet(df: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    df.to_parquet(tmp, engine="pyarrow", index=True)
    tmp.replace(path)
    return path


def load_macro_series(
    source: MacroSource | str, label: str, *, lake: Path | str | None = None
) -> pd.DataFrame:
    """Load one materialized series as a date-indexed DataFrame.

    Raises :class:`FileNotFoundError` if the series has not been ingested yet.
    """
    path = macro_path(source, label, lake=lake)
    if not path.exists():
        raise FileNotFoundError(
            f"macro series {source}/{label} not found at {path}; run "
            "`python -m tradedesk.data_sources.ingest` first"
        )
    return pd.read_parquet(path, engine="pyarrow")


def load_macro_frame(
    source: MacroSource | str,
    labels: list[str],
    *,
    column: str = "value",
    lake: Path | str | None = None,
) -> pd.DataFrame:
    """Load several series of one source into a single wide DataFrame.

    Each requested series contributes one column named after its label,
    taking ``column`` from the per-series frame (default ``"value"``, the
    natural choice for FRED/ECB).  The frames are outer-joined on ``date``.
    """
    cols: dict[str, pd.Series] = {}
    for label in labels:
        df = load_macro_series(source, label, lake=lake)
        if column not in df.columns:
            raise KeyError(
                f"series {source}/{label} has no column {column!r} "
                f"(available: {list(df.columns)})"
            )
        cols[label] = df[column]
    if not cols:
        return pd.DataFrame(index=pd.DatetimeIndex([], name="date"))
    wide = pd.concat(cols, axis=1)
    wide.sort_index(inplace=True)
    wide.index.name = "date"
    return wide


def available_macro_series(*, lake: Path | str | None = None) -> dict[str, list[str]]:
    """Map each source to its materialized series labels present in the lake."""
    out: dict[str, list[str]] = {}
    for src in MacroSource:
        d = macro_dir(src, lake=lake)
        labels = sorted(p.stem for p in d.glob("*.parquet")) if d.exists() else []
        out[src.value] = labels
    return out


# ---------------------------------------------------------------------------
# CFTC frame builder
# ---------------------------------------------------------------------------


def cot_history_frame(
    contract: CFTCContract,
    *,
    date_from: date,
    date_to: date,
    cache_dir: Path | str | None = None,
    force_refresh_current_year: bool = True,
) -> pd.DataFrame:
    """Load a contract's COT history as a Tuesday-indexed positioning frame."""
    cache = _resolve_lake(cache_dir)
    rows = load_contract_history(
        cache,
        contract,
        date_from=date_from,
        date_to=date_to,
        force_refresh_current_year=force_refresh_current_year,
    )
    records = [
        {
            "date": pd.Timestamp(r.report_date_tuesday),
            "release_date": pd.Timestamp(r.release_date_friday),
            "open_interest": r.open_interest,
            "commercial_long": r.commercial_long,
            "commercial_short": r.commercial_short,
            "commercial_net": r.commercial_net,
            "dealer_long": r.dealer_long,
            "dealer_short": r.dealer_short,
            "dealer_net": r.dealer_net,
            "asset_mgr_long": r.asset_mgr_long,
            "asset_mgr_short": r.asset_mgr_short,
            "asset_mgr_net": r.asset_mgr_net,
            "leveraged_long": r.leveraged_long,
            "leveraged_short": r.leveraged_short,
            "leveraged_net": r.leveraged_net,
        }
        for r in rows
    ]
    if not records:
        empty = pd.DataFrame(
            columns=["release_date", *_CFTC_NUMERIC_COLS],
            index=pd.DatetimeIndex([], name="date"),
        )
        return empty.astype({c: "int64" for c in _CFTC_NUMERIC_COLS})
    df = pd.DataFrame.from_records(records).set_index("date")
    df.index.name = "date"
    df[_CFTC_NUMERIC_COLS] = df[_CFTC_NUMERIC_COLS].astype("int64")
    return df


# ---------------------------------------------------------------------------
# Materialize
# ---------------------------------------------------------------------------


def materialize_fred(
    *,
    series: dict[str, str] | None = None,
    date_from: date | None = None,
    lake: Path | str | None = None,
    force: bool = True,
) -> dict[str, Path]:
    """Fetch and write FRED series to ``<lake>/macro/fred/``.

    Returns a label -> Parquet path map for the series that succeeded.  A
    network/parse error on one series is logged and skipped, not fatal.
    """
    lake_root = _resolve_lake(lake)
    spec = series if series is not None else _fred.DEFAULT_FRED_SERIES
    written: dict[str, Path] = {}
    for series_id in spec:
        try:
            df = _fred.fetch_fred_series(
                series_id, date_from=date_from, cache_dir=lake_root, force=force
            )
        except (OSError, ValueError) as exc:
            log.warning("FRED %s ingest failed: %s", series_id, exc)
            continue
        if df.empty:
            log.warning("FRED %s returned no observations; skipping", series_id)
            continue
        written[series_id] = _write_parquet(
            df, macro_path(MacroSource.FRED, series_id, lake=lake_root)
        )
    return written


def materialize_ecb(
    *,
    series: dict[str, _ecb.ECBSeries] | None = None,
    date_from: date | None = None,
    lake: Path | str | None = None,
    force: bool = True,
) -> dict[str, Path]:
    """Fetch and write ECB series to ``<lake>/macro/ecb/``."""
    lake_root = _resolve_lake(lake)
    spec = series if series is not None else _ecb.DEFAULT_ECB_SERIES
    written: dict[str, Path] = {}
    for label, ser in spec.items():
        try:
            df = _ecb.fetch_ecb_series(
                ser, date_from=date_from, cache_dir=lake_root, force=force
            )
        except (OSError, ValueError) as exc:
            log.warning("ECB %s ingest failed: %s", label, exc)
            continue
        if df.empty:
            log.warning("ECB %s returned no observations; skipping", label)
            continue
        written[label] = _write_parquet(
            df, macro_path(MacroSource.ECB, label, lake=lake_root)
        )
    return written


def materialize_cftc(
    *,
    contracts: dict[str, CFTCContract] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    lake: Path | str | None = None,
) -> dict[str, Path]:
    """Fetch and write CFTC COT positioning to ``<lake>/macro/cftc/``."""
    lake_root = _resolve_lake(lake)
    spec = contracts if contracts is not None else CFTC_CONTRACTS
    start = date_from or DEFAULT_HISTORY_START
    end = date_to or date.today()
    written: dict[str, Path] = {}
    for label, contract in spec.items():
        try:
            df = cot_history_frame(
                contract, date_from=start, date_to=end, cache_dir=lake_root
            )
        except (OSError, ValueError) as exc:
            log.warning("CFTC %s ingest failed: %s", label, exc)
            continue
        if df.empty:
            log.warning("CFTC %s returned no rows; skipping", label)
            continue
        written[label] = _write_parquet(
            df, macro_path(MacroSource.CFTC, label, lake=lake_root)
        )
    return written


def materialize_all(
    *,
    date_from: date | None = None,
    lake: Path | str | None = None,
) -> dict[str, dict[str, Path]]:
    """Materialize every default series across all three macro sources.

    Each source is isolated: an unexpected failure in one source is logged and
    yields an empty result for that source so the others still materialize. The
    per-series guards inside each ``materialize_*`` handle within-source failures;
    this is belt-and-suspenders for anything they don't catch.
    """
    result: dict[str, dict[str, Path]] = {}
    for name, fn in (
        (MacroSource.FRED.value, materialize_fred),
        (MacroSource.ECB.value, materialize_ecb),
        (MacroSource.CFTC.value, materialize_cftc),
    ):
        try:
            result[name] = fn(date_from=date_from, lake=lake)
        except Exception as exc:
            log.error("macro ingest: %s source failed: %s", name, exc)
            result[name] = {}
    return result
