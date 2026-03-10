"""
Signal handler for validating and processing trading signals.

Receives signals from the queue, validates them, and routes them
to the order router for execution.
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional

import aiosqlite
import redis.asyncio as redis
from pydantic import ValidationError

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
    ) -> None:
        """
        Initialize the signal handler.

        Args:
            db_connection: SQLite connection for writing results
            redis_client: Redis client for queue operations
            execution_mode: Execution mode - "live" or "mock" (default: "live")
        """
        self.db_connection = db_connection
        self.redis_client = redis_client
        self.execution_mode = execution_mode
        self.order_router = OrderRouter(db_connection, execution_mode=execution_mode)

    async def validate_signal(self, payload: Dict[str, Any]) -> bool:
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
            expires_at = datetime.fromisoformat(signal.expires_at_utc.replace("Z", "+00:00"))
            now = datetime.utcnow().replace(tzinfo=expires_at.tzinfo)

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

    async def process_signal(self, payload: Dict[str, Any]) -> None:
        """
        Process a trading signal end-to-end.

        Args:
            payload: The signal payload

        Raises:
            ValidationError: If signal validation fails
        """
        signal_id = payload.get("signal_id", "unknown")

        try:
            # Validate signal schema
            if not await self.validate_signal(payload):
                await self._log_signal_intent(signal_id, "REJECTED", "Signal validation failed")
                return

            signal = TradingSignal(**payload)
            logger.info("Processing signal: %s", signal_id)

            # Validate each leg independently
            valid_legs = []
            for idx, leg in enumerate(signal.legs):
                if await self.validate_params_independently(leg):
                    valid_legs.append(leg)
                else:
                    logger.warning("Invalid parameters for leg %d", idx)

            if not valid_legs:
                await self._log_signal_intent(signal_id, "REJECTED", "No valid legs")
                return

            # Log intent to process
            await self._log_signal_intent(
                signal_id, "INITIATED", f"Processing {len(valid_legs)} legs"
            )

            # Route orders to platforms
            await self.order_router.route_orders(
                signal_id=signal_id,
                legs=valid_legs,
                execution_mode=signal.execution_mode,
                abort_on_partial=signal.abort_on_partial,
                expiry_s=signal.expiry_s,
            )

        except ValidationError as e:
            await self._log_signal_intent(signal_id, "REJECTED", f"Validation error: {str(e)}")
            raise
        except Exception as e:
            await self._log_signal_intent(signal_id, "ERROR", f"Processing error: {str(e)}")
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
