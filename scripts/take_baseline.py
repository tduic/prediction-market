"""
Take a phase0 baseline snapshot against the live DB.

Records current pair counts and per-strategy PnL as a reference point.
Safe to call multiple times — each call appends a new row.

Usage:
    python scripts/take_baseline.py
"""

import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv("/data/predictor/.env")
load_dotenv()  # fallback for local dev

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import get_config  # noqa: E402
from core.logging_config import configure_from_env  # noqa: E402
from core.snapshots.phase0 import take_phase0_baseline_snapshot  # noqa: E402

configure_from_env()
logger = logging.getLogger(__name__)


async def main() -> None:
    import aiosqlite

    cfg = get_config()
    db_path = cfg.database.db_path
    # Open with a 30-second busy timeout so we wait for the trading session
    # to release its write lock rather than failing immediately.
    async with aiosqlite.connect(db_path, timeout=30) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await take_phase0_baseline_snapshot(db)
        logger.info("Baseline snapshot complete.")


if __name__ == "__main__":
    asyncio.run(main())
