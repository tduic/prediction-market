"""
Example integration of HardenedSignalQueue with the execution service.

This shows how to replace the existing simple queue consumption with
the production-hardened queue implementation.
"""

import asyncio
import json
import logging
from typing import Optional

import aiosqlite
import redis.asyncio as redis
from pydantic import ValidationError

from core.config import get_config
from core.signals.queue import HardenedSignalQueue
from core.storage.db import Database
from execution.handler import SignalHandler
from execution.state import PositionStateManager

logger = logging.getLogger(__name__)


class HardenedExecutionService:
    """
    Execution service using the hardened signal queue.

    This is a drop-in replacement for the basic execution service
    that adds deduplication, DLQ, backpressure, and retry logic.
    """

    def __init__(self) -> None:
        """Initialize the hardened execution service from global config."""
        self.config = get_config()

        self.redis_client: Optional[redis.Redis] = None
        self.db: Optional[Database] = None
        self.db_connection: Optional[aiosqlite.Connection] = None
        self.signal_handler: Optional[SignalHandler] = None
        self.position_manager: Optional[PositionStateManager] = None

        # The hardened queue combines dedup, DLQ, and backpressure
        self.signal_queue: Optional[HardenedSignalQueue] = None

        self.running = True

    async def connect(self) -> None:
        """Connect to Redis and database, initialize hardened queue."""
        cfg = self.config

        # Connect to Redis
        logger.info("Connecting to Redis at %s", cfg.redis.redis_url)
        self.redis_client = await redis.from_url(cfg.redis.redis_url)
        await self.redis_client.ping()

        # Initialize database
        logger.info("Initializing database at %s", cfg.database.db_path)
        self.db = Database(
            cfg.database.db_path,
            migrations_dir=cfg.database.migrations_dir,
        )
        await self.db.init()
        self.db_connection = self.db._conn

        # Initialize signal handler
        self.signal_handler = SignalHandler(
            self.db_connection,
            self.redis_client,
            execution_mode=cfg.execution.execution_mode,
        )

        # Initialize position manager
        self.position_manager = PositionStateManager(self.db_connection)

        # Initialize HARDENED SIGNAL QUEUE
        # This is the key difference from the basic service
        self.signal_queue = HardenedSignalQueue(
            redis_client=self.redis_client,
            queue_key=cfg.redis.signal_queue_name,
            max_retries=cfg.execution.max_order_retries,
            dedup_ttl_seconds=cfg.risk_controls.duplicate_signal_window_s,
            dlq_key="signals:dlq",
            backpressure_threshold=1000,  # Alert/reject if queue > 1000
        )

        logger.info(
            "Hardened execution service connected — "
            "mode=%s db=%s dedup_ttl=%ds max_retries=%d",
            cfg.execution.execution_mode,
            cfg.database.db_path,
            cfg.risk_controls.duplicate_signal_window_s,
            cfg.execution.max_order_retries,
        )

    async def disconnect(self) -> None:
        """Disconnect from Redis and database."""
        if self.redis_client:
            await self.redis_client.close()
            logger.info("Redis connection closed")

        if self.db:
            await self.db.close()
            logger.info("Database connection closed")

    async def process_signal(self, signal: dict) -> bool:
        """
        Process a single signal from the queue.

        Returns:
            True if signal was processed successfully
        """
        signal_id = signal.get("signal_id", "unknown")
        retry_count = signal.get("_retry_count", 0)

        try:
            logger.info(
                f"Processing signal {signal_id} "
                f"(attempt {retry_count + 1}/{self.config.execution.max_order_retries + 1})"
            )

            if not self.signal_handler:
                logger.error("Signal handler not initialized")
                raise RuntimeError("Signal handler not initialized")

            # Process the signal
            await self.signal_handler.process_signal(signal)

            logger.info(f"Signal {signal_id} processed successfully")
            return True

        except ValidationError as e:
            logger.error(f"Validation error for signal {signal_id}: {e}")
            return False
        except Exception as e:
            logger.error(
                f"Error processing signal {signal_id}: {e}",
                exc_info=True,
            )
            return False

    async def _reconnect_redis(self) -> bool:
        """Attempt to reconnect to Redis with exponential backoff."""
        max_attempts = 10
        for attempt in range(max_attempts):
            backoff = min(2**attempt, 30)
            logger.info(
                f"Redis reconnection attempt {attempt + 1}/{max_attempts} "
                f"(backoff: {backoff}s)"
            )
            try:
                if self.redis_client:
                    await self.redis_client.close()

                self.redis_client = await redis.from_url(self.config.redis.redis_url)
                await self.redis_client.ping()

                logger.info("Redis reconnected successfully")
                return True
            except Exception as e:
                logger.warning(f"Redis reconnection failed: {e}")
                await asyncio.sleep(backoff)

        logger.error(f"Failed to reconnect to Redis after {max_attempts} attempts")
        return False

    async def consume_queue(self) -> None:
        """
        Main loop: consume signals from hardened queue and process them.

        Key differences from basic service:
        1. Uses async generator interface (queue.consume())
        2. Automatic retry handling (queue.handle_processing_failure())
        3. Automatic DLQ routing on max retries
        4. Integrated backpressure monitoring
        5. Automatic deduplication
        """
        if not self.signal_queue:
            logger.error("Hardened queue not initialized")
            return

        queue_name = self.config.redis.signal_queue_name
        logger.info(f"Starting hardened signal consumer on queue: {queue_name}")

        while self.running:
            try:
                # Use the hardened queue's async generator interface
                async for signal in self.signal_queue.consume():
                    signal_id = signal.get("signal_id", "unknown")

                    try:
                        # Process the signal
                        success = await self.process_signal(signal)

                        if not success:
                            # Failed signal — automatic retry or DLQ
                            is_retrying = (
                                await self.signal_queue.handle_processing_failure(
                                    signal,
                                    error="Signal processing failed (validation/execution error)",
                                )
                            )

                            if is_retrying:
                                logger.warning(
                                    f"Signal {signal_id} queued for retry "
                                    f"({signal.get('_retry_count', 0)}/{self.config.execution.max_order_retries})"
                                )
                            else:
                                logger.error(
                                    f"Signal {signal_id} exceeded max retries, "
                                    "sent to DLQ for manual review"
                                )

                    except ValidationError as e:
                        logger.error(f"Validation error for signal {signal_id}: {e}")

                        is_retrying = (
                            await self.signal_queue.handle_processing_failure(
                                signal,
                                error=f"Validation error: {str(e)[:100]}",
                            )
                        )

                        if not is_retrying:
                            logger.error(f"Signal {signal_id} sent to DLQ")

                    except Exception as e:
                        logger.error(
                            f"Unexpected error processing signal {signal_id}: {e}",
                            exc_info=True,
                        )

                        is_retrying = (
                            await self.signal_queue.handle_processing_failure(
                                signal,
                                error=f"Unexpected error: {str(e)[:100]}",
                            )
                        )

                        if not is_retrying:
                            logger.error(f"Signal {signal_id} sent to DLQ")

            except asyncio.CancelledError:
                logger.info("Signal consumer cancelled")
                break

            except (
                redis.ConnectionError,
                redis.TimeoutError,
                ConnectionResetError,
            ) as e:
                logger.warning(
                    f"Redis connection lost: {e}. Attempting reconnect..."
                )
                if not await self._reconnect_redis():
                    logger.error("Giving up on Redis reconnection, shutting down")
                    self.running = False

            except Exception as e:
                logger.error(f"Unexpected error in consume loop: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def report_health(self) -> None:
        """
        Periodically report queue health status.

        In production, this would send metrics to a monitoring system.
        """
        while self.running:
            try:
                await asyncio.sleep(60)  # Report every minute

                if not self.signal_queue:
                    continue

                health = await self.signal_queue.health()

                logger.info(
                    f"Queue Health: depth={health['queue_depth']}, "
                    f"dlq_size={health['dlq_size']}, "
                    f"dedup_cache={health['dedup_cache_size']}, "
                    f"overloaded={health['is_overloaded']}"
                )

                # Additional detailed stats
                stats = health.get("stats", {})
                if "processed_count" in stats:
                    logger.debug(
                        f"Processing Stats: "
                        f"processed={stats['processed_count']}, "
                        f"avg_latency={stats.get('avg_latency_ms', 0):.1f}ms, "
                        f"p99_latency={stats.get('p99_latency_ms', 0):.1f}ms"
                    )

            except Exception as e:
                logger.error(f"Error reporting health: {e}")

    async def run(self) -> None:
        """Main entry point for the hardened execution service."""
        import signal as signal_module

        def _shutdown_handler(signum: int, frame) -> None:
            logger.info(f"Received signal {signum}, initiating graceful shutdown")
            self.running = False

        signal_module.signal(signal_module.SIGINT, _shutdown_handler)
        signal_module.signal(signal_module.SIGTERM, _shutdown_handler)

        try:
            await self.connect()

            # Run consumer and health reporter concurrently
            consumer_task = asyncio.create_task(self.consume_queue())
            health_task = asyncio.create_task(self.report_health())

            # Wait for either to fail (consumer typically runs forever)
            done, pending = await asyncio.wait(
                [consumer_task, health_task],
                return_when=asyncio.FIRST_EXCEPTION,
            )

            # Cancel remaining tasks
            for task in pending:
                task.cancel()

        except Exception as e:
            logger.error(f"Fatal error in execution service: {e}", exc_info=True)
        finally:
            await self.disconnect()
            logger.info("Hardened execution service shutdown complete")


async def main() -> None:
    """Main entry point."""
    from dotenv import load_dotenv
    from core.logging_config import configure_from_env

    load_dotenv()
    configure_from_env()

    service = HardenedExecutionService()
    await service.run()


if __name__ == "__main__":
    asyncio.run(main())
