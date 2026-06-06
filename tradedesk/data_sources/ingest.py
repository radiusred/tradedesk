"""CLI to ingest the free macro feeds into the Parquet data lake.

Runs on demand or on a weekly schedule (see ``docs/data_sources_guide.md``)::

    # Ingest everything (FRED + ECB + CFTC) into the default lake.  FRED is
    # incremental (delta on top of the existing parquet); CFTC/ECB use 2010.
    python -m tradedesk.data_sources.ingest

    # Just refresh CFTC positioning into a custom lake
    python -m tradedesk.data_sources.ingest --source cftc --lake /data/marketdata

    # Force a wider history window (overrides the incremental FRED default)
    python -m tradedesk.data_sources.ingest --from 2018-01-01

Ingestion is idempotent: re-running refreshes each series in place.  FRED only
downloads rows newer than what is already on disk (Akamai blocks multi-year
history fetches from data-centre IPs, RAD-3791); CFTC re-downloads the
current-year zip so the latest Friday release appears.
A failure on a single series is logged and skipped, never fatal, so a weekly
cron job stays green even if one upstream endpoint is briefly unavailable.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

from .lake import (
    DEFAULT_HISTORY_START,
    MacroSource,
    default_lake,
    materialize_all,
    materialize_cftc,
    materialize_ecb,
    materialize_fred,
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m tradedesk.data_sources.ingest",
        description="Ingest FRED / ECB / CFTC macro feeds into the Parquet lake.",
    )
    p.add_argument(
        "--source",
        choices=[*[s.value for s in MacroSource], "all"],
        default="all",
        help="Which feed to ingest (default: all).",
    )
    p.add_argument(
        "--lake",
        type=Path,
        default=None,
        help="Market-data lake root (default: $TRADEDESK_MARKETDATA or "
        "/paperclip/tradedesk/marketdata).",
    )
    p.add_argument(
        "--from",
        dest="date_from",
        type=date.fromisoformat,
        default=None,
        help="History start date YYYY-MM-DD (override). Default is incremental: "
        "FRED fetches only rows newer than the existing parquet (trailing 1y on "
        "first run); CFTC/ECB fall back to "
        f"{DEFAULT_HISTORY_START} when no override is given.",
    )
    p.add_argument(
        "--log-level",
        default="info",
        help="Logging level (default: info).",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    lake = args.lake or default_lake()
    logging.getLogger(__name__).info(
        "Ingesting source=%s from=%s into lake=%s", args.source, args.date_from, lake
    )

    if args.source == "all":
        result = materialize_all(date_from=args.date_from, lake=lake)
    elif args.source == MacroSource.FRED.value:
        result = {args.source: materialize_fred(date_from=args.date_from, lake=lake)}
    elif args.source == MacroSource.ECB.value:
        result = {args.source: materialize_ecb(date_from=args.date_from, lake=lake)}
    else:
        result = {args.source: materialize_cftc(date_from=args.date_from, lake=lake)}

    summary = {
        src: {label: str(path) for label, path in written.items()}
        for src, written in result.items()
    }
    counts = {src: len(written) for src, written in result.items()}
    print(json.dumps({"lake": str(lake), "written": counts, "paths": summary}, indent=2))

    total = sum(counts.values())
    if total == 0:
        logging.getLogger(__name__).error("No series materialized — all sources failed")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
