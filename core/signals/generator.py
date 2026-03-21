"""Signal generator: consumes violations, produces trading signals."""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class OrderType(str, Enum):
    """Order type for signal leg."""

    LIMIT = "limit"
    MARKET = "market"


class SignalSide(str, Enum):
    """Position side."""

    BUY = "buy"
    SELL = "sell"


class ExecutionMode(str, Enum):
    """Signal execution mode."""

    LIVE = "live"
    PAPER = "paper"


@dataclass
class SignalLeg:
    """Single leg of a trading signal."""

    leg_id: str
    market_id: str
    platform: str  # "polymarket", "kalshi"
    platform_market_id: str
    side: SignalSide
    order_type: OrderType
    target_price: float
    price_tolerance: float  # Max allowed slippage
    size_usd: float
    expiry_s: int  # Order expiry in seconds


@dataclass
class Signal:
    """Complete trading signal."""

    schema_version: str = "1.0"
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    strategy: str = ""
    signal_type: str = ""
    legs: list[SignalLeg] = field(default_factory=list)
    execution_mode: ExecutionMode = ExecutionMode.LIVE
    abort_on_partial: bool = True
    max_total_slippage_usd: float = 0.0
    fired_at: datetime = field(default_factory=datetime.utcnow)
    ttl_s: int = 300

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dict."""
        return {
            "schema_version": self.schema_version,
            "signal_id": self.signal_id,
            "strategy": self.strategy,
            "signal_type": self.signal_type,
            "legs": [
                {
                    "leg_id": leg.leg_id,
                    "market_id": leg.market_id,
                    "platform": leg.platform,
                    "platform_market_id": leg.platform_market_id,
                    "side": leg.side.value,
                    "order_type": leg.order_type.value,
                    "target_price": leg.target_price,
                    "price_tolerance": leg.price_tolerance,
                    "size_usd": leg.size_usd,
                    "expiry_s": leg.expiry_s,
                }
                for leg in self.legs
            ],
            "execution_mode": self.execution_mode.value,
            "abort_on_partial": self.abort_on_partial,
            "max_total_slippage_usd": self.max_total_slippage_usd,
            "fired_at": self.fired_at.isoformat(),
            "ttl_s": self.ttl_s,
        }


@dataclass
class ViolationDetected:
    """Event indicating a signal should be generated."""

    violation_id: str
    market_id: str
    platform: str
    violation_type: str  # "mispricing", "arbitrage", etc.
    fair_value: float
    market_price: float
    edge: float  # fair_value - market_price
    detected_at: datetime = field(default_factory=datetime.utcnow)
    metadata: dict = field(default_factory=dict)


class SignalGenerator:
    """
    Consumes ViolationDetected events and model fair values.
    Produces SignalFired events to trading execution layer.
    """

    def __init__(
        self,
        event_bus: Optional[Any] = None,
        db: Optional[Any] = None,
        config: Optional[dict] = None,
    ):
        """
        Initialize signal generator.

        Args:
            event_bus: Event bus for publishing signals
            db: Database connection
            config: Configuration dict
        """
        self.event_bus = event_bus
        self.db = db
        self.config = config or {}
        self.paper_trading = self.config.get("paper_trading", False)

    async def process_violation(self, violation: ViolationDetected) -> Optional[Signal]:
        """
        Process a violation event and generate signal if warranted.

        Args:
            violation: ViolationDetected event

        Returns:
            Signal object or None if signal rejected
        """
        logger.info(
            f"Processing violation {violation.violation_id} on {violation.market_id}"
        )

        # Run risk checks
        signal = self._create_signal_from_violation(violation)

        from core.signals.risk import run_all_checks

        checks_passed, check_results = await run_all_checks(
            signal, self.config, self.db
        )

        # Log results
        for result in check_results:
            if result.passed:
                logger.info(f"Check {result.check_type} passed")
            else:
                logger.warning(f"Check {result.check_type} failed: {result.detail}")

        if not checks_passed:
            logger.warning(f"Signal {signal.signal_id} rejected by risk checks")
            return None

        # Create signal record in DB
        if self.db:
            try:
                await self.db.create_signal_record(
                    signal_id=signal.signal_id,
                    violation_id=violation.violation_id,
                    market_id=violation.market_id,
                    platform=violation.platform,
                    fair_value=violation.fair_value,
                    market_price=violation.market_price,
                    edge=violation.edge,
                    execution_mode=signal.execution_mode.value,
                )
            except Exception as e:
                logger.error(f"Failed to create signal record: {e}")

        # Emit signal
        if self.paper_trading:
            logger.info(f"Paper trading mode: not emitting signal {signal.signal_id}")
        else:
            await self._emit_signal(signal)

        return signal

    def _create_signal_from_violation(self, violation: ViolationDetected) -> Signal:
        """Create signal from violation."""
        from core.signals.sizing import compute_position_size, compute_kelly_fraction

        signal = Signal(
            strategy=self.config.get("strategy_name", "prediction_market"),
            signal_type=violation.violation_type,
            execution_mode=(
                ExecutionMode.PAPER if self.paper_trading else ExecutionMode.LIVE
            ),
        )

        # Compute position sizing
        bankroll = self.config.get("bankroll_usd", 100000)
        kelly_fraction = self.config.get("kelly_fraction", 0.25)
        max_position_size = self.config.get("max_position_size_usd", 5000)

        kelly_f = compute_kelly_fraction(
            edge=violation.edge,
            odds=violation.market_price,
            kelly_fraction=kelly_fraction,
        )

        position_size = compute_position_size(
            kelly_f=kelly_f,
            bankroll=bankroll,
            max_size=max_position_size,
        )

        # Determine side based on edge
        side = SignalSide.BUY if violation.edge > 0 else SignalSide.SELL

        leg = SignalLeg(
            leg_id=str(uuid.uuid4()),
            market_id=violation.market_id,
            platform=violation.platform,
            platform_market_id=violation.metadata.get("platform_market_id", ""),
            side=side,
            order_type=OrderType.LIMIT,
            target_price=violation.fair_value,
            price_tolerance=self.config.get("price_tolerance", 0.02),
            size_usd=position_size,
            expiry_s=self.config.get("order_expiry_s", 300),
        )

        signal.legs.append(leg)
        signal.max_total_slippage_usd = self.config.get("max_total_slippage_usd", 100.0)

        return signal

    async def _emit_signal(self, signal: Signal) -> None:
        """Emit signal to execution layer."""
        if not self.event_bus:
            logger.warning("No event bus configured, signal not emitted")
            return

        try:
            await self.event_bus.publish(
                "signal.fired",
                signal.to_dict(),
            )
            logger.info(f"Signal {signal.signal_id} emitted to event bus")
        except Exception as e:
            logger.error(f"Failed to emit signal: {e}")
