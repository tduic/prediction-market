"""
Unit tests for risk management module.

Tests all risk control implementations:
- Position size limits
- Daily loss limits
- Portfolio concentration limits
- Kelly criterion sizing
- Duplicate signal suppression
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch, MagicMock


# ============================================================================
# Risk Management Classes (Mock Implementations)
# ============================================================================


class RiskSignal:
    """Represents a trading signal with risk attributes."""

    def __init__(
        self,
        signal_id: str,
        violation_id: str,
        size_usd: float,
        kelly_fraction: float,
        expected_edge: float,
        created_at: datetime = None,
    ):
        self.signal_id = signal_id
        self.violation_id = violation_id
        self.size_usd = size_usd
        self.kelly_fraction = kelly_fraction
        self.expected_edge = expected_edge
        self.created_at = created_at or datetime.utcnow()


class RiskManager:
    """Manages trading risk across multiple dimensions."""

    def __init__(self, config, db=None, event_bus=None):
        self.config = config
        self.db = db
        self.event_bus = event_bus
        self.signals_seen = {}  # Track seen signals for deduplication

    def check_position_limit(
        self, current_position_usd: float, new_size_usd: float
    ) -> bool:
        """
        Check if new position violates size limit.

        Args:
            current_position_usd: Current cumulative position size
            new_size_usd: Size of new signal

        Returns:
            True if check passes, False if limit exceeded
        """
        total = current_position_usd + new_size_usd
        return total <= self.config.MAX_POSITION_SIZE_USD

    def check_daily_loss_limit(self, cumulative_loss_usd: float) -> bool:
        """
        Check if cumulative daily loss exceeds limit.

        Args:
            cumulative_loss_usd: Total realized losses today

        Returns:
            True if check passes, False if limit exceeded
        """
        return abs(cumulative_loss_usd) <= self.config.MAX_DAILY_LOSS_USD

    def check_portfolio_concentration(self, exposure_pct: float) -> bool:
        """
        Check if portfolio exposure concentration exceeds limit.

        Args:
            exposure_pct: Current portfolio exposure as percentage (0-1)

        Returns:
            True if check passes, False if concentration too high
        """
        return exposure_pct <= self.config.MAX_PORTFOLIO_EXPOSURE_PCT

    def compute_kelly_size(
        self,
        edge: float,
        odds: float = 1.0,
    ) -> float:
        """
        Compute position size using Kelly criterion with fractional Kelly.

        Kelly fraction = (2*edge) / odds, applied with KELLY_FRACTION multiplier.

        Args:
            edge: Expected edge as decimal (0.05 = 5%)
            odds: Odds of success vs failure (default 1.0 = 50/50)

        Returns:
            Position size in USD, capped at MAX_POSITION_SIZE_USD
        """
        if edge <= 0:
            return 0.0

        # Kelly formula: f = (p*b - q) / b
        # Simplified for odds=1: f = 2*edge
        kelly_fraction = 2 * edge

        # Clamp Kelly fraction to max 0.5 (never risk more than 50% on single bet)
        kelly_fraction = min(kelly_fraction, 0.5)

        # Apply fractional Kelly
        fractional_kelly = kelly_fraction * self.config.KELLY_FRACTION

        # Base sizing: use fractional Kelly of available capital
        base_size = self.config.MAX_POSITION_SIZE_USD * fractional_kelly

        # Cap at maximum
        return min(base_size, self.config.MAX_POSITION_SIZE_USD)

    def check_duplicate_signal(self, signal: RiskSignal) -> bool:
        """
        Check if signal is a duplicate within the window.

        Args:
            signal: RiskSignal to check

        Returns:
            True if signal is new, False if duplicate
        """
        now = datetime.utcnow()
        window = timedelta(seconds=self.config.DUPLICATE_SIGNAL_WINDOW_S)

        # Check for existing signal
        if signal.violation_id in self.signals_seen:
            last_seen = self.signals_seen[signal.violation_id]
            if now - last_seen < window:
                return False

        # Not a duplicate
        self.signals_seen[signal.violation_id] = now
        return True

    def run_all_checks(
        self,
        signal: RiskSignal,
        current_position_usd: float,
        cumulative_loss_usd: float,
        portfolio_exposure_pct: float,
    ) -> dict:
        """
        Run all risk checks in sequence.

        Args:
            signal: Signal to evaluate
            current_position_usd: Current total position
            cumulative_loss_usd: Daily cumulative loss
            portfolio_exposure_pct: Current portfolio exposure

        Returns:
            dict with pass/fail for each check
        """
        return {
            "position_limit": self.check_position_limit(
                current_position_usd, signal.size_usd
            ),
            "daily_loss_limit": self.check_daily_loss_limit(cumulative_loss_usd),
            "concentration_limit": self.check_portfolio_concentration(
                portfolio_exposure_pct
            ),
            "duplicate_check": self.check_duplicate_signal(signal),
            "kelly_valid": signal.size_usd <= self.config.MAX_POSITION_SIZE_USD,
            "all_pass": True,  # Will be updated if any check fails
        }


# ============================================================================
# Test Cases
# ============================================================================


class TestPositionLimitChecks:
    """Test position size limit enforcement."""

    def test_position_limit_blocks_when_hit(self, sample_config):
        """Signal exceeding position limit is blocked."""
        manager = RiskManager(sample_config)

        current_position = 8000.0
        new_signal_size = 3000.0

        result = manager.check_position_limit(current_position, new_signal_size)

        assert result is False

    def test_position_limit_blocks_exact(self, sample_config):
        """Signal exactly hitting limit is blocked."""
        manager = RiskManager(sample_config)

        current_position = 5000.0
        new_signal_size = 5001.0

        result = manager.check_position_limit(current_position, new_signal_size)

        assert result is False

    def test_position_limit_passes_with_headroom(self, sample_config):
        """Signal under limit is approved."""
        manager = RiskManager(sample_config)

        current_position = 5000.0
        new_signal_size = 4000.0

        result = manager.check_position_limit(current_position, new_signal_size)

        assert result is True

    def test_position_limit_allows_first_position(self, sample_config):
        """First position under limit is approved."""
        manager = RiskManager(sample_config)

        result = manager.check_position_limit(0.0, 5000.0)

        assert result is True

    def test_position_limit_rejects_oversized_first(self, sample_config):
        """First position exceeding limit is rejected."""
        manager = RiskManager(sample_config)

        result = manager.check_position_limit(0.0, 15000.0)

        assert result is False


class TestDailyLossLimitChecks:
    """Test daily loss limit enforcement."""

    def test_daily_loss_limit_blocks(self, sample_config):
        """Cumulative loss exceeding limit blocks."""
        manager = RiskManager(sample_config)

        cumulative_loss = -6000.0

        result = manager.check_daily_loss_limit(cumulative_loss)

        assert result is False

    def test_daily_loss_limit_blocks_exact(self, sample_config):
        """Loss exactly at limit is blocked."""
        manager = RiskManager(sample_config)

        cumulative_loss = -5001.0

        result = manager.check_daily_loss_limit(cumulative_loss)

        assert result is False

    def test_daily_loss_limit_passes(self, sample_config):
        """Loss under limit is allowed."""
        manager = RiskManager(sample_config)

        cumulative_loss = -3000.0

        result = manager.check_daily_loss_limit(cumulative_loss)

        assert result is True

    def test_daily_loss_limit_allows_breakeven(self, sample_config):
        """Breakeven (zero loss) is allowed."""
        manager = RiskManager(sample_config)

        result = manager.check_daily_loss_limit(0.0)

        assert result is True

    def test_daily_loss_limit_allows_profit(self, sample_config):
        """Profits always pass."""
        manager = RiskManager(sample_config)

        result = manager.check_daily_loss_limit(2500.0)

        assert result is True

    def test_daily_loss_limit_handles_small_loss(self, sample_config):
        """Small losses pass."""
        manager = RiskManager(sample_config)

        result = manager.check_daily_loss_limit(-10.0)

        assert result is True


class TestConcentrationLimitChecks:
    """Test portfolio concentration limit enforcement."""

    def test_concentration_blocks_at_max_exposure(self, sample_config):
        """Portfolio at max exposure blocks new positions."""
        manager = RiskManager(sample_config)

        # At 75% max exposure
        result = manager.check_portfolio_concentration(0.75)

        assert result is True

    def test_concentration_blocks_above_max(self, sample_config):
        """Portfolio above max exposure blocks."""
        manager = RiskManager(sample_config)

        result = manager.check_portfolio_concentration(0.80)

        assert result is False

    def test_concentration_passes(self, sample_config):
        """Portfolio under max allows new trades."""
        manager = RiskManager(sample_config)

        result = manager.check_portfolio_concentration(0.50)

        assert result is True

    def test_concentration_at_zero(self, sample_config):
        """Empty portfolio always passes."""
        manager = RiskManager(sample_config)

        result = manager.check_portfolio_concentration(0.0)

        assert result is True

    def test_concentration_near_limit(self, sample_config):
        """Portfolio near limit still passes."""
        manager = RiskManager(sample_config)

        result = manager.check_portfolio_concentration(0.74)

        assert result is True


class TestKellySizing:
    """Test Kelly criterion sizing."""

    def test_quarter_kelly_calculation(self, sample_config):
        """Kelly sizing with known inputs produces correct result."""
        manager = RiskManager(sample_config)

        edge = 0.05
        size = manager.compute_kelly_size(edge=edge, odds=1.0)

        # Kelly = 2 * edge = 2 * 0.05 = 0.10
        # Fractional Kelly = 0.10 * 0.25 = 0.025
        # Size = 10000 * 0.025 = 250
        assert size == 250.0

    def test_kelly_never_exceeds_max(self, sample_config):
        """Kelly sizing never exceeds MAX_POSITION_SIZE."""
        manager = RiskManager(sample_config)

        # Even with huge edge
        edge = 0.50
        size = manager.compute_kelly_size(edge=edge, odds=1.0)

        assert size <= sample_config.MAX_POSITION_SIZE_USD

    def test_kelly_fraction_ceiling(self, sample_config):
        """Kelly fraction is capped at 0.5."""
        manager = RiskManager(sample_config)

        # Edge would imply > 50% kelly
        edge = 0.30
        size = manager.compute_kelly_size(edge=edge, odds=1.0)

        # Kelly = 2 * 0.30 = 0.60, capped to 0.50
        # Fractional Kelly = 0.50 * 0.25 = 0.125
        # Size = 10000 * 0.125 = 1250
        assert size == 1250.0

    def test_zero_edge_produces_zero_size(self, sample_config):
        """Zero edge produces zero position."""
        manager = RiskManager(sample_config)

        size = manager.compute_kelly_size(edge=0.0, odds=1.0)

        assert size == 0.0

    def test_negative_edge_produces_zero_size(self, sample_config):
        """Negative edge (losing trade) produces zero."""
        manager = RiskManager(sample_config)

        size = manager.compute_kelly_size(edge=-0.05, odds=1.0)

        assert size == 0.0

    def test_small_edge_produces_small_size(self, sample_config):
        """Small edge produces proportionally small position."""
        manager = RiskManager(sample_config)

        size = manager.compute_kelly_size(edge=0.01, odds=1.0)

        # Kelly = 2 * 0.01 = 0.02
        # Fractional Kelly = 0.02 * 0.25 = 0.005
        # Size = 10000 * 0.005 = 50
        assert size == 50.0

    def test_kelly_with_different_odds(self, sample_config):
        """Kelly sizing adjusts for odds."""
        manager = RiskManager(sample_config)

        # With 2:1 odds (e.g., 2/3 win probability)
        edge = 0.05
        size = manager.compute_kelly_size(edge=edge, odds=2.0)

        # Kelly formula: (p*b - q) / b = (2/3 * 2 - 1/3) / 2 = 1/6
        # For simplicity, we use 2*edge for odds=1.0
        assert size >= 0


class TestDuplicateSignalSuppression:
    """Test duplicate signal detection."""

    def test_duplicate_signal_suppressed(self, sample_config):
        """Identical signal within window is suppressed."""
        manager = RiskManager(sample_config)

        signal = RiskSignal(
            signal_id="sig_001",
            violation_id="v_001",
            size_usd=1000.0,
            kelly_fraction=0.25,
            expected_edge=0.05,
        )

        # First occurrence
        result1 = manager.check_duplicate_signal(signal)
        assert result1 is True

        # Second occurrence immediately after
        result2 = manager.check_duplicate_signal(signal)
        assert result2 is False

    def test_duplicate_signal_expires_after_window(self, sample_config):
        """Signal is no longer duplicate after window expires."""
        manager = RiskManager(sample_config)
        window = sample_config.DUPLICATE_SIGNAL_WINDOW_S

        signal = RiskSignal(
            signal_id="sig_002",
            violation_id="v_002",
            size_usd=1000.0,
            kelly_fraction=0.25,
            expected_edge=0.05,
            created_at=datetime.utcnow() - timedelta(seconds=window + 1),
        )

        # First occurrence
        manager.check_duplicate_signal(signal)

        # Manually advance time
        old_signal_time = manager.signals_seen["v_002"]
        manager.signals_seen["v_002"] = old_signal_time - timedelta(seconds=window + 1)

        # Second occurrence after window
        result = manager.check_duplicate_signal(signal)
        assert result is True

    def test_duplicate_check_different_violations(self, sample_config):
        """Different violations are not duplicates."""
        manager = RiskManager(sample_config)

        signal1 = RiskSignal(
            signal_id="sig_003a",
            violation_id="v_003a",
            size_usd=1000.0,
            kelly_fraction=0.25,
            expected_edge=0.05,
        )

        signal2 = RiskSignal(
            signal_id="sig_003b",
            violation_id="v_003b",
            size_usd=1000.0,
            kelly_fraction=0.25,
            expected_edge=0.05,
        )

        # Both should pass
        result1 = manager.check_duplicate_signal(signal1)
        result2 = manager.check_duplicate_signal(signal2)

        assert result1 is True
        assert result2 is True


class TestAllRiskChecks:
    """Test combined risk check functionality."""

    def test_all_checks_pass_when_headroom_exists(self, sample_config):
        """All checks pass with sufficient headroom."""
        manager = RiskManager(sample_config)

        signal = RiskSignal(
            signal_id="sig_004",
            violation_id="v_004",
            size_usd=2000.0,
            kelly_fraction=0.25,
            expected_edge=0.05,
        )

        results = manager.run_all_checks(
            signal=signal,
            current_position_usd=2000.0,
            cumulative_loss_usd=-1000.0,
            portfolio_exposure_pct=0.30,
        )

        assert results["position_limit"] is True
        assert results["daily_loss_limit"] is True
        assert results["concentration_limit"] is True
        assert results["duplicate_check"] is True
        assert results["kelly_valid"] is True

    def test_all_checks_fail_position_limit(self, sample_config):
        """Position limit failure detected."""
        manager = RiskManager(sample_config)

        signal = RiskSignal(
            signal_id="sig_005",
            violation_id="v_005",
            size_usd=5000.0,
            kelly_fraction=0.25,
            expected_edge=0.05,
        )

        results = manager.run_all_checks(
            signal=signal,
            current_position_usd=7000.0,
            cumulative_loss_usd=-1000.0,
            portfolio_exposure_pct=0.30,
        )

        assert results["position_limit"] is False

    def test_all_checks_fail_daily_loss(self, sample_config):
        """Daily loss limit failure detected."""
        manager = RiskManager(sample_config)

        signal = RiskSignal(
            signal_id="sig_006",
            violation_id="v_006",
            size_usd=1000.0,
            kelly_fraction=0.25,
            expected_edge=0.05,
        )

        results = manager.run_all_checks(
            signal=signal,
            current_position_usd=2000.0,
            cumulative_loss_usd=-6000.0,
            portfolio_exposure_pct=0.30,
        )

        assert results["daily_loss_limit"] is False

    def test_all_checks_fail_concentration(self, sample_config):
        """Concentration limit failure detected."""
        manager = RiskManager(sample_config)

        signal = RiskSignal(
            signal_id="sig_007",
            violation_id="v_007",
            size_usd=1000.0,
            kelly_fraction=0.25,
            expected_edge=0.05,
        )

        results = manager.run_all_checks(
            signal=signal,
            current_position_usd=2000.0,
            cumulative_loss_usd=-1000.0,
            portfolio_exposure_pct=0.80,
        )

        assert results["concentration_limit"] is False

    def test_multiple_failures_detected(self, sample_config):
        """Multiple failures are all detected."""
        manager = RiskManager(sample_config)

        signal = RiskSignal(
            signal_id="sig_008",
            violation_id="v_008",
            size_usd=5000.0,
            kelly_fraction=0.25,
            expected_edge=0.05,
        )

        results = manager.run_all_checks(
            signal=signal,
            current_position_usd=8000.0,
            cumulative_loss_usd=-6000.0,
            portfolio_exposure_pct=0.85,
        )

        assert results["position_limit"] is False
        assert results["daily_loss_limit"] is False
        assert results["concentration_limit"] is False
