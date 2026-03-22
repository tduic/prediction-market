"""
Mock test harness for the prediction market trading lifecycle.

Runs a complete trading session in-process without requiring Redis or external services.
- Setup: Creates SQLite DB, runs migrations, seeds market data
- Violation detection: Generates price scenarios with arbitrage opportunities
- Signal generation: Creates trading signals with Kelly sizing and risk checks
- Execution: Routes orders through mock execution clients
- Post-trade: Records fills, closes positions, records P&L
- Reporting: Generates comprehensive portfolio analytics

Usage:
    python scripts/run_mock_session.py --db-path ./data/mock_session.db --verbose
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

# Setup path for imports
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Set mock execution mode before any imports
os.environ["EXECUTION_MODE"] = "mock"

import aiosqlite  # noqa: E402

logger = logging.getLogger(__name__)


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================


def gen_id(prefix: str = "") -> str:
    """Generate a unique ID with optional prefix."""
    return f"{prefix}{uuid4().hex[:12]}"


def now_utc() -> str:
    """Current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def future_utc(seconds: int = 300) -> str:
    """Future UTC timestamp in ISO format (default 5 minutes ahead)."""
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


# ============================================================================
# MOCK DATA DEFINITIONS
# ============================================================================


MARKET_TEMPLATES = [
    # FOMC/Fed Funds Rate
    {
        "title": "Fed Funds Rate >5.25% at June 2026 FOMC",
        "category": "FOMC",
        "description": "Will the Fed maintain rates above 5.25% at the June meeting?",
        "yes_price": 0.42,
        "platform": "polymarket",
    },
    {
        "title": "Fed Funds Rate >5.25% June 2026",
        "category": "FOMC",
        "description": "FOMC funds rate decision June 2026",
        "yes_price": 0.39,
        "platform": "kalshi",
    },
    {
        "title": "Fed Funds Rate <5.00% at September 2026 FOMC",
        "category": "FOMC",
        "description": "Will the Fed cut rates below 5.00% by September?",
        "yes_price": 0.71,
        "platform": "polymarket",
    },
    {
        "title": "Fed Rate Cut by September 2026",
        "category": "FOMC",
        "description": "At least one rate cut by September FOMC",
        "yes_price": 0.68,
        "platform": "kalshi",
    },
    # CPI
    {
        "title": "CPI YoY >3.0% April 2026",
        "category": "CPI",
        "description": "Will April 2026 CPI YoY exceed 3.0%?",
        "yes_price": 0.55,
        "platform": "polymarket",
    },
    {
        "title": "US CPI April 2026 >3.0%",
        "category": "CPI",
        "description": "April 2026 CPI year-over-year inflation",
        "yes_price": 0.57,
        "platform": "kalshi",
    },
    {
        "title": "CPI YoY <2.5% June 2026",
        "category": "CPI",
        "description": "Will June 2026 CPI drop below 2.5%?",
        "yes_price": 0.32,
        "platform": "polymarket",
    },
    # Elections
    {
        "title": "Trump wins 2028 GOP Primary",
        "category": "Elections",
        "description": "Will Donald Trump win the 2028 Republican presidential primary?",
        "yes_price": 0.68,
        "platform": "polymarket",
    },
    {
        "title": "Trump 2028 GOP Primary Winner",
        "category": "Elections",
        "description": "2028 GOP primary outcome",
        "yes_price": 0.65,
        "platform": "kalshi",
    },
    {
        "title": "Harris wins 2028 Democratic Primary",
        "category": "Elections",
        "description": "Will Kamala Harris win 2028 Democratic primary?",
        "yes_price": 0.45,
        "platform": "polymarket",
    },
    # Crypto
    {
        "title": "Bitcoin >$100,000 by May 2026",
        "category": "Crypto",
        "description": "Will Bitcoin exceed $100,000 by end of May 2026?",
        "yes_price": 0.72,
        "platform": "polymarket",
    },
    {
        "title": "BTC above $100k May 2026",
        "category": "Crypto",
        "description": "Bitcoin price action",
        "yes_price": 0.70,
        "platform": "kalshi",
    },
    {
        "title": "Ethereum >$5,000 by May 2026",
        "category": "Crypto",
        "description": "Will Ethereum reach $5,000?",
        "yes_price": 0.48,
        "platform": "polymarket",
    },
]


# ============================================================================
# DATABASE SETUP
# ============================================================================


async def init_database(db_path: str) -> aiosqlite.Connection:
    """Initialize SQLite database with schema and indexes."""
    logger.info(f"Initializing database at {db_path}")

    db = await aiosqlite.connect(db_path)

    # Read and execute migrations
    migrations_dir = PROJECT_ROOT / "core" / "storage" / "migrations"

    for migration_file in sorted(migrations_dir.glob("*.sql")):
        logger.info(f"Running migration: {migration_file.name}")
        with open(migration_file) as f:
            sql = f.read()
            await db.executescript(sql)

    await db.commit()
    logger.info("Database initialized successfully")

    return db


# ============================================================================
# MOCK DATA SEEDING
# ============================================================================


async def seed_markets(db: aiosqlite.Connection, num_markets: int = 12) -> list[str]:
    """Seed database with mock market data. Returns list of market IDs."""
    logger.info(f"Seeding {num_markets} mock markets")

    market_ids = []
    templates = MARKET_TEMPLATES[: min(num_markets, len(MARKET_TEMPLATES))]

    for idx, template in enumerate(templates):
        market_id = f"mkt_{idx:03d}"
        platform_id = f"{template['platform']}_{idx:03d}"

        await db.execute(
            """
            INSERT INTO markets
            (id, platform, platform_id, title, description, category, event_type,
             status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market_id,
                template["platform"],
                platform_id,
                template["title"],
                template["description"],
                template["category"],
                "binary",
                "open",
                now_utc(),
                now_utc(),
            ),
        )

        # Add initial price snapshot
        no_price = 1.0 - template["yes_price"]
        spread = abs(template["yes_price"] - no_price)

        await db.execute(
            """
            INSERT INTO market_prices
            (market_id, yes_price, no_price, spread, volume_24h, open_interest,
             liquidity, poll_latency_ms, polled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market_id,
                template["yes_price"],
                no_price,
                spread,
                50000.0,  # 24h volume
                100000.0,  # open interest
                75000.0,  # liquidity
                50,  # latency ms
                now_utc(),
            ),
        )

        market_ids.append(market_id)

    await db.commit()
    logger.info(f"Seeded {len(market_ids)} markets")

    return market_ids


async def create_market_pairs(db: aiosqlite.Connection, market_ids: list[str]) -> None:
    """Create market pairs across platforms with similarity scores."""
    logger.info(f"Creating market pairs from {len(market_ids)} markets")

    pair_count = 0

    # Group by category to create realistic pairs
    markets_by_cat: dict[str, list[str]] = {}
    for market_id in market_ids:
        cursor = await db.execute(
            "SELECT category FROM markets WHERE id = ?", (market_id,)
        )
        row = await cursor.fetchone()
        if row:
            cat = row[0]
            if cat not in markets_by_cat:
                markets_by_cat[cat] = []
            markets_by_cat[cat].append(market_id)

    # Create pairs within same category
    for cat, ids in markets_by_cat.items():
        if len(ids) >= 2:
            for i in range(len(ids) - 1):
                pair_id = gen_id("pair_")
                market_a = ids[i]
                market_b = ids[i + 1]

                await db.execute(
                    """
                    INSERT INTO market_pairs
                    (id, market_id_a, market_id_b, pair_type, similarity_score,
                     match_method, verified, active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        pair_id,
                        market_a,
                        market_b,
                        "same_outcome",
                        0.92 + (pair_count * 0.01) % 0.05,  # 0.92-0.97
                        "semantic",
                        1,
                        1,
                        now_utc(),
                        now_utc(),
                    ),
                )
                pair_count += 1

    await db.commit()
    logger.info(f"Created {pair_count} market pairs")


async def add_price_snapshots(db: aiosqlite.Connection) -> None:
    """Add historical price snapshots for pair spread tracking."""
    logger.info("Adding price snapshots for pair history")

    # Get all market pairs
    cursor = await db.execute(
        "SELECT id, market_id_a, market_id_b FROM market_pairs LIMIT 10"
    )
    pairs = await cursor.fetchall()

    for pair_id, market_a, market_b in pairs:
        # Get current prices
        cursor_a = await db.execute(
            "SELECT yes_price FROM market_prices WHERE market_id = ? ORDER BY polled_at DESC LIMIT 1",
            (market_a,),
        )
        row_a = await cursor_a.fetchone()
        price_a = row_a[0] if row_a else 0.5

        cursor_b = await db.execute(
            "SELECT yes_price FROM market_prices WHERE market_id = ? ORDER BY polled_at DESC LIMIT 1",
            (market_b,),
        )
        row_b = await cursor_b.fetchone()
        price_b = row_b[0] if row_b else 0.5

        raw_spread = abs(price_a - price_b)
        fees = 0.02  # 2% total fees
        net_spread = raw_spread - fees

        await db.execute(
            """
            INSERT INTO pair_spread_history
            (pair_id, price_a, price_b, raw_spread, net_spread,
             constraint_satisfied, evaluated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (pair_id, price_a, price_b, raw_spread, net_spread, 1, now_utc()),
        )

    await db.commit()
    logger.info(f"Added price snapshots for {len(pairs)} pairs")


# ============================================================================
# VIOLATION DETECTION
# ============================================================================


async def generate_violations(
    db: aiosqlite.Connection, num_violations: int = 8
) -> list[str]:
    """Generate realistic violation scenarios with arbitrage opportunities."""
    logger.info(f"Generating {num_violations} mock violations")

    # Get all market pairs
    cursor = await db.execute(
        "SELECT id, market_id_a, market_id_b FROM market_pairs LIMIT 20"
    )
    pairs = await cursor.fetchall()

    violation_ids = []

    for idx, (pair_id, market_a, market_b) in enumerate(pairs[:num_violations]):
        # Get current prices
        cursor_a = await db.execute(
            "SELECT yes_price FROM market_prices WHERE market_id = ? ORDER BY polled_at DESC LIMIT 1",
            (market_a,),
        )
        row_a = await cursor_a.fetchone()
        price_a = row_a[0] if row_a else 0.5

        cursor_b = await db.execute(
            "SELECT yes_price FROM market_prices WHERE market_id = ? ORDER BY polled_at DESC LIMIT 1",
            (market_b,),
        )
        row_b = await cursor_b.fetchone()
        price_b = row_b[0] if row_b else 0.5

        # Create a spread by moving one price
        # This simulates cross-platform pricing inefficiency
        if idx % 2 == 0:
            # Spread on market A (YES higher)
            price_a_detect = min(price_a + 0.06, 0.95)
            price_b_detect = price_b
        else:
            # Spread on market B (YES higher)
            price_a_detect = price_a
            price_b_detect = min(price_b + 0.06, 0.95)

        raw_spread = abs(price_a_detect - price_b_detect)
        fee_estimate = 0.02  # 2% each side
        net_spread = raw_spread - (fee_estimate * 2)

        violation_id = gen_id("viol_")

        await db.execute(
            """
            INSERT INTO violations
            (id, pair_id, violation_type, price_a_at_detect, price_b_at_detect,
             raw_spread, net_spread, fee_estimate_a, fee_estimate_b,
             status, detected_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                violation_id,
                pair_id,
                "spread_exceed_threshold",
                price_a_detect,
                price_b_detect,
                raw_spread,
                net_spread,
                fee_estimate,
                fee_estimate,
                "detected",
                now_utc(),
                now_utc(),
            ),
        )

        violation_ids.append(violation_id)

    await db.commit()
    logger.info(f"Generated {len(violation_ids)} violations")

    return violation_ids


# ============================================================================
# SIGNAL GENERATION
# ============================================================================


async def generate_signals(
    db: aiosqlite.Connection, violation_ids: list[str]
) -> list[str]:
    """Generate trading signals for violations with Kelly sizing and risk checks."""
    logger.info(f"Generating trading signals for {len(violation_ids)} violations")

    strategies = ["P1", "P2", "P3", "P4", "P5"]
    signal_ids = []

    for viol_idx, violation_id in enumerate(violation_ids):
        # Get violation details
        cursor = await db.execute(
            """
            SELECT pair_id, price_a_at_detect, price_b_at_detect, net_spread
            FROM violations
            WHERE id = ?
            """,
            (violation_id,),
        )
        viol = await cursor.fetchone()
        if not viol:
            continue

        pair_id, price_a, price_b, net_spread = viol

        # Get pair details
        cursor = await db.execute(
            "SELECT market_id_a, market_id_b FROM market_pairs WHERE id = ?",
            (pair_id,),
        )
        pair = await cursor.fetchone()
        if not pair:
            continue

        market_a, market_b = pair

        # Get market details for platforms
        cursor_ma = await db.execute(
            "SELECT platform FROM markets WHERE id = ?", (market_a,)
        )
        market_a_data = await cursor_ma.fetchone()
        _platform_a = market_a_data[0] if market_a_data else "polymarket"

        cursor_mb = await db.execute(
            "SELECT platform FROM markets WHERE id = ?", (market_b,)
        )
        market_b_data = await cursor_mb.fetchone()
        _platform_b = market_b_data[0] if market_b_data else "kalshi"

        # Signal parameters
        strategy = strategies[viol_idx % len(strategies)]
        signal_id = gen_id("sig_")

        # Edge: model prediction advantage (2-8%)
        edge = 0.02 + (viol_idx % 7) * 0.01  # 2% to 8%

        # Kelly sizing
        kelly_fraction = 0.25  # Quarter-Kelly
        kelly_f = min(edge / 2.0, 0.10) * kelly_fraction  # Simplified Kelly calc

        # Position sizing
        bankroll = 100000.0
        position_size_a = bankroll * kelly_f
        position_size_b = bankroll * kelly_f
        total_capital_at_risk = position_size_a + position_size_b

        # Insert signal
        await db.execute(
            """
            INSERT INTO signals
            (id, violation_id, strategy, signal_type, market_id_a, market_id_b,
             target_price_a, target_price_b, model_fair_value, model_edge,
             kelly_fraction, position_size_a, position_size_b,
             total_capital_at_risk, risk_check_passed,
             status, fired_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?)
            """,
            (
                signal_id,
                violation_id,
                strategy,
                "arb_spread",
                market_a,
                market_b,
                price_a,
                price_b,
                (price_a + price_b) / 2.0,
                edge,
                kelly_fraction,
                position_size_a,
                position_size_b,
                total_capital_at_risk,
                1,  # risk_check_passed
                "queued",
                now_utc(),
                now_utc(),
            ),
        )

        # Log risk checks
        checks = [
            ("position_limit", True, position_size_a, 5000.0),
            ("daily_loss_limit", True, 0.0, 10000.0),
            ("concentration", True, 0.02, 0.3),
            ("min_edge", True, edge, 0.02),
        ]

        for check_type, passed, check_val, threshold in checks:
            await db.execute(
                """
                INSERT INTO risk_check_log
                (signal_id, violation_id, check_type, passed, check_value,
                 threshold, detail, evaluated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    violation_id,
                    check_type,
                    1 if passed else 0,
                    check_val,
                    threshold,
                    f"Check passed for {check_type}",
                    now_utc(),
                ),
            )

        signal_ids.append(signal_id)

    await db.commit()
    logger.info(f"Generated {len(signal_ids)} signals with risk checks")

    return signal_ids


# ============================================================================
# ORDER EXECUTION
# ============================================================================


async def execute_signals(
    db: aiosqlite.Connection, signal_ids: list[str]
) -> list[dict[str, Any]]:
    """Execute trading signals with simulated fills."""
    logger.info(f"Simulating execution for {len(signal_ids)} signals")

    execution_results = []

    for signal_id in signal_ids:
        # Get signal details
        cursor = await db.execute(
            """
            SELECT market_id_a, market_id_b, position_size_a, position_size_b,
                   strategy, model_edge
            FROM signals
            WHERE id = ?
            """,
            (signal_id,),
        )
        sig = await cursor.fetchone()
        if not sig:
            continue

        market_a, market_b, size_a, size_b, strategy, edge = sig

        # Get market platforms and prices
        cursor_a = await db.execute(
            """
            SELECT m.platform, mp.yes_price
            FROM markets m
            LEFT JOIN market_prices mp ON m.id = mp.market_id
            WHERE m.id = ?
            ORDER BY mp.polled_at DESC
            LIMIT 1
            """,
            (market_a,),
        )
        row_a = await cursor_a.fetchone()
        plat_a = row_a[0] if row_a else "polymarket"
        price_a = row_a[1] if row_a else 0.5

        cursor_b = await db.execute(
            """
            SELECT m.platform, mp.yes_price
            FROM markets m
            LEFT JOIN market_prices mp ON m.id = mp.market_id
            WHERE m.id = ?
            ORDER BY mp.polled_at DESC
            LIMIT 1
            """,
            (market_b,),
        )
        row_b = await cursor_b.fetchone()
        plat_b = row_b[0] if row_b else "kalshi"
        price_b = row_b[1] if row_b else 0.5

        # Simulate order executions
        orders = []
        _order_ids = []

        # Leg 1: BUY on market_a
        order_id_1 = gen_id("ord_")
        filled_price_1 = price_a * 1.002  # Slight slippage
        filled_price_1 = min(0.99, max(0.01, filled_price_1))
        fee_1 = size_a * filled_price_1 * 0.02

        await db.execute(
            """
            INSERT INTO orders
            (id, signal_id, platform, platform_order_id, market_id, side,
             order_type, requested_price, requested_size, filled_price,
             filled_size, slippage, fee_paid, status, submitted_at,
             filled_at, submission_latency_ms, fill_latency_ms,
             strategy, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id_1,
                signal_id,
                plat_a,
                order_id_1,
                market_a,
                "buy",
                "limit",
                price_a,
                size_a,
                filled_price_1,
                size_a,
                filled_price_1 - price_a,
                fee_1,
                "filled",
                now_utc(),
                now_utc(),
                100,
                200,
                strategy,
                now_utc(),
            ),
        )

        # Leg 2: SELL on market_b
        order_id_2 = gen_id("ord_")
        filled_price_2 = price_b * 0.998  # Slight slippage
        filled_price_2 = min(0.99, max(0.01, filled_price_2))
        fee_2 = size_b * filled_price_2 * 0.02

        await db.execute(
            """
            INSERT INTO orders
            (id, signal_id, platform, platform_order_id, market_id, side,
             order_type, requested_price, requested_size, filled_price,
             filled_size, slippage, fee_paid, status, submitted_at,
             filled_at, submission_latency_ms, fill_latency_ms,
             strategy, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id_2,
                signal_id,
                plat_b,
                order_id_2,
                market_b,
                "sell",
                "limit",
                price_b,
                size_b,
                filled_price_2,
                size_b,
                price_b - filled_price_2,
                fee_2,
                "filled",
                now_utc(),
                now_utc(),
                100,
                200,
                strategy,
                now_utc(),
            ),
        )

        orders.append(
            {
                "order_id": order_id_1,
                "platform": plat_a,
                "status": "FILLED",
                "filled_price": filled_price_1,
                "filled_size": size_a,
                "fee_paid": fee_1,
            }
        )
        orders.append(
            {
                "order_id": order_id_2,
                "platform": plat_b,
                "status": "FILLED",
                "filled_price": filled_price_2,
                "filled_size": size_b,
                "fee_paid": fee_2,
            }
        )

        execution_results.append(
            {
                "signal_id": signal_id,
                "strategy": strategy,
                "edge": edge,
                "orders": orders,
            }
        )

    await db.commit()
    logger.info(f"Simulated execution of {len(execution_results)} signals")

    return execution_results


# ============================================================================
# POST-TRADE: POSITIONS & P&L
# ============================================================================


async def record_positions(db: aiosqlite.Connection, signal_ids: list[str]) -> None:
    """Record positions from filled orders."""
    logger.info(f"Recording positions for {len(signal_ids)} signals")

    for signal_id in signal_ids:
        # Get signal and order details
        cursor = await db.execute(
            """
            SELECT strategy, market_id_a, market_id_b, position_size_a,
                   position_size_b, model_edge
            FROM signals
            WHERE id = ?
            """,
            (signal_id,),
        )
        sig = await cursor.fetchone()
        if not sig:
            continue

        strategy, market_a, market_b, size_a, size_b, edge = sig

        # Get filled orders for this signal (status = 'filled' in lowercase)
        cursor = await db.execute(
            """
            SELECT id, market_id, side, filled_price, filled_size, fee_paid
            FROM orders
            WHERE signal_id = ? AND status IN ('filled', 'FILLED')
            """,
            (signal_id,),
        )
        orders = await cursor.fetchall()

        # Create position for each filled order
        for order_id, market_id, side, filled_price, filled_size, fee_paid in orders:
            position_id = gen_id("pos_")

            await db.execute(
                """
                INSERT INTO positions
                (id, signal_id, market_id, strategy, side, entry_price,
                 entry_size, current_price, unrealized_pnl, status,
                 opened_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    signal_id,
                    market_id,
                    strategy,
                    side,
                    filled_price,
                    filled_size,
                    filled_price,  # current = entry for now
                    0.0,  # unrealized PnL
                    "open",
                    now_utc(),
                    now_utc(),
                ),
            )

    await db.commit()
    logger.info(f"Recorded positions for {len(signal_ids)} signals")


async def close_positions(db: aiosqlite.Connection) -> None:
    """Simulate position closes with random P&L."""
    logger.info("Closing open positions with simulated P&L")

    # Get all open positions
    cursor = await db.execute("""
        SELECT id, signal_id, market_id, side, entry_price, entry_size,
               strategy
        FROM positions
        WHERE status = 'open'
        LIMIT 50
        """)
    positions = await cursor.fetchall()

    closed_count = 0

    for (
        pos_id,
        signal_id,
        market_id,
        side,
        entry_price,
        entry_size,
        strategy,
    ) in positions:
        # Get signal edge for realistic P&L
        cursor = await db.execute(
            "SELECT model_edge FROM signals WHERE id = ?", (signal_id,)
        )
        sig = await cursor.fetchone()
        edge = sig[0] if sig else 0.03

        # Simulate exit price with edge ± noise
        noise = (hash(pos_id) % 100) / 1000.0 - 0.05  # ±5%
        exit_price = entry_price * (1 + edge + noise)
        exit_price = max(0.01, min(0.99, exit_price))  # Clamp to [0.01, 0.99]

        # Calculate P&L
        if side.upper() == "BUY":
            realized_pnl = (exit_price - entry_price) * entry_size
        else:
            realized_pnl = (entry_price - exit_price) * entry_size

        fee_estimate = entry_price * entry_size * 0.02  # 2% fees

        closed_at = now_utc()
        await db.execute(
            """
            UPDATE positions
            SET exit_price = ?, exit_size = ?, realized_pnl = ?,
                fees_paid = ?, status = 'closed', closed_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                exit_price,
                entry_size,
                realized_pnl,
                fee_estimate,
                closed_at,
                closed_at,
                pos_id,
            ),
        )

        # Write trade_outcomes record for analytics
        trade_id = f"trade_{pos_id[:8]}"
        edge_captured = (
            (realized_pnl / (edge * entry_size * entry_price)) * 100
            if edge > 0 and entry_size > 0 and entry_price > 0
            else 0.0
        )
        await db.execute(
            """
            INSERT INTO trade_outcomes (
                id, signal_id, strategy, violation_id,
                market_id_a, market_id_b, predicted_edge, predicted_pnl,
                actual_pnl, fees_total, edge_captured_pct,
                signal_to_fill_ms, holding_period_ms,
                spread_at_signal, volume_at_signal, liquidity_at_signal,
                resolved_at, created_at
            ) VALUES (?, ?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
            """,
            (
                trade_id,
                signal_id,
                strategy,
                market_id,
                edge,
                edge * entry_size * entry_price,
                realized_pnl,
                fee_estimate,
                min(200.0, max(-100.0, edge_captured)),
                200,  # mock fill latency
                5000,  # mock holding period
                closed_at,
                closed_at,
            ),
        )

        closed_count += 1

    await db.commit()
    logger.info(f"Closed {closed_count} positions with trade outcomes")


async def take_pnl_snapshot(db: aiosqlite.Connection) -> None:
    """Take a PnL snapshot of current portfolio state."""
    logger.info("Taking P&L snapshot")

    # Calculate portfolio metrics
    cursor = await db.execute("""
        SELECT
            COUNT(CASE WHEN status = 'open' THEN 1 END) as open_count,
            SUM(CASE WHEN status = 'open' THEN unrealized_pnl ELSE 0 END) as unrealized,
            SUM(CASE WHEN status = 'closed' THEN realized_pnl ELSE 0 END) as realized_total,
            SUM(CASE WHEN status = 'closed' THEN fees_paid ELSE 0 END) as fees_total
        FROM positions
        """)
    row = await cursor.fetchone()
    open_count = row[0] if row[0] else 0
    unrealized_pnl = row[1] if row[1] else 0.0
    realized_pnl_total = row[2] if row[2] else 0.0
    fees_total = row[3] if row[3] else 0.0

    total_capital = 100000.0
    cash = total_capital - (unrealized_pnl if unrealized_pnl > 0 else 0)

    cursor = await db.execute(
        """
        INSERT INTO pnl_snapshots
        (snapshot_type, total_capital, cash, open_positions_count,
         unrealized_pnl, realized_pnl_total, fees_total,
         pnl_constraint_arb, capital_polymarket, capital_kalshi,
         snapshotted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "scheduled",
            total_capital,
            cash,
            open_count,
            unrealized_pnl,
            realized_pnl_total,
            fees_total,
            realized_pnl_total,  # All PnL from arb for this test
            50000.0,  # Polymarket allocation
            50000.0,  # Kalshi allocation
            now_utc(),
        ),
    )
    snapshot_id = cursor.lastrowid

    # Write per-strategy PnL to normalized table
    strategy_cursor = await db.execute("""
        SELECT
            strategy,
            SUM(CASE WHEN status = 'closed' THEN realized_pnl ELSE 0 END) as realized,
            SUM(CASE WHEN status = 'open' THEN unrealized_pnl ELSE 0 END) as unrealized,
            SUM(fees_paid) as fees,
            COUNT(*) as trade_count,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as win_count
        FROM positions
        GROUP BY strategy
    """)
    strategy_rows = await strategy_cursor.fetchall()
    for srow in strategy_rows:
        await db.execute(
            """INSERT INTO strategy_pnl_snapshots
               (snapshot_id, strategy, realized_pnl, unrealized_pnl, fees, trade_count, win_count)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (snapshot_id, srow[0], srow[1], srow[2], srow[3], srow[4], srow[5]),
        )

    await db.commit()
    logger.info(
        f"Snapshot: {open_count} open, "
        f"unrealized ${unrealized_pnl:.2f}, "
        f"realized ${realized_pnl_total:.2f}"
    )


# ============================================================================
# REPORTING
# ============================================================================


async def generate_report(db: aiosqlite.Connection, verbose: bool = False) -> None:
    """Generate comprehensive end-of-session report."""
    print("\n" + "=" * 80)
    print("MOCK TRADING SESSION REPORT")
    print("=" * 80)
    print(f"Generated: {now_utc()}\n")

    # Setup phase summary
    print("SETUP PHASE")
    print("-" * 80)
    cursor = await db.execute("SELECT COUNT(*) FROM markets")
    market_count = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM market_pairs")
    pair_count = (await cursor.fetchone())[0]
    print(f"  Markets seeded:       {market_count}")
    print(f"  Market pairs created: {pair_count}")

    # Violation detection phase
    print("\nVIOLATION DETECTION PHASE")
    print("-" * 80)
    cursor = await db.execute(
        "SELECT COUNT(*) FROM violations WHERE status = 'detected'"
    )
    violation_count = (await cursor.fetchone())[0]
    cursor = await db.execute(
        "SELECT AVG(net_spread), MIN(net_spread), MAX(net_spread) FROM violations"
    )
    row = await cursor.fetchone()
    avg_spread, min_spread, max_spread = row if row else (0, 0, 0)
    print(f"  Violations detected:   {violation_count}")
    if avg_spread:
        print("  Spread statistics:")
        print(f"    - Average: {avg_spread:.4f}")
        print(f"    - Min:     {min_spread:.4f}")
        print(f"    - Max:     {max_spread:.4f}")

    # Signal generation phase
    print("\nSIGNAL GENERATION PHASE")
    print("-" * 80)
    cursor = await db.execute("SELECT COUNT(*) FROM signals")
    signal_count = (await cursor.fetchone())[0]
    cursor = await db.execute("""
        SELECT strategy, COUNT(*) as count, AVG(model_edge) as avg_edge
        FROM signals
        GROUP BY strategy
        ORDER BY strategy
        """)
    signals_by_strat = await cursor.fetchall()
    print(f"  Total signals generated: {signal_count}")
    print("  By strategy:")
    for strat, count, avg_edge in signals_by_strat:
        print(f"    - {strat}: {count} signals (avg edge {avg_edge:.4f})")

    # Execution phase
    print("\nEXECUTION PHASE")
    print("-" * 80)
    cursor = await db.execute("SELECT COUNT(*) FROM orders")
    order_count = (await cursor.fetchone())[0]
    cursor = await db.execute(
        "SELECT status, COUNT(*) as count FROM orders GROUP BY status"
    )
    orders_by_status = await cursor.fetchall()
    print(f"  Total orders submitted: {order_count}")
    print("  By status:")
    for status, count in orders_by_status:
        print(f"    - {status}: {count}")

    # Post-trade phase
    print("\nPOST-TRADE PHASE")
    print("-" * 80)
    cursor = await db.execute("SELECT COUNT(*) FROM positions WHERE status = 'open'")
    open_pos_count = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM positions WHERE status = 'closed'")
    closed_pos_count = (await cursor.fetchone())[0]
    print(f"  Open positions:        {open_pos_count}")
    print(f"  Closed positions:      {closed_pos_count}")

    # P&L Summary
    print("\nP&L SUMMARY")
    print("-" * 80)
    cursor = await db.execute("""
        SELECT
            SUM(CASE WHEN status = 'closed' THEN realized_pnl ELSE 0 END) as realized_pnl,
            SUM(CASE WHEN status = 'open' THEN unrealized_pnl ELSE 0 END) as unrealized_pnl,
            SUM(fees_paid) as total_fees
        FROM positions
        """)
    row = await cursor.fetchone()
    realized_pnl, unrealized_pnl, total_fees = row if row else (0, 0, 0)
    realized_pnl = realized_pnl if realized_pnl else 0.0
    unrealized_pnl = unrealized_pnl if unrealized_pnl else 0.0
    total_fees = total_fees if total_fees else 0.0

    print(f"  Realized P&L:   ${realized_pnl:>12.2f}")
    print(f"  Unrealized P&L: ${unrealized_pnl:>12.2f}")
    print(f"  Total Fees:     ${total_fees:>12.2f}")
    print(f"  Net P&L:        ${realized_pnl + unrealized_pnl - total_fees:>12.2f}")

    # Snapshot summary
    print("\nLATEST P&L SNAPSHOT")
    print("-" * 80)
    cursor = await db.execute("""
        SELECT total_capital, cash, open_positions_count, unrealized_pnl,
               realized_pnl_total, fees_total, snapshotted_at
        FROM pnl_snapshots
        ORDER BY snapshotted_at DESC
        LIMIT 1
        """)
    row = await cursor.fetchone()
    if row:
        (
            total_cap,
            cash,
            open_count,
            unreal,
            real,
            fees,
            snap_time,
        ) = row
        print(f"  Total Capital:    ${total_cap:>12.2f}")
        print(f"  Cash Available:   ${cash:>12.2f}")
        print(f"  Open Positions:   {open_count:>12}")
        print(f"  Unrealized P&L:   ${unreal:>12.2f}")
        print(f"  Realized P&L:     ${real:>12.2f}")
        print(f"  Fees Paid:        ${fees:>12.2f}")
        print(f"  Snapshot Time:    {snap_time}")

    # Risk check summary
    print("\nRISK CHECK SUMMARY")
    print("-" * 80)
    cursor = await db.execute("""
        SELECT check_type, SUM(CASE WHEN passed = 1 THEN 1 ELSE 0 END) as passed,
               COUNT(*) as total
        FROM risk_check_log
        GROUP BY check_type
        ORDER BY check_type
        """)
    checks = await cursor.fetchall()
    for check_type, passed, total in checks:
        pct = (passed / total * 100) if total > 0 else 0
        print(f"  {check_type:<25} {passed:>3}/{total:<3} ({pct:>5.1f}%)")

    # Platform breakdown
    print("\nPLATFORM BREAKDOWN")
    print("-" * 80)
    cursor = await db.execute("""
        SELECT m.platform, COUNT(o.id) as order_count,
               SUM(CASE WHEN o.status IN ('FILLED', 'filled') THEN 1 ELSE 0 END) as filled_count
        FROM markets m
        LEFT JOIN orders o ON m.id = o.market_id
        GROUP BY m.platform
        """)
    platforms = await cursor.fetchall()
    for platform, order_count, filled_count in platforms:
        filled = filled_count if filled_count else 0
        total = order_count if order_count else 0
        print(f"  {platform:<15} Orders: {total:>3}, Filled: {filled:>3}")

    if verbose:
        # Detailed signal breakdown
        print("\nDETAILED SIGNAL BREAKDOWN (VERBOSE)")
        print("-" * 80)
        cursor = await db.execute("""
            SELECT id, strategy, model_edge, position_size_a, position_size_b,
                   total_capital_at_risk, status
            FROM signals
            ORDER BY fired_at DESC
            LIMIT 10
            """)
        signals = await cursor.fetchall()
        for sig_id, strat, edge, size_a, size_b, risk, status in signals:
            print(
                f"  {sig_id[:8]} {strat} edge={edge:.4f} "
                f"size_a=${size_a:.0f} size_b=${size_b:.0f} risk=${risk:.0f} status={status}"
            )

    print("\n" + "=" * 80)
    print("END OF REPORT")
    print("=" * 80 + "\n")


# ============================================================================
# MAIN
# ============================================================================


async def main() -> None:
    """Main entry point for mock trading session."""
    parser = argparse.ArgumentParser(
        description="Run a complete mock prediction market trading lifecycle",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/run_mock_session.py
  python scripts/run_mock_session.py --db-path ./data/mock_session.db --verbose
  python scripts/run_mock_session.py --num-markets 20 --num-violations 15
        """,
    )

    parser.add_argument(
        "--db-path",
        type=str,
        default=":memory:",
        help="Path to SQLite database (default: in-memory)",
    )
    parser.add_argument(
        "--num-markets",
        type=int,
        default=12,
        help="Number of markets to seed (default: 12)",
    )
    parser.add_argument(
        "--num-violations",
        type=int,
        default=8,
        help="Number of violations to generate (default: 8)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose output",
    )

    args = parser.parse_args()

    # Setup logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    )

    logger.info("Starting mock trading session")
    logger.info(f"  Database: {args.db_path}")
    logger.info(f"  Markets: {args.num_markets}")
    logger.info(f"  Violations: {args.num_violations}")
    logger.info(f"  Verbose: {args.verbose}")

    try:
        # Phase 1: Setup
        logger.info("\n=== PHASE 1: SETUP ===")
        db = await init_database(args.db_path)
        market_ids = await seed_markets(db, args.num_markets)
        await create_market_pairs(db, market_ids)
        await add_price_snapshots(db)

        # Phase 2: Violation detection
        logger.info("\n=== PHASE 2: VIOLATION DETECTION ===")
        violation_ids = await generate_violations(db, args.num_violations)

        # Phase 3: Signal generation
        logger.info("\n=== PHASE 3: SIGNAL GENERATION ===")
        signal_ids = await generate_signals(db, violation_ids)

        # Phase 4: Execution
        logger.info("\n=== PHASE 4: EXECUTION ===")
        execution_results = await execute_signals(db, signal_ids)  # noqa: F841

        # Phase 5: Post-trade
        logger.info("\n=== PHASE 5: POST-TRADE ===")
        await record_positions(db, signal_ids)
        await close_positions(db)
        await take_pnl_snapshot(db)

        # Phase 6: Reporting
        logger.info("\n=== PHASE 6: REPORTING ===")
        await generate_report(db, args.verbose)

        # Cleanup
        await db.close()

        # Database persistence message
        if args.db_path != ":memory:":
            logger.info(f"Database saved to: {args.db_path}")
        else:
            logger.info("In-memory database discarded")

        logger.info("Mock trading session completed successfully")

    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
