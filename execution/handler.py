"""
Signal handler for validating and processing trading signals.

Receives signals from the queue, validates them, runs risk checks,
and routes them to the order router for execution.

Risk checks are ENFORCED here — a signal that fails any check is
rejected and logged, never routed to exchange clients.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite
import redis.asyncio as redis
from pydantic import ValidationError

from core.config import RiskControlConfig, get_config
from core.signals.risk import run_all_checks
from execution.models import OrderLeg, TradingSignal
from execution.router import OrderRouter

logger = logging.getLogger(__name__)


class SignalHandler:
    """Handler for validating and processing trading signals."""

    def __init__(
        self,
        db_connection: aiosqlite.Connection,
        redis_client: redis.Redis,
        execution_mode: str = "live",
        risk_config: RiskControlConfig | None = None,
        reconciliation_engine: Any | None = None,
    ) -> None:
        """
        Initialize the signal handler.

        Args:
            db_connection: SQLite connection for writing results
            redis_client: Redis client for queue operations
            execution_mode: Execution mode - "live", "paper", or "mock"
            risk_config: Risk control configuration. If None, loaded from env.
            reconciliation_engine: Optional ReconciliationEngine for halt checks.
        """
        self.db_connection = db_connection
        self.redis_client = redis_client
        self.execution_mode = execution_mode
        self.order_router = OrderRouter(db_connection, execution_mode=execution_mode)
        self.risk_config = risk_config or get_config().risk_controls
        self.reconciliation_engine = reconciliation_engine

    async def validate_signal(self, payload: dict[str, Any]) -> bool:
        """
        Validate signal against schema.

        Args:
            payload: The signal payload to validate

        Returns:
            True if valid, False otherwise

        Raises:
            ValidationError: If validation fails
        """
        try:
            signal = TradingSignal(**payload)

            # Check TTL - ensure expires_at is in the future
            expires_at = datetime.fromisoformat(
                signal.expires_at_utc.replace("Z", "+00:00")
            )
            now = datetime.now(timezone.utc).replace(tzinfo=expires_at.tzinfo)

            if expires_at <= now:
                logger.warning("Signal has expired: %s", signal.signal_id)
                return False

            logger.debug("Signal validation passed: %s", signal.signal_id)
            return True

        except ValidationError as e:
            logger.error("Signal validation failed: %s", e)
            raise

    async def validate_params_independently(self, leg: OrderLeg) -> bool:
        """
        Validate order leg parameters independently.

        Don't trust core blindly - verify limits and constraints.

        Args:
            leg: The order leg to validate

        Returns:
            True if valid, False otherwise
        """
        # Validate size limits
        if leg.size <= 0 or leg.size > 10000:
            logger.error("Order size out of bounds: %f", leg.size)
            return False

        # Validate price limits
        if leg.order_type == "LIMIT":
            if leg.limit_price is None or leg.limit_price < 0 or leg.limit_price > 1:
                logger.error("Invalid limit price: %s", leg.limit_price)
                return False

        # Verify market exists
        cursor = await self.db_connection.execute(
            "SELECT market_id FROM markets WHERE market_id = ?",
            (leg.market_id,),
        )
        market = await cursor.fetchone()
        if not market:
            logger.error("Market not found: %s", leg.market_id)
            return False

        logger.debug("Parameter validation passed for market: %s", leg.market_id)
        return True

    async def process_signal(self, payload: dict[str, Any]) -> None:
        """
        Process a trading signal end-to-end.

        Flow:
            1. Validate signal schema + TTL
            2. Validate each leg's parameters
            3. Run ALL risk checks (position limit, daily loss, exposure, dedup, min edge)
            4. If all pass → route to exchange clients
            5. If any fail → reject and log

        Args:
            payload: The signal payload
        """
        signal_id = payload.get("signal_id", "unknown")

        try:
            # ── 0. Reconciliation halt check ──
            if (
                self.reconciliation_engine
                and self.reconciliation_engine.trading_halted
            ):
                logger.error(
                    "TRADING HALTED by reconciliation — rejecting signal %s",
                    signal_id,
                )
                await self._log_signal_intent(
                    signal_id,
                    "REJECTED",
                    "Trading halted by reconciliation discrepancy",
                )
                return

            # ── 1. Schema validation ──
            if not await self.validate_signal(payload):
                await self._log_signal_intent(
                    signal_id, "REJECTED", "Signal validation failed"
                )
                return

            signal = TradingSignal(**payload)
            logger.info("Processing signal: %s", signal_id)

            # ── 2. Leg parameter validation ──
            valid_legs = []
            for idx, leg in enumerate(signal.legs):
                if await self.validate_params_independently(leg):
                    valid_legs.append(leg)
                else:
                    logger.warning("Invalid parameters for leg %d", idx)

            if not valid_legs:
                await self._log_signal_intent(signal_id, "REJECTED", "No valid legs")
                return

            # ── 3. Risk checks (ENFORCED) ──
            all_passed, risk_results = await run_all_checks(
                signal=signal,
                risk_config=self.risk_config,
                db=self.db_connection,
            )

            if not all_passed:
                failed = [r for r in risk_results if not r.passed]
                reasons = "; ".join(f"{r.check_type}: {r.detail}" for r in failed)
                logger.warning(
                    "RISK REJECTED signal %s: %s", signal_id, reasons,
                )
                await self._log_signal_intent(
                    signal_id, "RISK_REJECTED", f"Failed checks: {reasons}"
                )
                return

            # ── 4. Route orders ──
            await self._log_signal_intent(
                signal_id, "INITIATED", f"Processing {len(valid_legs)} legs (all risk checks passed)"
            )

            await self.order_router.route_orders(
                signal_id=signal_id,
                legs=valid_legs,
                execution_mode=signal.execution_mode,
                abort_on_partial=signal.abort_on_partial,
                expiry_s=signal.expiry_s,
            )

        except ValidationError as e:
            await self._log_signal_intent(
                signal_id, "REJECTED", f"Validation error: {str(e)}"
            )
            raise
        except Exception as e:
            await self._log_signal_intent(
                signal_id, "ERROR", f"Processing error: {str(e)}"
            )
            logger.error("Error processing signal %s: %s", signal_id, exc_info=e)

    async def _log_signal_intent(
        self,
        signal_id: str,
        status: str,
        details: str,
    ) -> None:
        """
        Log signal processing intent and status.

        Args:
            signal_id: The signal ID
            status: Status of processing
            details: Additional details
        """
        try:
            await self.db_connection.execute(
                """
                INSERT INTO signal_events (signal_id, status, details, timestamp_utc)
                VALUES (?, ?, ?, datetime('now'))
                """,
                (signal_id, status, details),
            )
            await self.db_connection.commit()
            logger.info("Logged signal event: %s - %s", signal_id, status)
        except Exception as e:
            logger.error("Error logging signal intent: %s", exc_info=e)
