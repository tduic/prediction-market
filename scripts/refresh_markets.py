"""
Standalone market refresh: fetches all markets from Polymarket and Kalshi,
stores them in the DB, runs the matching engine, and persists matches.

Designed for the predictor-refresh.service (systemd oneshot) and manual use.
Exits after completion — does not start the trading session or websocket streams.

Usage:
    python scripts/refresh_markets.py
"""

import asyncio
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import get_config  # noqa: E402
from core.logging_config import configure_from_env  # noqa: E402
from core.storage.db import Database  # noqa: E402

logger = logging.getLogger(__name__)


async def main() -> None:
    configure_from_env()
    cfg = get_config()

    logger.info("=== Market Refresh ===")
    logger.info("DB: %s", cfg.database.db_path)

    db_wrapper = Database(cfg.database.db_path, migrations_dir=cfg.database.migrations_dir)
    await db_wrapper.init()
    db = db_wrapper._conn

    try:
        from core.ingestor.store import (
            fetch_kalshi_markets,
            fetch_polymarket_markets,
            store_markets,
        )
        from core.matching.engine import find_matches, persist_matches

        logger.info("Fetching markets from exchanges...")
        poly_task = asyncio.create_task(fetch_polymarket_markets())
        kalshi_task = asyncio.create_task(
            fetch_kalshi_markets(
                api_key=cfg.platform_credentials.kalshi_api_key,
                rsa_key_path=cfg.platform_credentials.kalshi_rsa_key_path,
                api_base=cfg.platform_credentials.kalshi_api_base,
            )
        )
        poly_markets, kalshi_markets = await asyncio.gather(poly_task, kalshi_task)

        logger.info(
            "Fetched %d Polymarket + %d Kalshi markets",
            len(poly_markets),
            len(kalshi_markets),
        )

        if not poly_markets and not kalshi_markets:
            logger.error("No markets fetched from either exchange — aborting")
            sys.exit(1)

        await store_markets(db, poly_markets, kalshi_markets)
        logger.info("Markets stored")

        matches = await find_matches(db)
        if matches:
            await persist_matches(db, matches)
            logger.info("Found and persisted %d cross-platform matches", len(matches))
        else:
            logger.warning("No cross-platform matches found")

        logger.info("=== Refresh complete ===")

    finally:
        await db_wrapper.close()


if __name__ == "__main__":
    asyncio.run(main())
