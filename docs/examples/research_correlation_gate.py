"""Correlation gate — minimal end-to-end example.

Stage 2 of the research workflow rejects any candidate whose daily PnL is
>= 0.6 correlated with an existing LIVE sleeve. This script reads a
candidate trade-log CSV plus a directory of LIVE-sleeve trade-log CSVs,
prints the correlation matrix, and exits non-zero if the kill rule fires.

Usage::

    python docs/examples/research_correlation_gate.py \\
        --candidate trades_candidate.csv \\
        --sleeves-dir live_sleeves/ \\
        --threshold 0.6

Each CSV is expected to be a tradedesk fill log with the standard columns
(``instrument`` or ``epic``, ``direction``, ``timestamp``, ``price``,
``size``). The sleeves directory's filename stems become sleeve labels.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from tradedesk.research import correlation_gate, daily_pnl_from_csv


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--candidate", type=Path, required=True)
    p.add_argument("--sleeves-dir", type=Path, required=True)
    p.add_argument("--threshold", type=float, default=0.6)
    p.add_argument("--min-overlap-days", type=int, default=20)
    args = p.parse_args()

    candidate_pnl = daily_pnl_from_csv(args.candidate)
    sleeves = {
        path.stem: daily_pnl_from_csv(path)
        for path in sorted(args.sleeves_dir.glob("*.csv"))
    }

    result = correlation_gate(
        args.candidate.stem,
        candidate_pnl,
        sleeves,
        threshold=args.threshold,
        min_overlap_days=args.min_overlap_days,
    )

    print(f"Candidate: {result.candidate}")
    print(f"Threshold: {result.threshold}")
    print("Candidate vs LIVE sleeves:")
    for name, corr in sorted(result.candidate_vs_sleeves.items()):
        days = result.overlap_days.get(name, 0)
        marker = " *KILL*" if abs(corr) >= result.threshold else ""
        print(f"  {name:<24} corr={corr:+.3f}  days={days}{marker}")

    if result.skipped_sleeves:
        print(
            f"Skipped (overlap < {args.min_overlap_days}d): "
            + ", ".join(result.skipped_sleeves)
        )

    if result.fails_gate:
        print("\nKill rule fired:")
        for name, corr in result.flagged:
            print(f"  {name}: |{corr:+.3f}| >= {result.threshold}")
        return 1
    print("\nGate clear — candidate passes correlation check.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
