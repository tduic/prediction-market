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
from core.storage.db import Database  # noqa: E402

configure_from_env()
logger = logging.getLogger(__name__)


async def main() -> None:
    cfg = get_config()
    db_wrapper = Database(cfg.database.db_path, migrations_dir=cfg.database.migrations_dir)
    await db_wrapper.init()
    try:
        await take_phase0_baseline_snapshot(db_wrapper._conn)
        logger.info("Baseline snapshot complete.")
    finally:
        await db_wrapper.close()


if __name__ == "__main__":
    asyncio.run(main())
