"""
Unit tests for Kelly criterion position sizing.

Tests sizing calculations with various inputs and edge cases.
"""

import pytest
from decimal import Decimal


# ============================================================================
# Kelly Sizing Implementation (Mock)
# ============================================================================

class PositionSizer:
    """Compute position sizes using Kelly criterion with fractional Kelly."""

    def __init__(self, config):
        self.config = config

    def kelly_fraction(self, edge: float, odds: float = 1.0) -> float:
        """
        Compute Kelly fraction given edge and odds.

        Kelly formula: f = (p*b - q) / b
        Simplified for equal odds: f = 2 * edge

        Args:
            edge: Expected edge as decimal (0.05 = 5%)
            odds: Odds of success to failure (1.0 = 50/50)

        Returns:
            Kelly fraction as decimal
        """
        if edge <= 0:
            return 0.0

        # For simplicity, use 2*edge formula
        kelly = 2 * edge

        # Clamp Kelly to max 0.5 (never bet more than 50% on single bet)
        kelly = min(kelly, 0.5)

        return kelly

    def fractional_kelly_size(
        self,
        edge: float,
        capital: float = None,
        kelly_fraction: float = None,
    ) -> float:
        """
        Compute position size using fractional Kelly.

        size = capital * kelly_fraction * KELLY_FRACTION

        Args:
            edge: Expected edge as decimal
            capital: Available capital (uses MAX_POSITION_SIZE_USD if None)
            kelly_fraction: Kelly fraction to use (computed if None)

        Returns:
            Position size in USD
        """
        if capital is None:
            capital = self.config.MAX_POSITION_SIZE_USD

        if kelly_fraction is None:
            kelly_fraction = self.kelly_fraction(edge)

        # Apply fractional Kelly multiplier
        fractional = kelly_fraction * self.config.KELLY_FRACTION

        # Compute size and cap at maximum
        size = capital * fractional
        return min(size, self.config.MAX_POSITION_SIZE_USD)

    def edge_from_spread(
        self,
        raw_spread: float,
        total_fees: float,
        position_size: float,
    ) -> float:
        """
        Compute edge from spread and fees.

        edge = (raw_spread - total_fees) / position_size

        Args:
            raw_spread: Raw spread in dollars
            total_fees: Total fees paid in dollars
            position_size: Size of position in dollars

        Returns:
            Edge as decimal
        """
        if position_size == 0:
            return 0.0

        net_spread = raw_spread - total_fees
        return net_spread / position_size

    def size_from_spread(
        self,
        raw_spread_bps: int,
        fees_bps: int,
    ) -> float:
        """
        Compute position size from spread in basis points.

        Args:
            raw_spread_bps: Raw spread in basis points
            fees_bps: Total fees in basis points

        Returns:
            Position size in USD
        """
        raw_spread_decimal = raw_spread_bps / 10000
        fees_decimal = fees_bps / 10000

        net_spread = raw_spread_decimal - fees_decimal

        if net_spread <= 0:
            return 0.0

        # Edge = net_spread (for 1:1 odds)
        edge = net_spread

        return self.fractional_kelly_size(edge=edge)


# ============================================================================
# Test Cases
# ============================================================================

class TestKellySizingBasics:
    """Test basic Kelly sizing calculations."""

    def test_quarter_kelly_calculation(self, sample_config):
        """Known input produces expected size."""
        sizer = PositionSizer(sample_config)

        # Edge of 5%, capital of $10,000
        # Kelly fraction = 2 * 0.05 = 0.10
        # Fractional Kelly = 0.10 * 0.25 = 0.025
        # Size = 10000 * 0.025 = 250
        size = sizer.fractional_kelly_size(edge=0.05)

        assert size == 250.0

    def test_kelly_with_higher_edge(self, sample_config):
        """Higher edge produces larger position."""
        sizer = PositionSizer(sample_config)

        size_5pct = sizer.fractional_kelly_size(edge=0.05)
        size_10pct = sizer.fractional_kelly_size(edge=0.10)

        assert size_10pct > size_5pct
        # 10% edge: Kelly = 0.20, Frac = 0.05, Size = 500
        assert size_10pct == 500.0

    def test_kelly_with_lower_edge(self, sample_config):
        """Lower edge produces smaller position."""
        sizer = PositionSizer(sample_config)

        size_5pct = sizer.fractional_kelly_size(edge=0.05)
        size_2pct = sizer.fractional_kelly_size(edge=0.02)

        assert size_2pct < size_5pct
        # 2% edge: Kelly = 0.04, Frac = 0.01, Size = 100
        assert size_2pct == 100.0


class TestKellyCapping:
    """Test Kelly fraction capping logic."""

    def test_kelly_never_exceeds_max(self, sample_config):
        """Even huge edge doesn't exceed max position."""
        sizer = PositionSizer(sample_config)

        size = sizer.fractional_kelly_size(edge=0.50)

        assert size <= sample_config.MAX_POSITION_SIZE_USD

    def test_kelly_fraction_ceiling(self, sample_config):
        """Kelly fraction is capped at 0.5."""
        sizer = PositionSizer(sample_config)

        # 30% edge would imply Kelly of 0.60, but capped to 0.50
        size = sizer.fractional_kelly_size(edge=0.30)

        # Kelly capped to 0.50
        # Fractional Kelly = 0.50 * 0.25 = 0.125
        # Size = 10000 * 0.125 = 1250
        assert size == 1250.0

    def test_kelly_capping_at_50_percent(self, sample_config):
        """Kelly is never computed to exceed 50%."""
        sizer = PositionSizer(sample_config)

        kelly_frac = sizer.kelly_fraction(edge=0.50)

        assert kelly_frac == 0.5


class TestEdgeCases:
    """Test edge cases in sizing."""

    def test_zero_edge_produces_zero_size(self, sample_config):
        """Zero edge produces zero position."""
        sizer = PositionSizer(sample_config)

        size = sizer.fractional_kelly_size(edge=0.0)

        assert size == 0.0

    def test_negative_edge_produces_zero_size(self, sample_config):
        """Negative edge produces zero position."""
        sizer = PositionSizer(sample_config)

        size = sizer.fractional_kelly_size(edge=-0.05)

        assert size == 0.0

    def test_very_small_edge(self, sample_config):
        """Very small edge produces proportional size."""
        sizer = PositionSizer(sample_config)

        size = sizer.fractional_kelly_size(edge=0.001)

        # Kelly = 0.002, Frac = 0.0005, Size = 5
        assert size == 5.0

    def test_edge_precision(self, sample_config):
        """Edge calculations maintain precision."""
        sizer = PositionSizer(sample_config)

        size1 = sizer.fractional_kelly_size(edge=0.051)
        size2 = sizer.fractional_kelly_size(edge=0.050)

        # Should have small but measurable difference
        assert abs(size1 - size2) > 0
        assert abs(size1 - size2) < 10.0


class TestSizingWithCapital:
    """Test sizing with different capital amounts."""

    def test_sizing_respects_capital_input(self, sample_config):
        """Sizing scales with input capital."""
        sizer = PositionSizer(sample_config)

        size_10k = sizer.fractional_kelly_size(edge=0.05, capital=10000)
        size_20k = sizer.fractional_kelly_size(edge=0.05, capital=20000)

        assert size_20k == 2 * size_10k

    def test_sizing_capped_at_max(self, sample_config):
        """Even with large capital, respects max."""
        sizer = PositionSizer(sample_config)

        # Even with $1M capital
        size = sizer.fractional_kelly_size(edge=0.20, capital=1000000)

        assert size <= sample_config.MAX_POSITION_SIZE_USD

    def test_sizing_with_small_capital(self, sample_config):
        """Sizing works with small capital amounts."""
        sizer = PositionSizer(sample_config)

        size = sizer.fractional_kelly_size(edge=0.05, capital=1000)

        # Kelly = 0.10, Frac = 0.025, Size = 1000 * 0.025 = 25
        assert size == 25.0


class TestEdgeComputation:
    """Test edge calculation from spread."""

    def test_edge_from_spread_basic(self, sample_config):
        """Basic edge computation from spread."""
        sizer = PositionSizer(sample_config)

        # $10 spread, $1 fees, $100 position
        edge = sizer.edge_from_spread(
            raw_spread=10.0,
            total_fees=1.0,
            position_size=100.0,
        )

        # Edge = (10 - 1) / 100 = 0.09
        assert edge == 0.09

    def test_edge_from_spread_no_profit(self, sample_config):
        """Spread consumed by fees results in zero edge."""
        sizer = PositionSizer(sample_config)

        edge = sizer.edge_from_spread(
            raw_spread=2.0,
            total_fees=2.0,
            position_size=100.0,
        )

        assert edge == 0.0

    def test_edge_from_spread_negative(self, sample_config):
        """Fees exceed spread results in negative edge."""
        sizer = PositionSizer(sample_config)

        edge = sizer.edge_from_spread(
            raw_spread=1.0,
            total_fees=2.0,
            position_size=100.0,
        )

        # Edge = (1 - 2) / 100 = -0.01
        assert edge == -0.01


class TestSizingFromSpreadBasis:
    """Test sizing directly from spread in basis points."""

    def test_sizing_from_50bps_spread(self, sample_config):
        """Minimum spread (50bps) produces minimal size."""
        sizer = PositionSizer(sample_config)

        # 50bps spread, 20bps total fees
        size = sizer.size_from_spread(raw_spread_bps=50, fees_bps=20)

        # Net spread = 30bps = 0.003 = edge
        # Kelly = 0.006, Frac = 0.0015, Size = 150
        assert size == 150.0

    def test_sizing_from_100bps_spread(self, sample_config):
        """Larger spread produces larger size."""
        sizer = PositionSizer(sample_config)

        size = sizer.size_from_spread(raw_spread_bps=100, fees_bps=20)

        # Net spread = 80bps = 0.008
        # Kelly = 0.016, Frac = 0.004, Size = 400
        assert size == 400.0

    def test_sizing_from_zero_net_spread(self, sample_config):
        """Fees consuming spread results in zero size."""
        sizer = PositionSizer(sample_config)

        size = sizer.size_from_spread(raw_spread_bps=20, fees_bps=40)

        assert size == 0.0

    def test_sizing_scales_with_spread(self, sample_config):
        """Size scales linearly with spread (before Kelly cap)."""
        sizer = PositionSizer(sample_config)

        size_50bps = sizer.size_from_spread(raw_spread_bps=50, fees_bps=20)
        size_100bps = sizer.size_from_spread(raw_spread_bps=100, fees_bps=20)

        # Should scale roughly 2:1 (before Kelly capping)
        assert size_100bps > size_50bps


class TestFractionalKellyFraction:
    """Test fractional Kelly multiplier application."""

    def test_kelly_fraction_defaults_to_config(self, sample_config):
        """Fractional Kelly uses config value."""
        sizer = PositionSizer(sample_config)

        # With default KELLY_FRACTION of 0.25
        size = sizer.fractional_kelly_size(edge=0.10)

        # Kelly = 0.20, Frac = 0.20 * 0.25 = 0.05
        # Size = 10000 * 0.05 = 500
        assert size == 500.0

    def test_kelly_fraction_override(self, sample_config):
        """Custom Kelly fraction can be provided."""
        sizer = PositionSizer(sample_config)

        # Override to 0.50 (half Kelly)
        size = sizer.fractional_kelly_size(
            edge=0.10,
            kelly_fraction=0.50,
        )

        # Kelly = 0.20, Frac = 0.20 * 0.50 = 0.10
        # Size = 10000 * 0.10 = 1000
        assert size == 1000.0

    def test_conservative_kelly_fraction(self, sample_config):
        """More conservative Kelly produces smaller sizes."""
        sizer = PositionSizer(sample_config)

        size_full = sizer.fractional_kelly_size(edge=0.10, kelly_fraction=1.0)
        size_half = sizer.fractional_kelly_size(edge=0.10, kelly_fraction=0.50)
        size_quarter = sizer.fractional_kelly_size(edge=0.10, kelly_fraction=0.25)

        assert size_full > size_half
        assert size_half > size_quarter


class TestMultiplePositionSizing:
    """Test sizing across multiple positions."""

    def test_sequential_sizing_consistency(self, sample_config):
        """Multiple positions with same edge get same size."""
        sizer = PositionSizer(sample_config)

        size1 = sizer.fractional_kelly_size(edge=0.05)
        size2 = sizer.fractional_kelly_size(edge=0.05)

        assert size1 == size2

    def test_sizing_different_edges(self, sample_config):
        """Different edges produce different sizes."""
        sizer = PositionSizer(sample_config)

        sizes = [
            sizer.fractional_kelly_size(edge=e)
            for e in [0.02, 0.05, 0.10, 0.15, 0.20]
        ]

        # All should be increasing
        for i in range(len(sizes) - 1):
            assert sizes[i] < sizes[i + 1]

    def test_sizing_for_portfolio(self, sample_config):
        """Compute sizes for a portfolio of signals."""
        sizer = PositionSizer(sample_config)

        edges = [0.03, 0.05, 0.07, 0.10]
        sizes = [sizer.fractional_kelly_size(edge=e) for e in edges]
        total = sum(sizes)

        # Total should be reasonable
        assert total < sample_config.MAX_POSITION_SIZE_USD * 2
        assert all(s > 0 for s in sizes)
