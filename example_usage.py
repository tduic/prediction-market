"""
Example usage demonstrating all core components of the prediction market system.
Shows proper initialization, event handling, and database operations.
"""

import asyncio
import logging
from datetime import datetime
from uuid import uuid4

from core import get_config, Database, EventBus
from core.events import (
    MarketUpdated,
    ViolationDetected,
    SignalFired,
    PnLSnapshot,
)
from core.storage import queries

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    """Run example demonstrating all system components."""

    # ============================================================================
    # 1. CONFIGURATION
    # ============================================================================
    print("\n" + "=" * 80)
    print("1. CONFIGURATION MANAGEMENT")
    print("=" * 80)

    config = get_config()
    print(f"Paper Trading: {config.observability.paper_trading}")
    print(f"Log Level: {config.observability.log_level}")
    print(f"Max Position Size: ${config.risk_controls.max_position_size_usd}")
    print(f"Kelly Fraction: {config.risk_controls.kelly_fraction}")
    print(f"Database Path: {config.database.database_path}")

    # ============================================================================
    # 2. DATABASE INITIALIZATION
    # ============================================================================
    print("\n" + "=" * 80)
    print("2. DATABASE INITIALIZATION")
    print("=" * 80)

    db = Database(
        config.database.database_path, migrations_dir="./core/storage/migrations"
    )

    try:
        await db.init()
        print("✓ Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        return

    # ============================================================================
    # 3. EVENT BUS SETUP
    # ============================================================================
    print("\n" + "=" * 80)
    print("3. EVENT BUS SETUP")
    print("=" * 80)

    event_bus = EventBus(max_queue_size=10000)

    # Define event handlers
    async def on_market_updated(event: MarketUpdated):
        logger.info(
            f"Market {event.market_id} updated: "
            f"YES=${event.yes_price}, NO=${event.no_price}"
        )

    async def on_violation_detected(event: ViolationDetected):
        logger.warning(
            f"Violation detected! {event.market_id_a} vs {event.market_id_b}: "
            f"Net spread={event.net_spread:.4f}"
        )

    async def on_signal_fired(event: SignalFired):
        logger.info(
            f"Signal fired ({event.strategy}): "
            f"Edge={event.model_edge:.4f}, Size=${event.position_size_a:.2f}"
        )

    # Subscribe to events
    await event_bus.subscribe(MarketUpdated, on_market_updated)
    await event_bus.subscribe(ViolationDetected, on_violation_detected)
    await event_bus.subscribe(SignalFired, on_signal_fired)

    print(
        f"✓ Event bus configured with {event_bus.get_subscriber_count(MarketUpdated)} market subscribers"
    )
    print(
        f"✓ Event bus configured with {event_bus.get_subscriber_count(ViolationDetected)} violation subscribers"
    )
    print(
        f"✓ Event bus configured with {event_bus.get_subscriber_count(SignalFired)} signal subscribers"
    )

    await event_bus.start()
    print("✓ Event bus started")

    # ============================================================================
    # 4. MARKET DATA OPERATIONS
    # ============================================================================
    print("\n" + "=" * 80)
    print("4. MARKET DATA OPERATIONS")
    print("=" * 80)

    market_id_a = f"pm_trump_{uuid4().hex[:8]}"
    market_id_b = f"kalshi_trump_{uuid4().hex[:8]}"

    # Upsert markets
    await queries.markets.upsert_market(
        db,
        market_id=market_id_a,
        platform="polymarket",
        platform_id="trump_approval_2026",
        title="Trump Approval Rating Above 45%",
        description="Will Trump's approval rating be above 45% in Q2 2026?",
        category="politics",
        event_type="approval_rating",
        close_time="2026-06-30T23:59:59Z",
    )

    await queries.markets.upsert_market(
        db,
        market_id=market_id_b,
        platform="kalshi",
        platform_id="TRUMP45_Q2",
        title="Trump Approval > 45%",
        description="Approval rating > 45%",
        category="politics",
        close_time="2026-06-30T23:59:59Z",
    )

    print(f"✓ Inserted markets {market_id_a} and {market_id_b}")

    # Insert price data
    await queries.markets.insert_price(
        db,
        market_id=market_id_a,
        yes_price=0.52,
        no_price=0.48,
        spread=0.04,
        volume_24h=50000,
        liquidity=10000,
    )

    await queries.markets.insert_price(
        db,
        market_id=market_id_b,
        yes_price=0.53,
        no_price=0.47,
        spread=0.06,
        volume_24h=30000,
        liquidity=8000,
    )

    print("✓ Inserted price data")

    # Publish market updated events
    await event_bus.publish(
        MarketUpdated(
            market_id=market_id_a,
            platform="polymarket",
            yes_price=0.52,
            no_price=0.48,
        )
    )

    await event_bus.publish(
        MarketUpdated(
            market_id=market_id_b,
            platform="kalshi",
            yes_price=0.53,
            no_price=0.47,
        )
    )

    # ============================================================================
    # 5. MARKET PAIR AND VIOLATION OPERATIONS
    # ============================================================================
    print("\n" + "=" * 80)
    print("5. VIOLATION DETECTION")
    print("=" * 80)

    pair_id = f"pair_{uuid4().hex[:8]}"
    violation_id = f"vio_{uuid4().hex[:8]}"

    # Insert market pair (manually for demo)
    pair_insert_sql = """
    INSERT INTO market_pairs (
        id, market_id_a, market_id_b, pair_type, relationship,
        similarity_score, match_method, verified, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    await db.execute(
        pair_insert_sql,
        (
            pair_id,
            market_id_a,
            market_id_b,
            "identical_question",
            "same_market_cross_platform",
            0.95,
            "semantic_similarity",
            1,
            datetime.utcnow().isoformat(),
            datetime.utcnow().isoformat(),
        ),
    )

    # Insert violation
    raw_spread = abs(0.52 - 0.53)
    fee_a = 0.52 * config.constraint_engine.fee_rate_polymarket
    fee_b = 0.53 * config.constraint_engine.fee_rate_kalshi
    net_spread = raw_spread - fee_a - fee_b

    await queries.violations.insert_violation(
        db,
        violation_id=violation_id,
        pair_id=pair_id,
        violation_type="spread_violation",
        price_a_at_detect=0.52,
        price_b_at_detect=0.53,
        raw_spread=raw_spread,
        net_spread=net_spread,
        fee_estimate_a=fee_a,
        fee_estimate_b=fee_b,
    )

    print(f"✓ Violation {violation_id} detected")
    print(f"  Raw spread: {raw_spread:.4f}, Net spread: {net_spread:.4f}")

    # Publish violation event
    await event_bus.publish(
        ViolationDetected(
            violation_id=violation_id,
            pair_id=pair_id,
            market_id_a=market_id_a,
            market_id_b=market_id_b,
            platform_a="polymarket",
            platform_b="kalshi",
            violation_type="spread_violation",
            price_a=0.52,
            price_b=0.53,
            raw_spread=raw_spread,
            net_spread=net_spread,
            fee_estimate_a=fee_a,
            fee_estimate_b=fee_b,
        )
    )

    # ============================================================================
    # 6. SIGNAL OPERATIONS
    # ============================================================================
    print("\n" + "=" * 80)
    print("6. SIGNAL GENERATION AND RISK CHECKS")
    print("=" * 80)

    signal_id = f"sig_{uuid4().hex[:8]}"

    # Insert signal
    await queries.signals.insert_signal(
        db,
        signal_id=signal_id,
        strategy="constraint_arb",
        signal_type="market_pair",
        market_id_a=market_id_a,
        market_id_b=market_id_b,
        model_edge=net_spread * 0.7,  # Conservative edge estimate
        kelly_fraction=config.risk_controls.kelly_fraction,
        position_size_a=100.0,
        position_size_b=-95.0,
        total_capital_at_risk=100.0,
        violation_id=violation_id,
        target_price_a=0.52,
        target_price_b=0.53,
    )

    print(f"✓ Signal {signal_id} created")

    # Insert risk checks
    await queries.signals.insert_risk_check(
        db,
        signal_id=signal_id,
        check_type="max_position_size",
        passed=1,
        check_value=100.0,
        threshold=config.risk_controls.max_position_size_usd,
    )

    await queries.signals.insert_risk_check(
        db,
        signal_id=signal_id,
        check_type="daily_loss_limit",
        passed=1,
        check_value=0,
        threshold=config.risk_controls.max_daily_loss_usd,
    )

    print("✓ Risk checks passed")

    # Publish signal event
    await event_bus.publish(
        SignalFired(
            signal_id=signal_id,
            violation_id=violation_id,
            strategy="constraint_arb",
            signal_type="market_pair",
            market_id_a=market_id_a,
            market_id_b=market_id_b,
            target_price_a=0.52,
            target_price_b=0.53,
            model_edge=net_spread * 0.7,
            kelly_fraction=config.risk_controls.kelly_fraction,
            position_size_a=100.0,
            position_size_b=-95.0,
            total_capital_at_risk=100.0,
        )
    )

    # ============================================================================
    # 7. ORDER AND POSITION OPERATIONS
    # ============================================================================
    print("\n" + "=" * 80)
    print("7. ORDER EXECUTION AND POSITIONS")
    print("=" * 80)

    order_id = f"ord_{uuid4().hex[:8]}"
    position_id = f"pos_{uuid4().hex[:8]}"

    # Insert order
    await queries.positions.insert_order(
        db,
        order_id=order_id,
        signal_id=signal_id,
        platform="polymarket",
        market_id=market_id_a,
        side="BUY",
        order_type="LIMIT",
        requested_price=0.515,
        requested_size=100.0,
    )

    print(f"✓ Order {order_id} submitted")

    # Simulate fill
    await queries.positions.update_order(
        db,
        order_id=order_id,
        filled_price=0.516,
        filled_size=100.0,
        status="filled",
        fee_paid=0.516 * 100.0 * config.constraint_engine.fee_rate_polymarket,
    )

    print("✓ Order filled at 0.516")

    # Insert position
    await queries.positions.insert_position(
        db,
        position_id=position_id,
        signal_id=signal_id,
        market_id=market_id_a,
        strategy="constraint_arb",
        side="BUY",
        entry_price=0.516,
        entry_size=100.0,
    )

    print(f"✓ Position {position_id} opened")

    # Update position with current price
    await queries.positions.update_position(
        db,
        position_id=position_id,
        current_price=0.530,
        unrealized_pnl=100.0 * (0.530 - 0.516),
    )

    print("✓ Position marked to market: unrealized PnL = $1.40")

    # ============================================================================
    # 8. PNL SNAPSHOTS
    # ============================================================================
    print("\n" + "=" * 80)
    print("8. PNL TRACKING")
    print("=" * 80)

    snapshot_id = await queries.pnl.insert_snapshot(
        db,
        total_capital=10000.0,
        cash=9800.0,
        open_positions_count=1,
        open_notional=100.0,
        unrealized_pnl=1.40,
        realized_pnl_today=0.0,
        realized_pnl_total=0.0,
        fees_today=1.03,
        fees_total=1.03,
        snapshot_type="signal_fired",
    )

    print(f"✓ PnL snapshot {snapshot_id} recorded")
    print("  Total Capital: $10,000.00")
    print("  Cash: $9,800.00")
    print("  Open Positions: 1")
    print("  Unrealized PnL: $1.40")
    print("  Fees (today): $1.03")

    # Publish snapshot event
    await event_bus.publish(
        PnLSnapshot(
            snapshot_type="signal_fired",
            total_capital=10000.0,
            cash=9800.0,
            open_positions_count=1,
            open_notional=100.0,
            unrealized_pnl=1.40,
            realized_pnl_today=0.0,
            realized_pnl_total=0.0,
            fees_today=1.03,
            fees_total=1.03,
        )
    )

    # ============================================================================
    # 9. DATA RETRIEVAL AND QUERIES
    # ============================================================================
    print("\n" + "=" * 80)
    print("9. DATA RETRIEVAL")
    print("=" * 80)

    # Get signal
    signal = await queries.signals.get_signal(db, signal_id)
    print(f"✓ Retrieved signal: {signal['id']}")
    print(f"  Strategy: {signal['strategy']}")
    print(f"  Status: {signal['status']}")
    print(f"  Edge: {signal['model_edge']:.4f}")

    # Get market
    market = await queries.markets.get_market(db, market_id_a)
    print(f"✓ Retrieved market: {market['title']}")
    print(f"  Platform: {market['platform']}")
    print(f"  Status: {market['status']}")

    # Get open positions
    positions = await queries.positions.get_open_positions(db)
    print(f"✓ Open positions: {len(positions)}")

    # Get market count
    count = await queries.markets.get_market_count(db)
    print(f"✓ Total markets in database: {count}")

    # ============================================================================
    # 10. CLEANUP
    # ============================================================================
    print("\n" + "=" * 80)
    print("10. CLEANUP")
    print("=" * 80)

    # Stop event bus
    await event_bus.stop()
    print("✓ Event bus stopped")

    # Close database
    await db.close()
    print("✓ Database connection closed")

    print("\n" + "=" * 80)
    print("EXAMPLE COMPLETED SUCCESSFULLY")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
