"""Clear the persistent execution halt after reconciling both venues.

Usage:
    python scripts/clear_halt.py --reason "flattened KXFOO on Kalshi manually"
    python scripts/clear_halt.py --status        # show the halt, change nothing
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite

from core.engine.execution_control import clear_halt, get_halt


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.getenv("DB_PATH", "prediction_market.db"))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--reason", help="Why it is safe to resume trading")
    group.add_argument("--status", action="store_true", help="Show halt state only")
    args = parser.parse_args()

    async with aiosqlite.connect(args.db) as db:
        state = await get_halt(db)
        if state is None:
            print("Not halted.")
            return 0
        print(
            f"HALTED since {state['halted_at']} by {state['component']}: "
            f"{state['reason']}"
        )
        if args.status:
            return 0
        await clear_halt(db, reason=args.reason)
        print("Halt cleared.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
