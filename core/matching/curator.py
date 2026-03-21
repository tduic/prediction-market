"""CRUD interface for market pair management."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class MarketPair:
    """Market pair record."""

    pair_id: str
    market_id_a: str
    market_id_b: str
    pair_type: str
    relationship: str | None
    match_method: str  # "rules" or "embedding"
    similarity_score: float | None
    is_active: bool
    verified_by: str | None
    verified_at: datetime | None
    created_at: datetime
    created_by: str | None


class MarketPairCurator:
    """CRUD interface for market_pairs table."""

    def __init__(self, db: Any):
        """
        Initialize curator.

        Args:
            db: Database instance
        """
        self.db = db

    async def add_pair(
        self,
        market_id_a: str,
        market_id_b: str,
        pair_type: str,
        relationship: str | None = None,
        match_method: str = "rules",
        similarity_score: float | None = None,
        created_by: str | None = None,
    ) -> str:
        """
        Add a new market pair.

        Args:
            market_id_a: ID of first market
            market_id_b: ID of second market
            pair_type: Type of pair (e.g., "complement", "subset", "cross_platform")
            relationship: Optional relationship info (e.g., "subset", "superset")
            match_method: Method used to find pair ("rules" or "embedding")
            similarity_score: Similarity score if embedding-based match
            created_by: User/system that created the pair

        Returns:
            Pair ID of created pair

        Raises:
            ValueError: If markets are identical or already paired
            Exception: If database operation fails
        """
        if market_id_a == market_id_b:
            raise ValueError("Cannot pair a market with itself")

        # Check if pair already exists (in either direction)
        existing = await self.get_pair(market_id_a, market_id_b)
        if existing:
            raise ValueError(f"Pair already exists: {existing.pair_id}")

        try:
            pair_id = await self.db.insert_market_pair(
                market_id_a=market_id_a,
                market_id_b=market_id_b,
                pair_type=pair_type,
                relationship=relationship,
                match_method=match_method,
                similarity_score=similarity_score,
                is_active=True,
                created_by=created_by,
                created_at=datetime.now(timezone.utc),
            )

            logger.info(
                f"Added market pair: {pair_id} "
                f"({market_id_a} <-> {market_id_b}) "
                f"type={pair_type} method={match_method}"
            )

            return pair_id

        except Exception as e:
            logger.error(f"Error adding market pair: {e}")
            raise

    async def get_pair(self, market_id_a: str, market_id_b: str) -> MarketPair | None:
        """
        Get pair for two markets (works in either direction).

        Args:
            market_id_a: ID of first market
            market_id_b: ID of second market

        Returns:
            MarketPair if exists, None otherwise
        """
        try:
            # Try both directions
            pair = await self.db.get_market_pair(market_id_a, market_id_b)
            if pair:
                return pair

            pair = await self.db.get_market_pair(market_id_b, market_id_a)
            return pair

        except Exception as e:
            logger.error(f"Error getting pair: {e}")
            return None

    async def get_pair_by_id(self, pair_id: str) -> MarketPair | None:
        """
        Get pair by ID.

        Args:
            pair_id: Pair ID

        Returns:
            MarketPair if exists, None otherwise
        """
        try:
            return await self.db.get_market_pair_by_id(pair_id)
        except Exception as e:
            logger.error(f"Error getting pair by ID {pair_id}: {e}")
            return None

    async def get_active_pairs(self, pair_type: str | None = None) -> list[MarketPair]:
        """
        Get all active market pairs.

        Args:
            pair_type: Optional filter by pair type

        Returns:
            List of active MarketPair objects
        """
        try:
            pairs = await self.db.get_active_market_pairs(pair_type)
            return pairs or []

        except Exception as e:
            logger.error(f"Error getting active pairs: {e}")
            return []

    async def get_pairs_for_market(self, market_id: str) -> list[MarketPair]:
        """
        Get all pairs for a specific market.

        Args:
            market_id: Market ID

        Returns:
            List of MarketPair objects containing the market
        """
        try:
            pairs = await self.db.get_market_pairs_by_market_id(market_id)
            return pairs or []

        except Exception as e:
            logger.error(f"Error getting pairs for market {market_id}: {e}")
            return []

    async def get_pairs_by_type(self, pair_type: str) -> list[MarketPair]:
        """
        Get all pairs of a specific type.

        Args:
            pair_type: Type to filter by

        Returns:
            List of MarketPair objects
        """
        try:
            pairs = await self.db.get_market_pairs_by_type(pair_type)
            return pairs or []

        except Exception as e:
            logger.error(f"Error getting pairs of type {pair_type}: {e}")
            return []

    async def verify_pair(self, pair_id: str, verified_by: str) -> bool:
        """
        Mark a pair as verified (human-reviewed and confirmed).

        Args:
            pair_id: Pair ID to verify
            verified_by: User/system verifying the pair

        Returns:
            True if verified successfully, False otherwise
        """
        try:
            await self.db.verify_market_pair(
                pair_id=pair_id,
                verified_by=verified_by,
                verified_at=datetime.now(timezone.utc),
            )

            logger.info(f"Verified market pair {pair_id} by {verified_by}")
            return True

        except Exception as e:
            logger.error(f"Error verifying pair {pair_id}: {e}")
            return False

    async def deactivate_pair(self, pair_id: str) -> bool:
        """
        Deactivate a market pair.

        Args:
            pair_id: Pair ID to deactivate

        Returns:
            True if deactivated successfully, False otherwise
        """
        try:
            await self.db.deactivate_market_pair(pair_id)
            logger.info(f"Deactivated market pair {pair_id}")
            return True

        except Exception as e:
            logger.error(f"Error deactivating pair {pair_id}: {e}")
            return False

    async def reactivate_pair(self, pair_id: str) -> bool:
        """
        Reactivate a deactivated market pair.

        Args:
            pair_id: Pair ID to reactivate

        Returns:
            True if reactivated successfully, False otherwise
        """
        try:
            await self.db.reactivate_market_pair(pair_id)
            logger.info(f"Reactivated market pair {pair_id}")
            return True

        except Exception as e:
            logger.error(f"Error reactivating pair {pair_id}: {e}")
            return False

    async def update_pair(
        self,
        pair_id: str,
        pair_type: str | None = None,
        relationship: str | None = None,
        similarity_score: float | None = None,
    ) -> bool:
        """
        Update a market pair.

        Args:
            pair_id: Pair ID
            pair_type: New pair type (if provided)
            relationship: New relationship (if provided)
            similarity_score: New similarity score (if provided)

        Returns:
            True if updated successfully, False otherwise
        """
        try:
            updates = {}

            if pair_type is not None:
                updates["pair_type"] = pair_type
            if relationship is not None:
                updates["relationship"] = relationship
            if similarity_score is not None:
                updates["similarity_score"] = similarity_score

            if not updates:
                logger.warning(f"No updates provided for pair {pair_id}")
                return True

            await self.db.update_market_pair(pair_id, **updates)

            logger.info(f"Updated market pair {pair_id}: {updates}")
            return True

        except Exception as e:
            logger.error(f"Error updating pair {pair_id}: {e}")
            return False

    async def get_unverified_pairs(
        self, pair_type: str | None = None
    ) -> list[MarketPair]:
        """
        Get all unverified pairs (for human review).

        Args:
            pair_type: Optional filter by pair type

        Returns:
            List of unverified MarketPair objects
        """
        try:
            pairs = await self.db.get_unverified_market_pairs(pair_type)
            return pairs or []

        except Exception as e:
            logger.error(f"Error getting unverified pairs: {e}")
            return []

    async def get_verified_pairs(
        self, pair_type: str | None = None
    ) -> list[MarketPair]:
        """
        Get all verified pairs.

        Args:
            pair_type: Optional filter by pair type

        Returns:
            List of verified MarketPair objects
        """
        try:
            pairs = await self.db.get_verified_market_pairs(pair_type)
            return pairs or []

        except Exception as e:
            logger.error(f"Error getting verified pairs: {e}")
            return []

    async def get_pair_stats(self) -> dict:
        """
        Get statistics about market pairs.

        Returns:
            Dictionary with pair statistics
        """
        try:
            stats = {
                "total_pairs": 0,
                "active_pairs": 0,
                "inactive_pairs": 0,
                "verified_pairs": 0,
                "unverified_pairs": 0,
                "by_type": {},
                "by_match_method": {},
            }

            all_pairs = await self.db.get_all_market_pairs()

            if not all_pairs:
                return stats

            stats["total_pairs"] = len(all_pairs)

            for pair in all_pairs:
                if pair.is_active:
                    stats["active_pairs"] += 1
                else:
                    stats["inactive_pairs"] += 1

                if pair.verified_at is not None:
                    stats["verified_pairs"] += 1
                else:
                    stats["unverified_pairs"] += 1

                # Count by type
                ptype = pair.pair_type
                if ptype not in stats["by_type"]:
                    stats["by_type"][ptype] = 0
                stats["by_type"][ptype] += 1

                # Count by method
                method = pair.match_method
                if method not in stats["by_match_method"]:
                    stats["by_match_method"][method] = 0
                stats["by_match_method"][method] += 1

            return stats

        except Exception as e:
            logger.error(f"Error getting pair stats: {e}")
            return {}
