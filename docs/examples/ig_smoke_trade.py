"""
IG DEMO smoke test: open then net-close a position via opposite market order.

Usage:
  IG_API_KEY=... IG_USERNAME=... IG_PASSWORD=... IG_ENVIRONMENT=DEMO \
    python ../tradedesk/docs/examples/ig_smoke_trade.py --epic <EPIC> --size 1

Notes:
- Assumes netting semantics (force_open=False). “Close” is achieved by placing the opposite
  direction with the same size.
- Does not expose account-type logic; IGClient should route SB/CFD internally.
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
    # Smoke-test convenience: not all clients wrap this yet.
    return await client._request("GET", "/positions")  # noqa: SLF001


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--epic", required=True, help="IG EPIC to trade")
    parser.add_argument("--size", type=float, default=1.0, help="Size (stake for SB, contracts for CFD)")
    parser.add_argument("--open-direction", choices=["BUY", "SELL"], default="BUY")
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--skip-positions", action="store_true", help="Skip GET /positions checks")
    parser.add_argument("--confirm-timeout", type=float, default=30.0)
    parser.add_argument("--confirm-poll", type=float, default=0.5)
    parser.add_argument("--force-open", action="store_true", help="Set force_open=True (hedging)")

    args = parser.parse_args()
    _configure_logging(args.log_level)

    # Validate creds early (fail fast, cleanly)
    try:
        settings.validate()
    except Exception as e:
        log.error("Settings validation failed: %s", e)
        return 2

    epic: str = args.epic
    size: float = args.size
    open_dir: str = args.open_direction
    close_dir: str = "SELL" if open_dir == "BUY" else "BUY"

    try:
        async with IGClient() as client:
            snapshot = await client.get_market_snapshot(epic)
            status = (snapshot.get("snapshot", {}) or {}).get("marketStatus")
            log.info("Market status for %s: %s", epic, status)

            if status and str(status).upper() != "TRADEABLE":
                log.error("Market not tradeable: %s", status)
                return 1

            if not args.skip_positions:
                before = await _get_positions(client)
                log.info("Positions (before):\n%s", _pretty(before))

            log.info("Placing OPEN %s %s size=%s force_open=%s", epic, open_dir, size, args.force_open)
            open_confirm = await client.place_market_order_confirmed(
                epic=epic,
                direction=open_dir,
                size=size,
                currency="GBP",
                force_open=args.force_open,
                confirm_timeout_s=args.confirm_timeout,
                confirm_poll_s=args.confirm_poll,
            )
            log.info("OPEN confirm:\n%s", _pretty(open_confirm))

            if (open_confirm.get("dealStatus") or "").upper() != "ACCEPTED":
                # Surface the rejection reason cleanly
                reason = open_confirm.get("reason") or open_confirm.get("rejectReason") or "UNKNOWN"
                log.error("OPEN rejected: %s", reason)
                return 1

            log.info("Placing CLOSE %s %s size=%s force_open=%s (netting unless force_open=True)", epic, close_dir, size, args.force_open)
            close_confirm = await client.place_market_order_confirmed(
                epic=epic,
                direction=close_dir,
                size=size,
                currency="GBP",
                force_open=args.force_open,
                confirm_timeout_s=args.confirm_timeout,
                confirm_poll_s=args.confirm_poll,
            )
            log.info("CLOSE confirm:\n%s", _pretty(close_confirm))

            if (close_confirm.get("dealStatus") or "").upper() != "ACCEPTED":
                reason = close_confirm.get("reason") or close_confirm.get("rejectReason") or "UNKNOWN"
                log.error("CLOSE rejected: %s", reason)
                return 1

            if not args.skip_positions:
                after = await _get_positions(client)
                log.info("Positions (after):\n%s", _pretty(after))

            log.info("Smoke test complete: OPEN + net-CLOSE both ACCEPTED.")
            return 0

    except KeyboardInterrupt:
        log.warning("Interrupted.")
        return 130
    except Exception as e:
        # Graceful “top level” error handling
        log.error("Smoke test failed: %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
