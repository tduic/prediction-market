"""
Entry point for the execution service (separate process).

Connects to Redis queue for signal consumption, validates signals,
dispatches to order router, and writes results to shared database.
"""

import asyncio
import json
import logging
import signal as signal_module
import sys
from typing import Optional, Set

import aiosqlite
import redis.asyncio as redis
from pydantic import ValidationError

from execution.handler import SignalHandler
from execution.state import PositionStateManager

logger = logging.getLogger(__name__)


class ExecutionService:
    """Main execution service that consumes signals from Redis queue."""

    def __init__(
        self,
        redis_url: str,
        signal_queue_name: str,
        db_path: str,
        max_retries: int = 3,
        execution_mode: str = "live",
    ) -> None:
        """
        Initialize the execution service.

        Args:
            redis_url: Redis connection URL
            signal_queue_name: Name of the Redis queue for signals
            db_path: Path to the shared SQLite database
            max_retries: Maximum retries for queue operations
            execution_mode: Execution mode - "live" or "mock" (default: "live")
        """
        self.redis_url = redis_url
        self.signal_queue_name = signal_queue_name
        self.db_path = db_path
        self.max_retries = max_retries
        self.execution_mode = execution_mode

        self.redis_client: Optional[redis.Redis] = None
        self.db_connection: Optional[aiosqlite.Connection] = None
        self.signal_handler: Optional[SignalHandler] = None
        self.position_manager: Optional[PositionStateManager] = None

        self.processed_signal_ids: Set[str] = set()
        self.running = True

    async def connect(self) -> None:
        """Connect to Redis and SQLite database."""
        logger.info("Connecting to Redis at %s", self.redis_url)
        self.redis_client = await redis.from_url(self.redis_url)

        logger.info("Opening SQLite database at %s", self.db_path)
        self.db_connection = await aiosqlite.connect(self.db_path)

        self.signal_handler = SignalHandler(
            self.db_connection, self.redis_client, execution_mode=self.execution_mode
        )
        self.position_manager = PositionStateManager(self.db_connection)

        logger.info("Execution service running in %s mode", self.execution_mode)
        logger.info("Execution service connected successfully")

    async def disconnect(self) -> None:
        """Disconnect from Redis and database."""
        if self.redis_client:
            await self.redis_client.close()
            logger.info("Redis connection closed")

        if self.db_connection:
            await self.db_connection.close()
            logger.info("Database connection closed")

    async def check_duplicate_signal(self, signal_id: str) -> bool:
        """
        Check if signal has already been processed (idempotency).

        Args:
            signal_id: The signal ID to check

        Returns:
            True if signal has been processed, False otherwise
        """
        if signal_id in self.processed_signal_ids:
            logger.warning("Duplicate signal detected: %s", signal_id)
            return True

        # Check database for previous processing
        if self.db_connection:
            cursor = await self.db_connection.execute(
                "SELECT COUNT(*) FROM order_events WHERE signal_id = ?",
                (signal_id,),
            )
            row = await cursor.fetchone()
            if row and row[0] > 0:
                logger.warning("Signal previously processed in database: %s", signal_id)
                return True

        return False

    async def process_signal(self, payload: dict) -> None:
        """
        Process a single signal from the queue.

        Args:
            payload: The signal payload
        """
        try:
            signal_id = payload.get("signal_id")

            # Check for duplicates (idempotency)
            if signal_id and await self.check_duplicate_signal(signal_id):
                logger.info("Skipping duplicate signal: %s", signal_id)
                return

            # Process the signal
            if not self.signal_handler:
                logger.error("Signal handler not initialized")
                return

            await self.signal_handler.process_signal(payload)

            # Track processed signal
            if signal_id:
                self.processed_signal_ids.add(signal_id)
                logger.info("Signal processed successfully: %s", signal_id)

        except ValidationError as e:
            logger.error("Signal validation error: %s", e)
        except Exception as e:
            logger.error("Error processing signal: %s", exc_info=e)

    async def _reconnect_redis(self) -> bool:
        """Attempt to reconnect to Redis with exponential backoff."""
        max_attempts = 10
        for attempt in range(max_attempts):
            backoff = min(2**attempt, 30)
            logger.info(
                "Redis reconnection attempt %d/%d (backoff: %ds)",
                attempt + 1,
                max_attempts,
                backoff,
            )
            try:
                if self.redis_client:
                    await self.redis_client.close()
                self.redis_client = await redis.from_url(self.redis_url)
                await self.redis_client.ping()
                logger.info("Redis reconnected successfully")
                return True
            except Exception as e:
                logger.warning("Redis reconnection failed: %s", e)
                await asyncio.sleep(backoff)

        logger.error("Failed to reconnect to Redis after %d attempts", max_attempts)
        return False

    async def consume_queue(self) -> None:
        """Main loop: consume signals from Redis queue and process them."""
        if not self.redis_client:
            logger.error("Redis client not initialized")
            return

        logger.info("Starting signal consumer on queue: %s", self.signal_queue_name)

        while self.running:
            try:
                # BRPOP blocks with 1-second timeout to allow graceful shutdown
                result = await self.redis_client.brpop(
                    self.signal_queue_name, timeout=1
                )

                if result is None:
                    continue

                _, signal_json = result

                try:
                    payload = json.loads(signal_json)
                    logger.debug("Received signal: %s", payload.get("signal_id"))
                    await self.process_signal(payload)
                except json.JSONDecodeError as e:
                    logger.error("Failed to decode signal JSON: %s", e)

            except asyncio.CancelledError:
                logger.info("Signal consumer cancelled")
                break
            except (
                redis.ConnectionError,
                redis.TimeoutError,
                ConnectionResetError,
            ) as e:
                logger.warning("Redis connection lost: %s. Attempting reconnect...", e)
                if not await self._reconnect_redis():
                    logger.error("Giving up on Redis reconnection, shutting down")
                    self.running = False
            except Exception as e:
                logger.error("Error consuming from queue: %s", e, exc_info=True)
                await asyncio.sleep(1)

    def _shutdown_handler(self, signum: int, frame) -> None:
        """Handle shutdown signals."""
        logger.info("Received signal %d, initiating graceful shutdown", signum)
        self.running = False

    async def run(self) -> None:
        """Main entry point for the execution service."""
        # Register signal handlers
        signal_module.signal(signal_module.SIGINT, self._shutdown_handler)
        signal_module.signal(signal_module.SIGTERM, self._shutdown_handler)

        try:
            await self.connect()
            await self.consume_queue()
        except Exception as e:
            logger.error("Fatal error in execution service: %s", exc_info=e)
            sys.exit(1)
        finally:
            await self.disconnect()
            logger.info("Execution service shutdown complete")


async def main() -> None:
    """Main entry point."""
    import os
    from core.logging_config import configure_from_env

    configure_from_env()

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    signal_queue_name = os.getenv("SIGNAL_QUEUE_NAME", "trading_signals")
    db_path = os.getenv("DB_PATH", "prediction_market.db")
    execution_mode = os.getenv("EXECUTION_MODE", "mock")

    service = ExecutionService(
        redis_url=redis_url,
        signal_queue_name=signal_queue_name,
        db_path=db_path,
        execution_mode=execution_mode,
    )

    await service.run()


if __name__ == "__main__":
    asyncio.run(main())
