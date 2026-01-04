"""
IG DEMO smoke test: open then net-close a position via opposite market order.

Usage:
  IG_API_KEY=... IG_USERNAME=... IG_PASSWORD=... IG_ENVIRONMENT=DEMO \
    python -m examples.ig_smoke_trade --epic <EPIC> --size 1

Notes:
- Assumes spread betting + netting semantics (forceOpen=False).
- “Close” is achieved by placing the opposite direction with the same size.
"""

import argparse
import asyncio
import json
import logging
import sys
from typing import Any
from dotenv import load_dotenv

from tradedesk.providers.ig.client import IGClient
from tradedesk.providers.ig.settings import settings


load_dotenv()  # reads .env from CWD by default
log = logging.getLogger(__name__)

def _configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if root.hasHandlers():
        return
    root.setLevel(level.upper())
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root.addHandler(h)


def _pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=str)


async def _get_positions(client: IGClient) -> dict[str, Any]:
    # Not currently wrapped by IGClient; use raw request for the smoke test.
    return await client._request("GET", "/positions")  # noqa: SLF001


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epic", required=True, help="IG EPIC to trade")
    parser.add_argument("--size", type=float, default=1.0, help="Bet size (e.g. £/point for SB)")
    parser.add_argument("--open-direction", choices=["BUY", "SELL"], default="BUY")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--skip-positions", action="store_true", help="Skip GET /positions checks")
    args = parser.parse_args()

    _configure_logging(args.log_level)

    # Ensure creds exist
    settings.validate()

    epic: str = args.epic
    size: float = args.size
    open_dir: str = args.open_direction
    close_dir: str = "SELL" if open_dir == "BUY" else "BUY"

    client = IGClient()
    await client.start()

    try:
        snapshot = await client.get_market_snapshot(epic)
        status = (snapshot.get("snapshot", {}) or {}).get("marketStatus")
        log.info("Market status for %s: %s", epic, status)

        if status and str(status).upper() != "TRADEABLE":
            raise RuntimeError(f"Market not tradeable: {status}")

        if not args.skip_positions:
            before = await _get_positions(client)
            log.info("Positions (before):\n%s", _pretty(before))

        log.info("Placing OPEN %s %s size=%s", epic, open_dir, size)
        open_confirm = await client.place_market_order_confirmed(
            epic=epic,
            direction=open_dir,
            size=size,
            currency="GBP",
            force_open=False,
            confirm_timeout_s=30.0,
            confirm_poll_s=0.5,
        )
        log.info("OPEN confirm:\n%s", _pretty(open_confirm))
        if (open_confirm.get("dealStatus") or "").upper() != "ACCEPTED":
            raise RuntimeError(f"Open rejected: {open_confirm}")

        log.info("Placing CLOSE %s %s size=%s (netting)", epic, close_dir, size)
        close_confirm = await client.place_market_order_confirmed(
            epic=epic,
            direction=close_dir,
            size=size,
            currency="GBP",
            force_open=False,
            confirm_timeout_s=30.0,
            confirm_poll_s=0.5,
        )
        log.info("CLOSE confirm:\n%s", _pretty(close_confirm))
        if (close_confirm.get("dealStatus") or "").upper() != "ACCEPTED":
            raise RuntimeError(f"Close rejected: {close_confirm}")

        if not args.skip_positions:
            after = await _get_positions(client)
            log.info("Positions (after):\n%s", _pretty(after))

        log.info("Smoke test complete: OPEN + net-CLOSE both ACCEPTED.")
        return 0

    finally:
        await client.close()



if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
