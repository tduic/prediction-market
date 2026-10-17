"""Clear the persistent execution halt after reconciling both venues.

Usage:
    python scripts/clear_halt.py --reason "flattened KXFOO on Kalshi manually"
    python scripts/clear_halt.py --status        # show the halt, change nothing

Loads /data/predictor/.env (the service's EnvironmentFile) and then the repo
.env (local dev) so the default --db matches the service's DB_PATH. Refuses to run against a DB file that does not exist or
has no execution_control table, instead of creating an empty file and
reporting "Not halted".
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite
from dotenv import load_dotenv

from core.engine.execution_control import clear_halt, get_halt


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _check_db(db_path: str) -> str | None:
    """Return an error message if db_path is not a migrated trading DB."""
    path = Path(db_path)
    if not path.is_file():
        return (
            f"ERROR: database {path} does not exist. Pass --db or set DB_PATH "
            "(the service reads it from /data/predictor/.env)."
        )
    try:
        # mode=ro: never create or modify anything while checking. The path
        # is percent-encoded: a raw ?, # or % would end/garble the URI path.
        uri = f"file:{quote(str(path.resolve()))}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'execution_control'"
            ).fetchone()
    except sqlite3.Error as exc:
        return f"ERROR: cannot read database {path}: {exc}"
    if row is None:
        return (
            f"ERROR: {path} has no execution_control table — wrong database or "
            "migrations not applied. Refusing to report halt state."
        )
    return None


async def run(db_path: str, reason: str | None, status_only: bool) -> int:
    problem = _check_db(db_path)
    if problem:
        _err(problem)
        return 2
    try:
        async with aiosqlite.connect(db_path) as db:
            state = await get_halt(db)
            if state is None:
                print("Not halted.")
                return 0
            print(
                f"HALTED since {state['halted_at']} by {state['component']}: "
                f"{state['reason']}"
            )
            if status_only:
                return 0
            assert reason is not None
            await clear_halt(db, reason=reason)
            print("Halt cleared.")
    except aiosqlite.Error as exc:
        _err(f"ERROR: cannot read/update database {db_path}: {exc}")
        return 2
    return 0


def _load_env() -> None:
    # Production first (same file systemd feeds the service), then the repo
    # .env for local dev; load_dotenv never overrides already-set variables.
    load_dotenv("/data/predictor/.env")
    load_dotenv()


async def main() -> int:
    _load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.getenv("DB_PATH", "prediction_market.db"))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--reason", help="Why it is safe to resume trading")
    group.add_argument("--status", action="store_true", help="Show halt state only")
    args = parser.parse_args()
    return await run(args.db, reason=args.reason, status_only=args.status)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
