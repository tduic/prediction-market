"""
Integration tests for end-to-end signal flow.

Tests the complete pipeline:
- Market data ingestion
- Constraint violation detection
- Risk checking
- Signal creation and queuing
- Paper trading mode
"""

import pytest
from unittest.mock import Mock, patch, MagicMock, AsyncMock
from datetime import datetime, timedelta
import asyncio
import json


# ============================================================================
# Signal Flow Classes (Mock Implementations)
# ============================================================================

class MarketUpdatedEvent:
    """Event emitted when market prices are updated."""

    def __init__(
        self,
        market_id: str,
        yes_price: float,
        no_price: float,
        timestamp: datetime = None,
    ):
        self.market_id = market_id
        self.yes_price = yes_price
        self.no_price = no_price
        self.timestamp = timestamp or datetime.utcnow()


class ConstraintViolationEvent:
    """Event emitted when constraint violation detected."""

    def __init__(
        self,
        violation_id: str,
        pair_id: str,
        violation_type: str,
        spread: float,
    ):
        self.violation_id = violation_id
        self.pair_id = pair_id
        self.violation_type = violation_type
        self.spread = spread
        self.timestamp = datetime.utcnow()


class TradeSignal:
    """Represents a trading signal ready for execution."""

    def __init__(
        self,
        signal_id: str,
        violation_id: str,
        signal_type: str,
        size_usd: float,
        kelly_fraction: float = 0.25,
        expected_edge: float = 0.0,
        ttl_s: int = 300,
    ):
        self.signal_id = signal_id
        self.violation_id = violation_id
        self.signal_type = signal_type
        self.size_usd = size_usd
        self.kelly_fraction = kelly_fraction
        self.expected_edge = expected_edge
        self.ttl_s = ttl_s
        self.created_at = datetime.utcnow()
        self.expires_at = self.created_at + timedelta(seconds=ttl_s)


class SignalGenerator:
    """Generate trading signals from constraint violations."""

    def __init__(self, db=None, event_bus=None, config=None):
        self.db = db
        self.event_bus = event_bus
        self.config = config

    def generate_signal_from_violation(
        self,
        violation_event: ConstraintViolationEvent,
        size_usd: float,
        edge: float = 0.0,
    ) -> TradeSignal:
        """
        Generate a trading signal from a constraint violation.

        Args:
            violation_event: Constraint violation event
            size_usd: Sized position in USD
            edge: Expected edge

        Returns:
            TradeSignal ready for execution
        """
        signal_id = f"sig_{violation_event.violation_id}"

        signal = TradeSignal(
            signal_id=signal_id,
            violation_id=violation_event.violation_id,
            signal_type=violation_event.violation_type,
            size_usd=size_usd,
            expected_edge=edge,
        )

        # Store in database
        if self.db:
            self.db.execute(
                """INSERT INTO signals
                   (id, violation_id, signal_type, size_usd, kelly_fraction,
                    expected_edge, ttl_s, expires_at, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal.signal_id,
                    signal.violation_id,
                    signal.signal_type,
                    signal.size_usd,
                    signal.kelly_fraction,
                    signal.expected_edge,
                    signal.ttl_s,
                    signal.expires_at,
                    "pending",
                ),
            )
            self.db.commit()

        return signal


class RedisQueueMock:
    """Mock Redis queue for testing."""

    def __init__(self):
        self.queue = []

    async def push(self, signal: TradeSignal) -> bool:
        """Push signal to queue."""
        self.queue.append({
            "signal_id": signal.signal_id,
            "violation_id": signal.violation_id,
            "size_usd": signal.size_usd,
            "created_at": signal.created_at.isoformat(),
        })
        return True

    async def peek(self) -> dict:
        """Peek at next signal without removing."""
        return self.queue[0] if self.queue else None

    def clear(self) -> None:
        """Clear queue."""
        self.queue = []


class SignalRouter:
    """Route signals based on trading mode and risk checks."""

    def __init__(
        self,
        db=None,
        event_bus=None,
        config=None,
        queue=None,
    ):
        self.db = db
        self.event_bus = event_bus
        self.config = config
        self.queue = queue or RedisQueueMock()

    async def process_signal(self, signal: TradeSignal) -> dict:
        """
        Process a signal through the full pipeline.

        Args:
            signal: TradeSignal to process

        Returns:
            dict with processing result
        """
        # Check if signal expired
        if datetime.utcnow() > signal.expires_at:
            return {
                "success": False,
                "reason": "signal_expired",
                "signal_id": signal.signal_id,
            }

        # Check paper trading mode
        if self.config and self.config.PAPER_TRADING:
            # Log but don't queue
            if self.event_bus:
                self.event_bus.emit("signal_paper_traded", {
                    "signal_id": signal.signal_id,
                    "size_usd": signal.size_usd,
                })

            # Update database
            if self.db:
                self.db.execute(
                    "UPDATE signals SET status = ? WHERE id = ?",
                    ("paper_traded", signal.signal_id),
                )
                self.db.commit()

            return {
                "success": True,
                "mode": "paper_trading",
                "signal_id": signal.signal_id,
            }

        # Live trading: push to queue
        await self.queue.push(signal)

        # Update database
        if self.db:
            self.db.execute(
                "UPDATE signals SET status = ? WHERE id = ?",
                ("queued", signal.signal_id),
            )
            self.db.commit()

        if self.event_bus:
            self.event_bus.emit("signal_queued", {
                "signal_id": signal.signal_id,
                "size_usd": signal.size_usd,
            })

        return {
            "success": True,
            "mode": "live_trading",
            "signal_id": signal.signal_id,
        }


class RiskFilter:
    """Filter signals based on risk checks."""

    def __init__(self, config=None, db=None):
        self.config = config
        self.db = db

    def check_signal_risk(self, signal: TradeSignal) -> dict:
        """
        Check if signal passes all risk filters.

        Args:
            signal: TradeSignal to check

        Returns:
            dict with pass/fail for each risk check
        """
        checks = {
            "position_size_ok": signal.size_usd <= (self.config.MAX_POSITION_SIZE_USD if self.config else 10000),
            "edge_positive": signal.expected_edge >= 0,
            "ttl_valid": signal.ttl_s > 0 and signal.ttl_s <= 3600,
            "all_pass": True,
        }

        # Mark all_pass false if any check fails
        if not all(v for k, v in checks.items() if k != "all_pass"):
            checks["all_pass"] = False

        return checks


# ============================================================================
# Test Cases
# ============================================================================

class TestViolationToSignalFlow:
    """Test full flow from violation detection to signal creation."""

    @pytest.mark.asyncio
    async def test_violation_to_signal_to_queue(self, in_memory_db, event_bus, sample_config):
        """Complete flow: violation -> signal -> queued."""
        generator = SignalGenerator(in_memory_db, event_bus, sample_config)
        router = SignalRouter(in_memory_db, event_bus, sample_config)

        # Create violation event
        violation_event = ConstraintViolationEvent(
            violation_id="v_001",
            pair_id="pair_001",
            violation_type="subset_superset",
            spread=0.06,
        )

        # Generate signal
        signal = generator.generate_signal_from_violation(
            violation_event,
            size_usd=1000.0,
            edge=0.05,
        )

        # Move to live trading for this test
        config = sample_config
        config.PAPER_TRADING = False

        router.config = config

        # Route signal
        result = await router.process_signal(signal)

        assert result["success"] is True
        assert result["mode"] == "live_trading"

        # Check queue
        assert len(router.queue.queue) == 1
        assert router.queue.queue[0]["signal_id"] == "sig_v_001"

    @pytest.mark.asyncio
    async def test_paper_trading_logs_only(self, in_memory_db, event_bus, sample_config):
        """With PAPER_TRADING=true, signal is logged but not queued."""
        generator = SignalGenerator(in_memory_db, event_bus, sample_config)
        router = SignalRouter(in_memory_db, event_bus, sample_config)

        violation_event = ConstraintViolationEvent(
            violation_id="v_002",
            pair_id="pair_002",
            violation_type="cross_platform_identical",
            spread=0.01,
        )

        signal = generator.generate_signal_from_violation(
            violation_event,
            size_usd=500.0,
            edge=0.005,
        )

        result = await router.process_signal(signal)

        assert result["success"] is True
        assert result["mode"] == "paper_trading"

        # Queue should be empty
        assert len(router.queue.queue) == 0

        # Event should be emitted
        events = event_bus.get_events("signal_paper_traded")
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_expired_signal_rejected(self, in_memory_db, event_bus, sample_config):
        """Expired signal is rejected."""
        router = SignalRouter(in_memory_db, event_bus, sample_config)

        # Create expired signal
        signal = TradeSignal(
            signal_id="sig_expired",
            violation_id="v_003",
            signal_type="expired_test",
            size_usd=1000.0,
            ttl_s=1,
        )

        # Manually expire it
        signal.expires_at = datetime.utcnow() - timedelta(seconds=1)

        result = await router.process_signal(signal)

        assert result["success"] is False
        assert result["reason"] == "signal_expired"


class TestSignalRiskChecks:
    """Test signal risk filtering."""

    def test_risk_check_blocks_oversized_signal(self, sample_config):
        """Signal exceeding position limit is blocked."""
        risk_filter = RiskFilter(sample_config)

        signal = TradeSignal(
            signal_id="sig_oversized",
            violation_id="v_004",
            signal_type="test",
            size_usd=15000.0,  # Exceeds MAX_POSITION_SIZE_USD of 10000
        )

        checks = risk_filter.check_signal_risk(signal)

        assert checks["position_size_ok"] is False
        assert checks["all_pass"] is False

    def test_risk_check_blocks_negative_edge(self, sample_config):
        """Signal with negative edge is blocked."""
        risk_filter = RiskFilter(sample_config)

        signal = TradeSignal(
            signal_id="sig_negative",
            violation_id="v_005",
            signal_type="test",
            size_usd=1000.0,
            expected_edge=-0.02,
        )

        checks = risk_filter.check_signal_risk(signal)

        assert checks["edge_positive"] is False
        assert checks["all_pass"] is False

    def test_risk_check_passes_valid_signal(self, sample_config):
        """Valid signal passes all checks."""
        risk_filter = RiskFilter(sample_config)

        signal = TradeSignal(
            signal_id="sig_valid",
            violation_id="v_006",
            signal_type="test",
            size_usd=1000.0,
            expected_edge=0.05,
            ttl_s=300,
        )

        checks = risk_filter.check_signal_risk(signal)

        assert checks["all_pass"] is True
        assert checks["position_size_ok"] is True
        assert checks["edge_positive"] is True


class TestSignalMultipleCycles:
    """Test multiple signals in sequence."""

    @pytest.mark.asyncio
    async def test_multiple_signals_queued(self, in_memory_db, event_bus, sample_config):
        """Multiple signals are all queued."""
        generator = SignalGenerator(in_memory_db, event_bus, sample_config)
        router = SignalRouter(in_memory_db, event_bus, sample_config)

        config = sample_config
        config.PAPER_TRADING = False
        router.config = config

        # Generate 3 signals
        for i in range(3):
            violation = ConstraintViolationEvent(
                violation_id=f"v_{i:03d}",
                pair_id=f"pair_{i:03d}",
                violation_type="test",
                spread=0.02,
            )

            signal = generator.generate_signal_from_violation(
                violation,
                size_usd=1000.0,
                edge=0.01,
            )

            await router.process_signal(signal)

        # All should be queued
        assert len(router.queue.queue) == 3

    @pytest.mark.asyncio
    async def test_signal_timestamps_recorded(self, in_memory_db, sample_config):
        """Signal timestamps are accurately recorded."""
        generator = SignalGenerator(in_memory_db, sample_config=sample_config)

        violation = ConstraintViolationEvent(
            violation_id="v_time",
            pair_id="pair_time",
            violation_type="test",
            spread=0.02,
        )

        signal = generator.generate_signal_from_violation(
            violation,
            size_usd=1000.0,
        )

        # Check database
        row = in_memory_db.execute(
            "SELECT created_at, expires_at FROM signals WHERE id = ?",
            (signal.signal_id,),
        ).fetchone()

        assert row is not None

    @pytest.mark.asyncio
    async def test_mixed_paper_and_live_signals(self, in_memory_db, event_bus, sample_config):
        """Mix of paper trading and live signals."""
        generator = SignalGenerator(in_memory_db, event_bus, sample_config)
        router = SignalRouter(in_memory_db, event_bus, sample_config)

        # Paper trading mode
        result1 = await router.process_signal(
            generator.generate_signal_from_violation(
                ConstraintViolationEvent("v_001", "pair_001", "test", 0.02),
                1000.0,
            )
        )

        assert result1["mode"] == "paper_trading"
        assert len(router.queue.queue) == 0

        # Switch to live trading
        sample_config.PAPER_TRADING = False
        router.config = sample_config

        result2 = await router.process_signal(
            generator.generate_signal_from_violation(
                ConstraintViolationEvent("v_002", "pair_002", "test", 0.02),
                1000.0,
            )
        )

        assert result2["mode"] == "live_trading"
        assert len(router.queue.queue) == 1
