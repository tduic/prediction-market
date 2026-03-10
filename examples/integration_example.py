"""
Integration example: Using constraint engine and matching layer together.

This example demonstrates how to set up the constraint engine and matching
layer in a production system.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List

from core.constraints import ConstraintEngine
from core.constraints.engine import ConstraintConfig, MarketPair, MarketData
from core.constraints.fees import FeeConfig
from core.matching import MarketPairCurator, MarketEmbedder
from core.matching.rules import match_by_rules


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SimpleEventBus:
    """Simple in-memory event bus for demonstration."""

    def __init__(self):
        self._subscribers: Dict[str, List[Any]] = {}

    def subscribe(self, event_type: str, handler: Any) -> None:
        """Subscribe to an event type."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Any) -> None:
        """Unsubscribe from an event type."""
        if event_type in self._subscribers:
            self._subscribers[event_type].remove(handler)

    def publish(self, event_type: str, event_data: Any) -> None:
        """Publish an event."""
        if event_type in self._subscribers:
            for handler in self._subscribers[event_type]:
                asyncio.create_task(handler(event_data))


class MockDatabase:
    """Mock database for demonstration."""

    def __init__(self):
        self.markets: Dict[str, MarketData] = {}
        self.pairs: Dict[str, MarketPair] = {}
        self.violations: List[Dict[str, Any]] = []
        self.spread_history: List[Dict[str, Any]] = []

    async def get_market(self, market_id: str) -> MarketData:
        """Get market data."""
        return self.markets.get(market_id)

    async def get_pairs_for_market(self, market_id: str) -> List[MarketPair]:
        """Get pairs for a market."""
        return [
            pair for pair in self.pairs.values()
            if pair.market_id_a == market_id or pair.market_id_b == market_id
        ]

    async def insert_market_pair(
        self,
        market_id_a: str,
        market_id_b: str,
        pair_type: str,
        relationship: str,
        match_method: str,
        similarity_score: float,
        is_active: bool,
        created_by: str,
        created_at: datetime
    ) -> str:
        """Insert a market pair."""
        pair_id = f"pair_{len(self.pairs) + 1}"
        # Would store to database in production
        return pair_id

    async def get_active_market_pairs(
        self,
        pair_type: str = None
    ) -> List[MarketPair]:
        """Get active pairs."""
        pairs = [p for p in self.pairs.values() if p.is_active]
        if pair_type:
            pairs = [p for p in pairs if p.pair_type == pair_type]
        return pairs

    async def get_market_pairs_by_market_id(
        self,
        market_id: str
    ) -> List[MarketPair]:
        """Get pairs for a market."""
        return [
            p for p in self.pairs.values()
            if p.market_id_a == market_id or p.market_id_b == market_id
        ]

    async def insert_pair_spread_history(self, entry: Any) -> None:
        """Record spread history."""
        self.spread_history.append(entry.__dict__)

    async def insert_violation(
        self,
        violation_id: str,
        pair_id: str,
        market_id_a: str,
        market_id_b: str,
        rule_type: str,
        severity: str,
        description: str,
        implied_arbitrage: float,
        detected_at: datetime,
        is_new: bool
    ) -> None:
        """Record a violation."""
        self.violations.append({
            "violation_id": violation_id,
            "pair_id": pair_id,
            "rule_type": rule_type,
            "severity": severity,
            "implied_arbitrage": implied_arbitrage,
            "is_new": is_new,
        })


async def example_constraint_engine():
    """Example: Running the constraint engine."""
    logger.info("=== Constraint Engine Example ===")

    # Setup
    event_bus = SimpleEventBus()
    db = MockDatabase()

    # Add sample markets
    db.markets["poly_1"] = MarketData(
        market_id="poly_1",
        platform="polymarket",
        title="Will Trump win 2024?",
        current_price=0.65,
        last_updated=datetime.utcnow()
    )
    db.markets["kalshi_1"] = MarketData(
        market_id="kalshi_1",
        platform="kalshi",
        title="Trump wins 2024",
        current_price=0.67,
        last_updated=datetime.utcnow()
    )

    # Add a market pair
    db.pairs["pair_1"] = MarketPair(
        pair_id="pair_1",
        market_id_a="poly_1",
        market_id_b="kalshi_1",
        pair_type="cross_platform",
        relationship=None,
        market_a_title="Will Trump win 2024?",
        market_b_title="Trump wins 2024",
        platform_a="polymarket",
        platform_b="kalshi",
        is_active=True
    )

    # Configure constraint engine
    fee_config = FeeConfig(polymarket=0.02, kalshi=0.02)
    config = ConstraintConfig(
        min_net_spread_cross_platform=0.03,
        fee_config=fee_config,
        enable_logging=True
    )

    # Create and start engine
    engine = ConstraintEngine(event_bus, db, config)
    await engine.start()

    # Subscribe to violations
    violations_detected = []

    async def on_violation(event):
        violations_detected.append(event)
        logger.warning(
            f"Violation detected: {event.rule_type} - "
            f"{event.implied_arbitrage:.2f}% arbitrage"
        )

    event_bus.subscribe("ConstraintViolationDetected", on_violation)

    # Simulate market update
    logger.info("Publishing MarketUpdated event for poly_1")
    await engine._on_market_updated({"market_id": "poly_1"})

    # Check results
    await asyncio.sleep(0.1)  # Give async handlers time to run
    logger.info(f"Violations detected: {len(violations_detected)}")
    logger.info(f"Spread history recorded: {len(db.spread_history)}")

    await engine.stop()


async def example_market_matching():
    """Example: Discovering market pairs with matching layer."""
    logger.info("\n=== Market Matching Example ===")

    # Rule-based matching
    logger.info("Testing rule-based matching...")
    result = match_by_rules(
        market_a_title="FOMC Interest Rate Decision March 2026 75bps",
        market_b_title="Federal Reserve 75bp Hike Probability March",
        category="economic"
    )
    if result:
        logger.info(
            f"Rule match found: type={result.pair_type}, "
            f"confidence={result.confidence:.2f}"
        )
    else:
        logger.info("No rule-based match found")

    # Semantic matching
    logger.info("Testing semantic matching...")
    embedder = MarketEmbedder()

    if embedder.is_available():
        matches = embedder.find_matches(
            "Federal Reserve Interest Rate Decision",
            [
                "FOMC Rate Decision",
                "Fed Hikes Rates 50bps",
                "Trump Election 2024",
                "S&P 500 above 6000"
            ],
            threshold=0.70
        )
        logger.info(f"Semantic matches: {len(matches)} found")
        for idx, similarity in matches:
            logger.info(f"  Index {idx}: similarity={similarity:.3f}")
    else:
        logger.info("Sentence-transformers not available, skipping semantic matching")


async def example_pair_curation():
    """Example: Managing market pairs."""
    logger.info("\n=== Pair Curation Example ===")

    db = MockDatabase()
    curator = MarketPairCurator(db)

    # Simulate adding pairs
    logger.info("Adding market pairs...")
    try:
        pair_id = await curator.add_pair(
            market_id_a="poly_001",
            market_id_b="kalshi_001",
            pair_type="cross_platform",
            match_method="rules",
            created_by="system"
        )
        logger.info(f"Created pair: {pair_id}")
    except Exception as e:
        logger.error(f"Error adding pair: {e}")

    # Get active pairs
    logger.info("Retrieving active pairs...")
    pairs = await curator.get_active_pairs()
    logger.info(f"Active pairs: {len(pairs)}")


async def example_end_to_end():
    """End-to-end example with both layers."""
    logger.info("\n=== End-to-End Integration Example ===")

    # 1. Discover pairs using matching layer
    logger.info("Step 1: Discovering market pairs...")
    new_markets = [
        ("Will Bitcoin exceed $100k in 2024?", "polymarket"),
        ("Bitcoin price above $100k", "kalshi"),
        ("BTC/USD > 100000", "metaculus"),
    ]

    matched_pairs = []
    for title, platform in new_markets:
        result = match_by_rules(title, "Bitcoin exceeds $100k")
        if result:
            matched_pairs.append((title, result))

    logger.info(f"Found {len(matched_pairs)} potential pairs")

    # 2. Setup constraint engine
    logger.info("Step 2: Setting up constraint engine...")
    event_bus = SimpleEventBus()
    db = MockDatabase()

    fee_config = FeeConfig(
        polymarket=0.02,
        kalshi=0.02,
        manifold=0.01,
        metaculus=0.00
    )
    config = ConstraintConfig(
        min_net_spread_single_platform=0.02,
        min_net_spread_cross_platform=0.03,
        fee_config=fee_config
    )

    engine = ConstraintEngine(event_bus, db, config)
    await engine.start()

    # 3. Monitor for violations
    logger.info("Step 3: Monitoring for violations...")
    violations = []

    async def handle_violation(event):
        violations.append(event)
        logger.warning(f"Found exploitable spread: {event.implied_arbitrage:.2f}%")

    event_bus.subscribe("ConstraintViolationDetected", handle_violation)

    logger.info(f"Detected {len(violations)} violations")

    await engine.stop()


async def main():
    """Run all examples."""
    logger.info("Starting integration examples...\n")

    await example_constraint_engine()
    await example_market_matching()
    await example_pair_curation()
    await example_end_to_end()

    logger.info("\nAll examples completed successfully!")


if __name__ == "__main__":
    asyncio.run(main())
