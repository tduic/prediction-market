"""Main constraint engine for prediction market arbitrage detection."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set

from core.constraints.fees import FeeEstimator, FeeConfig
from core.constraints.rules import (
    cross_platform,
    complementarity,
    mutual_exclusivity,
    subset_superset,
)


logger = logging.getLogger(__name__)


@dataclass
class ConstraintConfig:
    """Configuration for constraint engine."""

    min_net_spread_single_platform: float = 0.02  # 2%
    min_net_spread_cross_platform: float = 0.03  # 3%
    complementarity_tolerance: float = 0.01  # 1%
    fee_config: Optional[FeeConfig] = None
    enable_logging: bool = True
    max_violation_age_seconds: int = 3600  # 1 hour


@dataclass
class MarketPair:
    """A pair of related markets."""

    pair_id: str
    market_id_a: str
    market_id_b: str
    pair_type: str  # e.g., "complement", "subset", "cross_platform", "mutual_exclusivity"
    relationship: Optional[str]  # e.g., "subset", "superset" for subset_superset
    market_a_title: str
    market_b_title: str
    platform_a: Optional[str]
    platform_b: Optional[str]
    is_active: bool


@dataclass
class MarketData:
    """Current market data."""

    market_id: str
    platform: str
    title: str
    current_price: float
    last_updated: datetime


@dataclass
class ViolationEvent:
    """Event emitted when constraint violation detected."""

    violation_id: str
    pair_id: str
    market_id_a: str
    market_id_b: str
    rule_type: str
    severity: str
    description: str
    implied_arbitrage: float
    detected_at: datetime
    is_new: bool


@dataclass
class SpreadHistoryEntry:
    """Entry in pair spread history."""

    pair_id: str
    market_a_price: float
    market_b_price: float
    raw_spread: float
    net_spread: float
    rule_violations: List[str]
    recorded_at: datetime


class ConstraintEngine:
    """Main constraint evaluation engine for prediction markets."""

    def __init__(
        self,
        event_bus: Any,
        db: Any,
        config: Optional[ConstraintConfig] = None,
    ):
        """
        Initialize constraint engine.

        Args:
            event_bus: Event bus for publishing violations
            db: Database instance
            config: Constraint configuration
        """
        self.event_bus = event_bus
        self.db = db
        self.config = config or ConstraintConfig()
        self.fee_estimator = FeeEstimator(self.config.fee_config or FeeConfig())

        # Track known violations to avoid duplicate events
        self._active_violations: Dict[str, datetime] = {}

        # Rule checkers
        self._rule_checkers = {
            "subset_superset": self._check_subset_superset,
            "complement": self._check_complementarity,
            "mutual_exclusivity": self._check_mutual_exclusivity,
            "cross_platform": self._check_cross_platform,
        }

        if self.config.enable_logging:
            logger.info(
                f"ConstraintEngine initialized with config: "
                f"single_platform={self.config.min_net_spread_single_platform}, "
                f"cross_platform={self.config.min_net_spread_cross_platform}"
            )

    async def start(self) -> None:
        """Start the constraint engine and subscribe to market updates."""
        logger.info("Starting ConstraintEngine")
        self.event_bus.subscribe("MarketUpdated", self._on_market_updated)

    async def stop(self) -> None:
        """Stop the constraint engine."""
        logger.info("Stopping ConstraintEngine")
        self.event_bus.unsubscribe("MarketUpdated", self._on_market_updated)

    async def _on_market_updated(self, event: Dict[str, Any]) -> None:
        """
        Handle market update event.

        Args:
            event: Market update event with market_id and updated prices
        """
        try:
            market_id = event.get("market_id")
            if not market_id:
                logger.warning("Received MarketUpdated event without market_id")
                return

            # Load related market pairs from DB
            pairs = await self.db.get_pairs_for_market(market_id)

            if not pairs:
                return

            # Process each pair
            for pair in pairs:
                await self._evaluate_pair(pair)

        except Exception as e:
            logger.error(f"Error processing MarketUpdated event: {e}", exc_info=True)

    async def _evaluate_pair(self, pair: MarketPair) -> None:
        """
        Evaluate all constraints for a market pair.

        Args:
            pair: MarketPair to evaluate
        """
        try:
            # Fetch current market data
            market_a = await self.db.get_market(pair.market_id_a)
            market_b = await self.db.get_market(pair.market_id_b)

            if not market_a or not market_b:
                logger.warning(
                    f"Could not fetch data for pair {pair.pair_id}: "
                    f"market_a={market_a is not None}, market_b={market_b is not None}"
                )
                return

            # Run through constraint checkers based on pair type
            violations = []
            checker = self._rule_checkers.get(pair.pair_type)

            if not checker:
                logger.warning(f"No checker found for pair type '{pair.pair_type}'")
                return

            violation_info = await checker(market_a, market_b, pair)
            if violation_info:
                violations.append(violation_info)

            # Record spread history
            await self._record_spread_history(pair, market_a, market_b, violations)

            # Emit violation events
            for violation_info in violations:
                await self._emit_violation_event(pair, violation_info)

        except Exception as e:
            logger.error(f"Error evaluating pair {pair.pair_id}: {e}", exc_info=True)

    async def _check_subset_superset(
        self, market_a: MarketData, market_b: MarketData, pair: MarketPair
    ) -> Optional[Any]:
        """Check subset/superset constraint."""
        violation_info = subset_superset.check(
            market_a.current_price,
            market_b.current_price,
            pair.relationship or "subset",
        )
        return violation_info

    async def _check_complementarity(
        self, market_a: MarketData, market_b: MarketData, pair: MarketPair
    ) -> Optional[Any]:
        """Check complementarity constraint for binary markets."""
        violation_info = complementarity.check(
            market_a.current_price,
            market_b.current_price,
            tolerance=self.config.complementarity_tolerance,
        )
        return violation_info

    async def _check_mutual_exclusivity(
        self, market_a: MarketData, market_b: MarketData, pair: MarketPair
    ) -> Optional[Any]:
        """
        Check mutual exclusivity constraint.

        Note: This is a simplified version. In production, you'd fetch
        all markets in the exhaustive set.
        """
        violation_info = mutual_exclusivity.check(
            [market_a.current_price, market_b.current_price]
        )
        return violation_info

    async def _check_cross_platform(
        self, market_a: MarketData, market_b: MarketData, pair: MarketPair
    ) -> Optional[Any]:
        """Check cross-platform spread constraint."""
        violation_info = cross_platform.check(
            market_a.current_price,
            market_b.current_price,
            market_a.platform,
            market_b.platform,
            min_net_spread_threshold=self.config.min_net_spread_cross_platform,
            fee_config=self.config.fee_config,
        )
        return violation_info

    async def _record_spread_history(
        self,
        pair: MarketPair,
        market_a: MarketData,
        market_b: MarketData,
        violations: List[Any],
    ) -> None:
        """
        Record spread history to database.

        Args:
            pair: Market pair
            market_a: Data for market A
            market_b: Data for market B
            violations: List of violations detected
        """
        try:
            raw_spread = abs(market_a.current_price - market_b.current_price)

            # Calculate net spread if cross-platform
            if pair.pair_type == "cross_platform":
                fee_a = self.fee_estimator.estimate_fee(
                    market_a.platform, "buy", market_a.current_price, 1.0
                )
                fee_b = self.fee_estimator.estimate_fee(
                    market_b.platform, "buy", market_b.current_price, 1.0
                )
                net_spread = raw_spread - (fee_a + fee_b)
            else:
                net_spread = raw_spread

            violation_types = [v.rule_type for v in violations]

            history_entry = SpreadHistoryEntry(
                pair_id=pair.pair_id,
                market_a_price=market_a.current_price,
                market_b_price=market_b.current_price,
                raw_spread=raw_spread,
                net_spread=net_spread,
                rule_violations=violation_types,
                recorded_at=datetime.utcnow(),
            )

            await self.db.insert_pair_spread_history(history_entry)

        except Exception as e:
            logger.error(
                f"Error recording spread history for pair {pair.pair_id}: {e}",
                exc_info=True,
            )

    async def _emit_violation_event(
        self, pair: MarketPair, violation_info: Any
    ) -> None:
        """
        Emit violation event and record in database.

        Args:
            pair: Market pair with violation
            violation_info: Violation information
        """
        try:
            now = datetime.utcnow()

            # Generate violation ID
            violation_id = (
                f"{pair.pair_id}_{violation_info.rule_type}_{int(now.timestamp())}"
            )

            # Check if this is a new violation
            is_new = violation_id not in self._active_violations
            if is_new:
                self._active_violations[violation_id] = now

            # Record in database
            await self.db.insert_violation(
                violation_id=violation_id,
                pair_id=pair.pair_id,
                market_id_a=pair.market_id_a,
                market_id_b=pair.market_id_b,
                rule_type=violation_info.rule_type,
                severity=violation_info.severity,
                description=violation_info.description,
                implied_arbitrage=violation_info.implied_arbitrage,
                detected_at=now,
                is_new=is_new,
            )

            # Emit event
            violation_event = ViolationEvent(
                violation_id=violation_id,
                pair_id=pair.pair_id,
                market_id_a=pair.market_id_a,
                market_id_b=pair.market_id_b,
                rule_type=violation_info.rule_type,
                severity=violation_info.severity,
                description=violation_info.description,
                implied_arbitrage=violation_info.implied_arbitrage,
                detected_at=now,
                is_new=is_new,
            )

            self.event_bus.publish("ConstraintViolationDetected", violation_event)

            if self.config.enable_logging and is_new:
                logger.warning(
                    f"New constraint violation detected: {violation_id} - "
                    f"{violation_info.rule_type} - {violation_info.severity}"
                )

        except Exception as e:
            logger.error(
                f"Error emitting violation event for pair {pair.pair_id}: {e}",
                exc_info=True,
            )

    async def _cleanup_stale_violations(self) -> None:
        """Remove stale violations from tracking."""
        now = datetime.utcnow()
        max_age = self.config.max_violation_age_seconds

        stale_ids = [
            vid
            for vid, detected_at in self._active_violations.items()
            if (now - detected_at).total_seconds() > max_age
        ]

        for vid in stale_ids:
            del self._active_violations[vid]

        if stale_ids:
            logger.debug(f"Cleaned up {len(stale_ids)} stale violations")
