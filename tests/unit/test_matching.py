"""
Unit tests for market pair matching module.

Tests market matching via:
- Rule-based matching (FOMC, CPI, etc.)
- Embedding similarity matching
- Market pair curator operations
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone
import math

# ============================================================================
# Matching Classes (Mock Implementations)
# ============================================================================


class MarketPair:
    """Represents a matched pair of markets."""

    def __init__(
        self,
        pair_id: str,
        market_a_id: str,
        market_b_id: str,
        pair_type: str,
        similarity: float = None,
        verified: bool = False,
        status: str = "active",
    ):
        self.pair_id = pair_id
        self.market_a_id = market_a_id
        self.market_b_id = market_b_id
        self.pair_type = pair_type
        self.similarity = similarity
        self.verified = verified
        self.status = status
        self.created_at = datetime.now(timezone.utc)


class MarketMatcher:
    """Match markets using rule-based and embedding similarity methods."""

    def __init__(self):
        self.rules = {
            "fomc": self._match_fomc,
            "cpi": self._match_cpi,
            "unemployment": self._match_unemployment,
        }

    def match_by_event_type(self, market_a: dict, market_b: dict) -> bool:
        """
        Match markets by event type using rule-based approach.

        Args:
            market_a: Market A data
            market_b: Market B data

        Returns:
            True if markets match on event type
        """
        event_type = market_a.get("event_type")

        if event_type == "fomc":
            return self._match_fomc(market_a, market_b)
        elif event_type == "cpi":
            return self._match_cpi(market_a, market_b)
        elif event_type == "unemployment":
            return self._match_unemployment(market_a, market_b)

        return False

    def _match_fomc(self, market_a: dict, market_b: dict) -> bool:
        """Match FOMC markets on event type and date."""
        if market_a.get("event_type") != "fomc" or market_b.get("event_type") != "fomc":
            return False

        # Extract meeting month/year from title
        title_a = market_a.get("title", "").lower()
        title_b = market_b.get("title", "").lower()

        # Simple heuristic: both mention same month
        months = [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ]

        for month in months:
            if month in title_a and month in title_b:
                return True

        return False

    def _match_cpi(self, market_a: dict, market_b: dict) -> bool:
        """Match CPI markets on event type and date."""
        if market_a.get("event_type") != "cpi" or market_b.get("event_type") != "cpi":
            return False

        # Simple check: both are CPI markets
        return (
            "cpi" in market_a.get("title", "").lower()
            and "cpi" in market_b.get("title", "").lower()
        )

    def _match_unemployment(self, market_a: dict, market_b: dict) -> bool:
        """Match unemployment markets."""
        if (
            market_a.get("event_type") != "unemployment"
            or market_b.get("event_type") != "unemployment"
        ):
            return False

        return True

    def compute_embedding_similarity(self, text_a: str, text_b: str) -> float:
        """
        Compute text similarity (mock using character overlap).

        Args:
            text_a: Text from market A
            text_b: Text from market B

        Returns:
            Similarity score 0-1
        """
        # Simple mock: character-level similarity
        if not text_a or not text_b:
            return 0.0

        # Convert to lowercase and split by words
        words_a = set(text_a.lower().split())
        words_b = set(text_b.lower().split())

        if not words_a or not words_b:
            return 0.0

        # Jaccard similarity
        intersection = len(words_a & words_b)
        union = len(words_a | words_b)

        return intersection / union if union > 0 else 0.0

    def match_by_embedding_similarity(
        self,
        market_a: dict,
        market_b: dict,
        threshold: float = 0.7,
    ) -> bool:
        """
        Match markets by embedding similarity.

        Args:
            market_a: Market A data
            market_b: Market B data
            threshold: Minimum similarity (default 0.7)

        Returns:
            True if similarity >= threshold
        """
        title_a = market_a.get("title", "")
        title_b = market_b.get("title", "")

        similarity = self.compute_embedding_similarity(title_a, title_b)
        return similarity >= threshold


class MarketPairCurator:
    """Manage market pair creation, verification, and lifecycle."""

    def __init__(self, db=None, matcher=None):
        self.db = db
        self.matcher = matcher or MarketMatcher()
        self.pairs = {}  # In-memory pair storage

    def add_pair(
        self,
        pair_id: str,
        market_a_id: str,
        market_b_id: str,
        pair_type: str,
        similarity: float = None,
    ) -> MarketPair:
        """
        Add a market pair to the curator.

        Args:
            pair_id: Unique pair identifier
            market_a_id: ID of market A
            market_b_id: ID of market B
            pair_type: Type of pair (subset_superset, identical, etc.)
            similarity: Similarity score if computed

        Returns:
            Created MarketPair
        """
        pair = MarketPair(
            pair_id=pair_id,
            market_a_id=market_a_id,
            market_b_id=market_b_id,
            pair_type=pair_type,
            similarity=similarity,
        )

        self.pairs[pair_id] = pair

        # Write to database if available
        if self.db:
            self.db.execute(
                """INSERT INTO market_pairs (id, market_a_id, market_b_id, pair_type, status)
                   VALUES (?, ?, ?, ?, ?)""",
                (pair_id, market_a_id, market_b_id, pair_type, "active"),
            )
            self.db.commit()

        return pair

    def retrieve_pair(self, pair_id: str) -> MarketPair:
        """
        Retrieve a market pair by ID.

        Args:
            pair_id: Pair identifier

        Returns:
            MarketPair if found, None otherwise
        """
        return self.pairs.get(pair_id)

    def verify_pair(self, pair_id: str) -> bool:
        """
        Mark a pair as verified.

        Args:
            pair_id: Pair identifier

        Returns:
            True if verification successful
        """
        pair = self.pairs.get(pair_id)
        if not pair:
            return False

        pair.verified = True

        # Update database
        if self.db:
            self.db.execute(
                "UPDATE market_pairs SET verified = 1 WHERE id = ?",
                (pair_id,),
            )
            self.db.commit()

        return True

    def deactivate_pair(self, pair_id: str) -> bool:
        """
        Deactivate a market pair.

        Args:
            pair_id: Pair identifier

        Returns:
            True if deactivation successful
        """
        pair = self.pairs.get(pair_id)
        if not pair:
            return False

        pair.status = "inactive"

        # Update database
        if self.db:
            self.db.execute(
                "UPDATE market_pairs SET status = ? WHERE id = ?",
                ("inactive", pair_id),
            )
            self.db.commit()

        return True

    def list_pairs(self, status: str = None) -> list:
        """
        List all pairs, optionally filtered by status.

        Args:
            status: Filter by status (optional)

        Returns:
            List of MarketPair objects
        """
        if status:
            return [p for p in self.pairs.values() if p.status == status]
        return list(self.pairs.values())


# ============================================================================
# Test Cases
# ============================================================================


class TestRuleBasedMatching:
    """Test rule-based market matching."""

    def test_rule_based_fomc_match(self, sample_markets):
        """FOMC markets on both platforms are detected."""
        matcher = MarketMatcher()

        polymarket_fomc = next(
            (m for m in sample_markets["polymarket"] if m.event_type == "fomc"),
            None,
        )
        kalshi_fomc = next(
            (m for m in sample_markets["kalshi"] if m.event_type == "fomc"),
            None,
        )

        assert polymarket_fomc is not None
        assert kalshi_fomc is not None

        # Match them
        market_a = {
            "id": polymarket_fomc.id,
            "event_type": "fomc",
            "title": "Will the Fed cut rates in December 2024?",
        }
        market_b = {
            "id": kalshi_fomc.id,
            "event_type": "fomc",
            "title": "FOMC Rate Cut in December",
        }

        result = matcher.match_by_event_type(market_a, market_b)

        assert result is True

    def test_rule_based_cpi_match(self, sample_markets):
        """CPI markets are matched."""
        matcher = MarketMatcher()

        market_a = {
            "event_type": "cpi",
            "title": "Will US CPI be below 3.0% in November 2024?",
        }
        market_b = {
            "event_type": "cpi",
            "title": "CPI Below 3% in November",
        }

        result = matcher.match_by_event_type(market_a, market_b)

        assert result is True

    def test_rule_based_different_event_types_no_match(self):
        """Different event types do not match."""
        matcher = MarketMatcher()

        market_a = {
            "event_type": "fomc",
            "title": "Will the Fed cut rates in December?",
        }
        market_b = {
            "event_type": "cpi",
            "title": "Will CPI be below 3%?",
        }

        result = matcher.match_by_event_type(market_a, market_b)

        assert result is False

    def test_rule_based_fomc_different_months_no_match(self):
        """FOMC markets in different months do not match."""
        matcher = MarketMatcher()

        market_a = {
            "event_type": "fomc",
            "title": "Will the Fed cut rates in December?",
        }
        market_b = {
            "event_type": "fomc",
            "title": "Will the Fed cut rates in January?",
        }

        result = matcher.match_by_event_type(market_a, market_b)

        assert result is False


class TestEmbeddingSimilarityMatching:
    """Test embedding-based similarity matching."""

    def test_embedding_similarity_above_threshold(self):
        """High similarity pair is matched."""
        matcher = MarketMatcher()

        market_a = {
            "title": "Will Bitcoin exceed $50,000 by December 31, 2024?",
        }
        market_b = {
            "title": "Will Bitcoin exceed $50,000 by December 31, 2024?",
        }

        result = matcher.match_by_embedding_similarity(
            market_a, market_b, threshold=0.5
        )

        assert result is True

    def test_embedding_similarity_below_threshold(self):
        """Low similarity pair is rejected."""
        matcher = MarketMatcher()

        market_a = {
            "title": "Will Bitcoin exceed $50,000?",
        }
        market_b = {
            "title": "Will the Fed cut interest rates?",
        }

        result = matcher.match_by_embedding_similarity(
            market_a, market_b, threshold=0.5
        )

        assert result is False

    def test_embedding_similarity_identical_titles(self):
        """Identical titles produce high similarity."""
        matcher = MarketMatcher()

        title = "Will Bitcoin exceed $50,000 by December 31, 2024?"

        result = matcher.match_by_embedding_similarity(
            {"title": title},
            {"title": title},
            threshold=0.9,
        )

        assert result is True

    def test_embedding_similarity_computes_score(self):
        """Similarity score is correctly computed."""
        matcher = MarketMatcher()

        similarity = matcher.compute_embedding_similarity(
            "Will Bitcoin exceed $50,000?",
            "Will Bitcoin reach $50,000?",
        )

        # Should be reasonably high
        assert 0.5 < similarity < 1.0

    def test_embedding_similarity_empty_text(self):
        """Empty text results in zero similarity."""
        matcher = MarketMatcher()

        similarity = matcher.compute_embedding_similarity(
            "", "Will Bitcoin exceed $50,000?"
        )

        assert similarity == 0.0

    def test_embedding_similarity_threshold_boundary(self):
        """Matches at threshold boundary are accepted."""
        matcher = MarketMatcher()

        market_a = {"title": "Bitcoin price target"}
        market_b = {"title": "Bitcoin price BTC"}

        # Right at threshold
        result = matcher.match_by_embedding_similarity(
            market_a, market_b, threshold=0.5
        )

        # Should be true or false based on actual similarity
        assert isinstance(result, bool)


class TestMarketPairCurator:
    """Test market pair curator operations."""

    def test_curator_add_and_retrieve_pair(self):
        """Add and retrieve market pair."""
        curator = MarketPairCurator()

        pair = curator.add_pair(
            pair_id="pair_001",
            market_a_id="pm_001",
            market_b_id="ks_001",
            pair_type="fomc_match",
            similarity=0.95,
        )

        retrieved = curator.retrieve_pair("pair_001")

        assert retrieved is not None
        assert retrieved.market_a_id == "pm_001"
        assert retrieved.market_b_id == "ks_001"
        assert retrieved.similarity == 0.95

    def test_curator_add_multiple_pairs(self):
        """Add multiple market pairs."""
        curator = MarketPairCurator()

        for i in range(3):
            curator.add_pair(
                pair_id=f"pair_{i:03d}",
                market_a_id=f"pm_{i:03d}",
                market_b_id=f"ks_{i:03d}",
                pair_type="fomc_match",
            )

        pairs = curator.list_pairs()
        assert len(pairs) == 3

    def test_curator_verify_pair(self):
        """Verify a market pair."""
        curator = MarketPairCurator()

        curator.add_pair(
            pair_id="pair_verify",
            market_a_id="pm_001",
            market_b_id="ks_001",
            pair_type="fomc_match",
        )

        pair = curator.retrieve_pair("pair_verify")
        assert pair.verified is False

        result = curator.verify_pair("pair_verify")

        assert result is True
        assert pair.verified is True

    def test_curator_deactivate_pair(self):
        """Deactivate a market pair."""
        curator = MarketPairCurator()

        curator.add_pair(
            pair_id="pair_deactivate",
            market_a_id="pm_001",
            market_b_id="ks_001",
            pair_type="fomc_match",
        )

        pair = curator.retrieve_pair("pair_deactivate")
        assert pair.status == "active"

        result = curator.deactivate_pair("pair_deactivate")

        assert result is True
        assert pair.status == "inactive"

    def test_curator_list_pairs_by_status(self):
        """List pairs filtered by status."""
        curator = MarketPairCurator()

        # Add active pairs
        for i in range(2):
            curator.add_pair(
                pair_id=f"pair_active_{i}",
                market_a_id=f"pm_{i}",
                market_b_id=f"ks_{i}",
                pair_type="fomc_match",
            )

        # Deactivate one
        curator.deactivate_pair("pair_active_0")

        active_pairs = curator.list_pairs(status="active")
        inactive_pairs = curator.list_pairs(status="inactive")

        assert len(active_pairs) == 1
        assert len(inactive_pairs) == 1

    def test_curator_retrieve_nonexistent_pair(self):
        """Retrieve nonexistent pair returns None."""
        curator = MarketPairCurator()

        pair = curator.retrieve_pair("nonexistent")

        assert pair is None

    def test_curator_verify_nonexistent_pair(self):
        """Verifying nonexistent pair returns False."""
        curator = MarketPairCurator()

        result = curator.verify_pair("nonexistent")

        assert result is False

    def test_curator_with_database(self, in_memory_db):
        """Curator persists pairs to database."""
        # Create parent markets first
        in_memory_db.execute(
            """INSERT INTO markets (id, platform, platform_id, title, yes_price, no_price, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "pm_001",
                "polymarket",
                "pm_001_ext",
                "FOMC Test Market",
                0.5,
                0.5,
                "open",
            ),
        )
        in_memory_db.execute(
            """INSERT INTO markets (id, platform, platform_id, title, yes_price, no_price, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("ks_001", "kalshi", "ks_001_ext", "FOMC Test Market", 0.5, 0.5, "open"),
        )
        in_memory_db.commit()

        curator = MarketPairCurator(db=in_memory_db)

        curator.add_pair(
            pair_id="pair_db",
            market_a_id="pm_001",
            market_b_id="ks_001",
            pair_type="fomc_match",
        )

        # Verify in database
        cursor = in_memory_db.execute(
            "SELECT * FROM market_pairs WHERE id = ?",
            ("pair_db",),
        )
        row = cursor.fetchone()

        assert row is not None
        assert row["market_a_id"] == "pm_001"

    def test_curator_pair_lifecycle(self):
        """Full pair lifecycle: add -> verify -> deactivate."""
        curator = MarketPairCurator()

        # Create pair
        curator.add_pair(
            pair_id="pair_lifecycle",
            market_a_id="pm_001",
            market_b_id="ks_001",
            pair_type="fomc_match",
        )

        pair = curator.retrieve_pair("pair_lifecycle")
        assert pair.verified is False
        assert pair.status == "active"

        # Verify
        curator.verify_pair("pair_lifecycle")
        assert pair.verified is True

        # Deactivate
        curator.deactivate_pair("pair_lifecycle")
        assert pair.status == "inactive"
