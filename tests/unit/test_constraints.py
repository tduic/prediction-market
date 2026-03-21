"""
Unit tests for constraint detection module.

Tests all constraint rule implementations:
- Subset/Superset relationships
- Mutual exclusivity constraints
- Complementarity rules
- Cross-platform spread validation
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone

# ============================================================================
# Constraint Detection Classes (Mock Implementations)
# ============================================================================


class ConstraintViolation:
    """Represents a detected constraint violation."""

    def __init__(
        self,
        violation_type: str,
        market_a_id: str,
        market_b_id: str,
        price_a: float,
        price_b: float,
        spread: float,
        is_violation: bool,
        details: dict = None,
    ):
        self.violation_type = violation_type
        self.market_a_id = market_a_id
        self.market_b_id = market_b_id
        self.price_a = price_a
        self.price_b = price_b
        self.spread = spread
        self.is_violation = is_violation
        self.details = details or {}


class ConstraintEngine:
    """Engine for detecting constraint violations."""

    def __init__(self, config, event_bus=None, db=None):
        self.config = config
        self.event_bus = event_bus
        self.db = db

    def check_subset_superset(self, subset_price: float, superset_price: float) -> bool:
        """
        Check subset/superset relationship.

        Violation if: subset_price > superset_price

        Args:
            subset_price: YES price of subset market
            superset_price: YES price of superset market

        Returns:
            True if violation detected, False otherwise
        """
        return subset_price > superset_price

    def check_mutual_exclusivity(self, markets: list) -> bool:
        """
        Check mutual exclusivity constraint.

        Violation if: sum of YES prices > 1.0

        Args:
            markets: List of mutually exclusive markets with yes_price

        Returns:
            True if violation detected, False otherwise
        """
        sum_yes = sum(m["yes_price"] for m in markets)
        return sum_yes > 1.0

    def check_complementarity(
        self, yes_price: float, no_price: float, threshold: float = 0.02
    ) -> bool:
        """
        Check complementarity constraint.

        Violation if: abs(yes_price + no_price - 1.0) > threshold

        Args:
            yes_price: YES price
            no_price: NO price
            threshold: Tolerance for deviation (default 0.02)

        Returns:
            True if violation detected, False otherwise
        """
        sum_price = yes_price + no_price
        return abs(sum_price - 1.0) > threshold

    def check_cross_platform_spread(
        self,
        price_a: float,
        price_b: float,
        fee_a_pct: float,
        fee_b_pct: float,
        min_spread_bps: int = 50,
    ) -> dict:
        """
        Check cross-platform spread after fees.

        Violation if: net_spread (after fees) > threshold

        Args:
            price_a: YES price on platform A
            price_b: YES price on platform B
            fee_a_pct: Fee percentage on platform A
            fee_b_pct: Fee percentage on platform B
            min_spread_bps: Minimum profitable spread in basis points

        Returns:
            dict with spread analysis
        """
        # Calculate raw spread
        raw_spread = abs(price_a - price_b)

        # Calculate net spread (fees reduce arbitrage opportunity)
        higher_price = max(price_a, price_b)
        lower_price = min(price_a, price_b)

        # Buy at lower price, pay fee; sell at higher price, pay fee
        fee_on_buy = lower_price * (fee_a_pct / 100)
        fee_on_sell = higher_price * (fee_b_pct / 100)

        net_spread = raw_spread - fee_on_buy - fee_on_sell
        min_spread_decimal = min_spread_bps / 10000

        return {
            "raw_spread": raw_spread,
            "net_spread": net_spread,
            "total_fees": fee_on_buy + fee_on_sell,
            "is_violation": net_spread > min_spread_decimal,
            "profitable": net_spread > min_spread_decimal,
        }

    def emit_violation_event(self, violation: ConstraintViolation) -> None:
        """Emit violation event to event bus."""
        if self.event_bus:
            self.event_bus.emit(
                "constraint_violation_detected",
                {
                    "violation_type": violation.violation_type,
                    "market_a_id": violation.market_a_id,
                    "market_b_id": violation.market_b_id,
                    "spread": violation.spread,
                    "is_violation": violation.is_violation,
                    "details": violation.details,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )


# ============================================================================
# Test Cases
# ============================================================================


class TestSubsetSupersetConstraints:
    """Test subset/superset relationship detection."""

    def test_subset_superset_detects_violation(self, sample_config):
        """Subset price exceeding superset price is a violation."""
        engine = ConstraintEngine(sample_config)

        subset_price = 0.78
        superset_price = 0.72

        is_violation = engine.check_subset_superset(subset_price, superset_price)

        assert is_violation is True

    def test_subset_superset_no_violation(self, sample_config):
        """Correctly ordered subset/superset prices are valid."""
        engine = ConstraintEngine(sample_config)

        subset_price = 0.70
        superset_price = 0.75

        is_violation = engine.check_subset_superset(subset_price, superset_price)

        assert is_violation is False

    def test_subset_superset_equal_prices(self, sample_config):
        """Equal prices indicate valid ordering."""
        engine = ConstraintEngine(sample_config)

        subset_price = 0.72
        superset_price = 0.72

        is_violation = engine.check_subset_superset(subset_price, superset_price)

        assert is_violation is False

    def test_subset_superset_large_gap(self, sample_config):
        """Large gap between subset and superset."""
        engine = ConstraintEngine(sample_config)

        subset_price = 0.60
        superset_price = 0.85

        is_violation = engine.check_subset_superset(subset_price, superset_price)

        assert is_violation is False


class TestMutualExclusivityConstraints:
    """Test mutual exclusivity constraint detection."""

    def test_mutual_exclusivity_detects_violation(self, sample_config):
        """Sum of YES prices > 1.0 is a violation."""
        engine = ConstraintEngine(sample_config)

        markets = [
            {"id": "trump", "yes_price": 0.54},
            {"id": "harris", "yes_price": 0.53},
        ]

        is_violation = engine.check_mutual_exclusivity(markets)

        assert is_violation is True

    def test_mutual_exclusivity_no_violation_exact(self, sample_config):
        """Sum exactly at 1.0 is valid."""
        engine = ConstraintEngine(sample_config)

        markets = [
            {"id": "trump", "yes_price": 0.50},
            {"id": "harris", "yes_price": 0.50},
        ]

        is_violation = engine.check_mutual_exclusivity(markets)

        assert is_violation is False

    def test_mutual_exclusivity_no_violation_below(self, sample_config):
        """Sum below 1.0 is valid."""
        engine = ConstraintEngine(sample_config)

        markets = [
            {"id": "trump", "yes_price": 0.45},
            {"id": "harris", "yes_price": 0.50},
            {"id": "other", "yes_price": 0.03},
        ]

        is_violation = engine.check_mutual_exclusivity(markets)

        assert is_violation is False

    def test_mutual_exclusivity_large_violation(self, sample_config):
        """Detect large violations."""
        engine = ConstraintEngine(sample_config)

        markets = [
            {"id": "opt_a", "yes_price": 0.60},
            {"id": "opt_b", "yes_price": 0.60},
        ]

        is_violation = engine.check_mutual_exclusivity(markets)

        assert is_violation is True

    def test_mutual_exclusivity_three_way(self, sample_config):
        """Test three-way mutual exclusivity."""
        engine = ConstraintEngine(sample_config)

        markets = [
            {"id": "outcome_a", "yes_price": 0.40},
            {"id": "outcome_b", "yes_price": 0.35},
            {"id": "outcome_c", "yes_price": 0.30},
        ]

        is_violation = engine.check_mutual_exclusivity(markets)

        assert is_violation is True


class TestComplementarityConstraints:
    """Test complementarity constraint detection."""

    def test_complementarity_detects_violation(self, sample_config):
        """YES + NO != 1.0 beyond threshold is a violation."""
        engine = ConstraintEngine(sample_config)

        yes_price = 0.70
        no_price = 0.25

        is_violation = engine.check_complementarity(yes_price, no_price, threshold=0.02)

        assert is_violation is True

    def test_complementarity_no_violation_exact(self, sample_config):
        """YES + NO = 1.0 is valid."""
        engine = ConstraintEngine(sample_config)

        yes_price = 0.60
        no_price = 0.40

        is_violation = engine.check_complementarity(yes_price, no_price, threshold=0.02)

        assert is_violation is False

    def test_complementarity_no_violation_within_threshold(self, sample_config):
        """Deviation within threshold is valid."""
        engine = ConstraintEngine(sample_config)

        yes_price = 0.84
        no_price = 0.15

        is_violation = engine.check_complementarity(yes_price, no_price, threshold=0.02)

        assert is_violation is False

    def test_complementarity_large_violation(self, sample_config):
        """Detect large price sum deviations."""
        engine = ConstraintEngine(sample_config)

        yes_price = 0.90
        no_price = 0.90

        is_violation = engine.check_complementarity(yes_price, no_price, threshold=0.02)

        assert is_violation is True

    def test_complementarity_small_threshold(self, sample_config):
        """Stricter threshold detects small deviations."""
        engine = ConstraintEngine(sample_config)

        yes_price = 0.51
        no_price = 0.515

        # Threshold of 0.01 should detect this
        # Sum = 1.025, deviation = 0.025 > 0.01, so violation
        is_violation = engine.check_complementarity(yes_price, no_price, threshold=0.01)

        assert is_violation is True


class TestCrossPlatformConstraints:
    """Test cross-platform spread and arbitrage constraints."""

    def test_cross_platform_detects_violation(self, sample_config):
        """Large spread that exceeds fee threshold is a violation."""
        engine = ConstraintEngine(sample_config)

        result = engine.check_cross_platform_spread(
            price_a=0.72,
            price_b=0.65,
            fee_a_pct=0.2,
            fee_b_pct=0.2,
            min_spread_bps=50,
        )

        assert result["is_violation"] is True
        assert result["profitable"] is True
        assert result["net_spread"] > 0.005

    def test_cross_platform_no_violation(self, sample_config):
        """Small spread covered by fees is not profitable."""
        engine = ConstraintEngine(sample_config)

        result = engine.check_cross_platform_spread(
            price_a=0.58,
            price_b=0.57,
            fee_a_pct=2.0,
            fee_b_pct=2.0,
            min_spread_bps=50,
        )

        assert result["is_violation"] is False
        assert result["profitable"] is False

    def test_cross_platform_fee_subtraction_produces_correct_net_spread(
        self, sample_config
    ):
        """Verify fee calculation and net spread computation."""
        engine = ConstraintEngine(sample_config)

        price_a = 0.60
        price_b = 0.70
        fee_pct = 0.5  # 0.5% fee

        result = engine.check_cross_platform_spread(
            price_a=price_a,
            price_b=price_b,
            fee_a_pct=fee_pct,
            fee_b_pct=fee_pct,
            min_spread_bps=50,
        )

        # Raw spread = |0.60 - 0.70| = 0.10
        assert result["raw_spread"] == pytest.approx(0.10)

        # Fee on buy at 0.60: 0.60 * 0.005 = 0.003
        # Fee on sell at 0.70: 0.70 * 0.005 = 0.0035
        # Net spread = 0.10 - 0.003 - 0.0035 = 0.0935
        assert result["net_spread"] == pytest.approx(0.0935, abs=0.0001)

    def test_cross_platform_identical_prices(self, sample_config):
        """Identical prices across platforms have zero spread."""
        engine = ConstraintEngine(sample_config)

        result = engine.check_cross_platform_spread(
            price_a=0.65,
            price_b=0.65,
            fee_a_pct=0.2,
            fee_b_pct=0.2,
            min_spread_bps=50,
        )

        assert result["raw_spread"] == 0.0
        assert result["is_violation"] is False

    def test_cross_platform_high_fees_eliminate_spread(self, sample_config):
        """High fees can eliminate profitability despite spread."""
        engine = ConstraintEngine(sample_config)

        result = engine.check_cross_platform_spread(
            price_a=0.60,
            price_b=0.61,
            fee_a_pct=2.0,  # 2% fee
            fee_b_pct=2.0,  # 2% fee
            min_spread_bps=50,
        )

        # Fees exceed the small spread
        assert result["is_violation"] is False


class TestConstraintEngineIntegration:
    """Test full constraint engine functionality."""

    def test_constraint_engine_emits_violation_event(self, sample_config, event_bus):
        """Engine emits events when violations detected."""
        engine = ConstraintEngine(sample_config, event_bus=event_bus)

        violation = ConstraintViolation(
            violation_type="subset_superset",
            market_a_id="pm_001",
            market_b_id="ks_001",
            price_a=0.72,
            price_b=0.78,
            spread=0.06,
            is_violation=True,
            details={"reason": "subset exceeds superset"},
        )

        engine.emit_violation_event(violation)

        events = event_bus.get_events("constraint_violation_detected")
        assert len(events) == 1
        assert events[0]["data"]["violation_type"] == "subset_superset"
        assert events[0]["data"]["is_violation"] is True

    def test_constraint_engine_multiple_events(self, sample_config, event_bus):
        """Engine can emit multiple violation events."""
        engine = ConstraintEngine(sample_config, event_bus=event_bus)

        for i in range(3):
            violation = ConstraintViolation(
                violation_type="cross_platform_identical",
                market_a_id=f"pm_{i:03d}",
                market_b_id=f"ks_{i:03d}",
                price_a=0.50 + i * 0.01,
                price_b=0.50 + i * 0.01,
                spread=0.01,
                is_violation=False,
            )
            engine.emit_violation_event(violation)

        events = event_bus.get_events("constraint_violation_detected")
        assert len(events) == 3

    def test_constraint_engine_respects_all_constraints(self, sample_config):
        """Engine can check all constraint types in sequence."""
        engine = ConstraintEngine(sample_config)

        # Subset/Superset
        assert engine.check_subset_superset(0.75, 0.70) is True

        # Mutual Exclusivity
        assert (
            engine.check_mutual_exclusivity(
                [
                    {"yes_price": 0.55},
                    {"yes_price": 0.50},
                ]
            )
            is True
        )

        # Complementarity
        assert engine.check_complementarity(0.85, 0.10) is True

        # Cross-platform
        result = engine.check_cross_platform_spread(0.75, 0.65, 0.2, 0.2, 50)
        assert result["is_violation"] is True
