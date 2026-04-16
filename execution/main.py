"""
Entry point for the execution service (separate process).

Connects to Redis queue for signal consumption, validates signals,
dispatches to order router, and writes results to shared database.

All configuration is loaded from environment variables via get_config().
Both services share the same DB path and execution mode from config.
"""

import asyncio
import json
import logging
import signal as signal_module
import sys

import aiosqlite
import redis.asyncio as redis
from pydantic import ValidationError

from core.config import get_config
from core.storage.db import Database
from execution.handler import SignalHandler
from execution.state import PositionStateManager

logger = logging.getLogger(__name__)


class ExecutionService:
    """Main execution service that consumes signals from Redis queue."""

    def __init__(self) -> None:
        """Initialize the execution service from global config."""
        self.config = get_config()

        self.redis_client: redis.Redis | None = None
        self.db: Database | None = None
        self.db_connection: aiosqlite.Connection | None = None
        self.signal_handler: SignalHandler | None = None
        self.position_manager: PositionStateManager | None = None

        self.processed_signal_ids: set[str] = set()
        self.running = True

    async def connect(self) -> None:
        """Connect to Redis and SQLite database, run migrations."""
        cfg = self.config

        # Connect to Redis
        logger.info("Connecting to Redis at %s", cfg.redis.redis_url)
        self.redis_client = await redis.from_url(cfg.redis.redis_url)

        # Initialize database with migrations (same path + migrations as core)
        logger.info("Initializing database at %s", cfg.database.db_path)
        self.db = Database(
            cfg.database.db_path,
            migrations_dir=cfg.database.migrations_dir,
        )
        await self.db.init()
        self.db_connection = self.db._conn

        self.signal_handler = SignalHandler(
            self.db_connection,
            self.redis_client,
            execution_mode=cfg.execution.execution_mode,
        )
        self.position_manager = PositionStateManager(self.db_connection)

        logger.info(
            "Execution service connected — mode=%s db=%s",
            cfg.execution.execution_mode,
            cfg.database.db_path,
        )

    async def disconnect(self) -> None:
        """Disconnect from Redis and database."""
        if self.redis_client:
            await self.redis_client.close()
            logger.info("Redis connection closed")

        if self.db:
            await self.db.close()
            logger.info("Database connection closed")

    async def check_duplicate_signal(self, signal_id: str) -> bool:
        """Check if signal has already been processed (idempotency)."""
        if signal_id in self.processed_signal_ids:
            logger.warning("Duplicate signal detected: %s", signal_id)
            return True

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
        """Process a single signal from the queue."""
        try:
            signal_id = payload.get("signal_id")

            if signal_id and await self.check_duplicate_signal(signal_id):
                logger.info("Skipping duplicate signal: %s", signal_id)
                return

            if not self.signal_handler:
                logger.error("Signal handler not initialized")
                return

            await self.signal_handler.process_signal(payload)

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
                self.redis_client = await redis.from_url(self.config.redis.redis_url)
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

        queue_name = self.config.redis.signal_queue_name
        logger.info("Starting signal consumer on queue: %s", queue_name)

        while self.running:
            try:
                result = await self.redis_client.brpop(queue_name, timeout=1)

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
    from dotenv import load_dotenv

    from core.logging_config import configure_from_env

    load_dotenv()
    configure_from_env()

    service = ExecutionService()
    await service.run()


if __name__ == "__main__":
    asyncio.run(main())
