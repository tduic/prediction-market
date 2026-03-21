"""
Order router that dispatches orders to platform-specific clients.

Routes orders to Polymarket or Kalshi based on configuration,
handles retry logic, and manages execution mode (simultaneous vs sequential).
"""

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

import aiosqlite

from execution.models import OrderLeg

logger = logging.getLogger(__name__)

MAX_ORDER_RETRIES = 3
RETRY_BACKOFF_BASE_S = 1


class ExecutionMode(str, Enum):
    """Order execution mode."""

    SIMULTANEOUS = "simultaneous"
    SEQUENTIAL = "sequential"


@dataclass
class OrderResult:
    """Result of order submission."""

    order_id: str
    leg_index: int
    platform: str
    status: str  # "ACCEPTED", "REJECTED", "PENDING"
    submission_latency_ms: int
    fill_latency_ms: Optional[int] = None
    error_message: Optional[str] = None


class OrderRouter:
    """Routes orders to platform-specific clients and manages execution."""

    def __init__(
        self, db_connection: aiosqlite.Connection, execution_mode: str = "live"
    ) -> None:
        """
        Initialize the order router.

        Args:
            db_connection: SQLite connection for writing results
            execution_mode: "live" for real clients, "mock" for mock clients
        """
        self.db_connection = db_connection
        self.execution_mode = execution_mode

        if execution_mode == "mock":
            logger.info("Initializing router in MOCK execution mode")
            from execution.clients.mock import MockExecutionClient, MockConfig

            mock_config = MockConfig.from_env()
            self.polymarket_client = MockExecutionClient(
                db_connection=db_connection,
                config=mock_config,
                platform_label="mock_polymarket",
            )
            self.kalshi_client = MockExecutionClient(
                db_connection=db_connection,
                config=mock_config,
                platform_label="mock_kalshi",
            )
        else:
            logger.info("Initializing router in LIVE execution mode")
            from execution.clients.polymarket import PolymarketExecutionClient
            from execution.clients.kalshi import KalshiExecutionClient

            self.polymarket_client = PolymarketExecutionClient(
                db_connection=db_connection,
                # Credentials loaded from env vars inside the client
            )
            self.kalshi_client = KalshiExecutionClient(
                db_connection=db_connection,
                # Credentials loaded from env vars inside the client
            )

    async def route_order(
        self,
        leg: OrderLeg,
        leg_index: int,
        signal_id: str,
    ) -> OrderResult:
        """
        Route a single order to the appropriate platform with retry logic.

        Args:
            leg: The order leg to execute
            leg_index: Index of the leg in the order
            signal_id: The signal ID for tracking

        Returns:
            OrderResult with execution details
        """
        platform = leg.platform.lower()
        client = (
            self.polymarket_client if platform == "polymarket" else self.kalshi_client
        )

        # Retry logic with exponential backoff
        for attempt in range(MAX_ORDER_RETRIES):
            try:
                logger.info(
                    "Submitting order to %s (attempt %d/%d): signal=%s, market=%s",
                    platform,
                    attempt + 1,
                    MAX_ORDER_RETRIES,
                    signal_id,
                    leg.market_id,
                )

                result = await client.submit_order(leg)

                # Log successful submission
                await self._log_order_event(
                    signal_id=signal_id,
                    order_id=result.order_id,
                    leg_index=leg_index,
                    status="ACCEPTED",
                    details=f"Submitted to {platform}",
                )

                return result

            except Exception as e:
                logger.warning(
                    "Order submission attempt %d failed for %s: %s",
                    attempt + 1,
                    leg.market_id,
                    e,
                )

                if attempt < MAX_ORDER_RETRIES - 1:
                    backoff = RETRY_BACKOFF_BASE_S * (2**attempt)
                    logger.info("Retrying in %d seconds", backoff)
                    await asyncio.sleep(backoff)
                else:
                    # All retries exhausted
                    await self._log_order_event(
                        signal_id=signal_id,
                        order_id=f"FAILED-{leg.market_id}",
                        leg_index=leg_index,
                        status="REJECTED",
                        details=f"Failed after {MAX_ORDER_RETRIES} attempts: {str(e)}",
                    )

                    return OrderResult(
                        order_id=f"FAILED-{leg.market_id}",
                        leg_index=leg_index,
                        platform=platform,
                        status="REJECTED",
                        submission_latency_ms=0,
                        error_message=str(e),
                    )

        # Should not reach here
        return OrderResult(
            order_id=f"FAILED-{leg.market_id}",
            leg_index=leg_index,
            platform=platform,
            status="REJECTED",
            submission_latency_ms=0,
            error_message="Unknown error",
        )

    async def route_orders(
        self,
        signal_id: str,
        legs: List[OrderLeg],
        execution_mode: str = "simultaneous",
        abort_on_partial: bool = False,
        expiry_s: int = 300,
    ) -> None:
        """
        Route multiple orders with specified execution mode.

        Args:
            signal_id: The signal ID for tracking
            legs: List of order legs to execute
            execution_mode: "simultaneous" or "sequential"
            abort_on_partial: Cancel all orders if any leg fails
            expiry_s: Seconds to keep positions open
        """
        results: List[OrderResult] = []

        try:
            if execution_mode == ExecutionMode.SIMULTANEOUS.value:
                results = await self._execute_simultaneous(signal_id, legs)
            else:
                results = await self._execute_sequential(signal_id, legs)

            # Check for partial fills if abort_on_partial is set
            if abort_on_partial:
                failed_legs = [r for r in results if r.status == "REJECTED"]
                if failed_legs:
                    logger.warning(
                        "Partial fill detected, aborting all orders: signal=%s",
                        signal_id,
                    )
                    await self._cancel_all_orders(
                        signal_id, [r for r in results if r.status == "ACCEPTED"]
                    )

        except Exception as e:
            logger.error(
                "Error routing orders for signal %s: %s", signal_id, exc_info=e
            )

    async def _execute_simultaneous(
        self,
        signal_id: str,
        legs: List[OrderLeg],
    ) -> List[OrderResult]:
        """
        Execute all legs simultaneously using asyncio.gather.

        Args:
            signal_id: The signal ID for tracking
            legs: List of order legs

        Returns:
            List of OrderResults
        """
        logger.info("Executing %d legs simultaneously", len(legs))

        tasks = [self.route_order(leg, idx, signal_id) for idx, leg in enumerate(legs)]

        results = await asyncio.gather(*tasks, return_exceptions=False)
        return results

    async def _execute_sequential(
        self,
        signal_id: str,
        legs: List[OrderLeg],
    ) -> List[OrderResult]:
        """
        Execute legs sequentially: leg A first, then B on fill.

        Args:
            signal_id: The signal ID for tracking
            legs: List of order legs

        Returns:
            List of OrderResults
        """
        logger.info("Executing %d legs sequentially", len(legs))

        results: List[OrderResult] = []

        for idx, leg in enumerate(legs):
            result = await self.route_order(leg, idx, signal_id)
            results.append(result)

            # Check if this leg filled
            if result.status != "ACCEPTED":
                logger.warning(
                    "Sequential execution halted: leg %d rejected",
                    idx,
                )
                break

            # Wait a bit between orders
            if idx < len(legs) - 1:
                await asyncio.sleep(0.5)

        return results

    async def _cancel_all_orders(
        self,
        signal_id: str,
        results: List[OrderResult],
    ) -> None:
        """
        Cancel all filled orders (abort_on_partial logic).

        Args:
            signal_id: The signal ID for tracking
            results: List of successful OrderResults to cancel
        """
        logger.info("Cancelling %d orders for signal %s", len(results), signal_id)

        cancel_tasks = []
        for result in results:
            if result.platform == "polymarket":
                cancel_tasks.append(
                    self.polymarket_client.cancel_order(result.order_id)
                )
            else:
                cancel_tasks.append(self.kalshi_client.cancel_order(result.order_id))

        if cancel_tasks:
            await asyncio.gather(*cancel_tasks, return_exceptions=True)

    async def _log_order_event(
        self,
        signal_id: str,
        order_id: str,
        leg_index: int,
        status: str,
        details: str,
    ) -> None:
        """
        Log an order event to the database.

        Args:
            signal_id: The signal ID
            order_id: The order ID
            leg_index: The leg index
            status: The order status
            details: Additional details
        """
        try:
            await self.db_connection.execute(
                """
                INSERT INTO order_events
                (signal_id, order_id, leg_index, status, details, timestamp_utc)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                """,
                (signal_id, order_id, leg_index, status, details),
            )
            await self.db_connection.commit()
        except Exception as e:
            logger.error("Error logging order event: %s", exc_info=e)
