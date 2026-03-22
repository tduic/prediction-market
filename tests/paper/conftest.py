"""
Shared async fixtures for paper trading tests.

Uses the REAL migration schema (001_initial.sql) so that every INSERT
exercised here is guaranteed to match the production DB.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import pytest_asyncio

# Ensure project root is importable
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

MIGRATIONS_DIR = PROJECT_ROOT / "core" / "storage" / "migrations"


async def _apply_migrations(db: aiosqlite.Connection) -> None:
    """Run all .sql migration files in order."""
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        sql = sql_file.read_text()
        await db.executescript(sql)
    await db.execute("PRAGMA foreign_keys = ON")
    await db.commit()


@pytest_asyncio.fixture
async def db():
    """In-memory aiosqlite connection with the real schema applied."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await _apply_migrations(conn)
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def db_with_markets(db):
    """DB pre-loaded with a small set of Polymarket + Kalshi markets and prices."""
    now = datetime.now(timezone.utc).isoformat()

    poly_markets = [
        (
            "poly_abc123",
            "polymarket",
            "cond_abc123",
            "Will Bitcoin exceed $100,000 by end of 2025?",
            now,
            now,
        ),
        (
            "poly_def456",
            "polymarket",
            "cond_def456",
            "Will the Fed cut rates in March 2026?",
            now,
            now,
        ),
        (
            "poly_ghi789",
            "polymarket",
            "cond_ghi789",
            "Will US GDP growth exceed 3% in Q1 2026?",
            now,
            now,
        ),
        (
            "poly_jkl012",
            "polymarket",
            "cond_jkl012",
            "Will SpaceX launch Starship successfully in April 2026?",
            now,
            now,
        ),
        (
            "poly_mno345",
            "polymarket",
            "cond_mno345",
            "Will Apple stock price exceed $250 by June 2026?",
            now,
            now,
        ),
    ]

    kalshi_markets = [
        (
            "kal_BTC100K",
            "kalshi",
            "BTC100K",
            "Bitcoin above $100,000 by end of 2025?",
            now,
            now,
        ),
        (
            "kal_FEDCUT-MAR26",
            "kalshi",
            "FEDCUT-MAR26",
            "Federal Reserve rate cut in March 2026?",
            now,
            now,
        ),
        (
            "kal_GDPQ1-26",
            "kalshi",
            "GDPQ1-26",
            "US GDP growth above 3% in Q1 2026?",
            now,
            now,
        ),
        (
            "kal_SPACEX-APR26",
            "kalshi",
            "SPACEX-APR26",
            "SpaceX Starship successful launch in April 2026?",
            now,
            now,
        ),
        (
            "kal_UNRELATED",
            "kalshi",
            "UNRELATED",
            "Will it snow in Miami in July 2026?",
            now,
            now,
        ),
    ]

    await db.executemany(
        """INSERT INTO markets (id, platform, platform_id, title, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'open', ?, ?)""",
        poly_markets,
    )
    await db.executemany(
        """INSERT INTO markets (id, platform, platform_id, title, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'open', ?, ?)""",
        kalshi_markets,
    )

    # Prices (poly slightly cheaper than kalshi to create arb spreads)
    prices = [
        ("poly_abc123", 0.55, 0.45, 0.02, 50000, now),
        ("poly_def456", 0.40, 0.60, 0.03, 30000, now),
        ("poly_ghi789", 0.30, 0.70, 0.02, 20000, now),
        ("poly_jkl012", 0.65, 0.35, 0.04, 10000, now),
        ("poly_mno345", 0.50, 0.50, 0.03, 15000, now),
        ("kal_BTC100K", 0.60, 0.40, 0.03, 40000, now),
        ("kal_FEDCUT-MAR26", 0.45, 0.55, 0.04, 25000, now),
        ("kal_GDPQ1-26", 0.33, 0.67, 0.02, 18000, now),
        ("kal_SPACEX-APR26", 0.70, 0.30, 0.05, 8000, now),
        ("kal_UNRELATED", 0.10, 0.90, 0.02, 5000, now),
    ]

    await db.executemany(
        """INSERT INTO market_prices (market_id, yes_price, no_price, spread, liquidity, polled_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        prices,
    )
    await db.commit()

    yield db
