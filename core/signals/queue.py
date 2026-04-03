"""
Hardened signal queue with deduplication, DLQ, and backpressure.

Unifies all signal queue hardening features into a single interface
for publishing and consuming signals with automatic error handling.
"""

import json
import logging
import time
from typing import AsyncIterator, Optional

import redis.asyncio as redis

from .backpressure import BackpressureMonitor
from .dedup import SignalDeduplicator
from .dlq import DeadLetterQueue

logger = logging.getLogger(__name__)


class HardenedSignalQueue:
    """
    Production-hardened signal queue with deduplication, retry, and flow control.

    Features:
    - Automatic deduplication of signals
    - Dead letter queue for failed signals
    - Backpressure monitoring and flow control
    - Automatic retry with configurable limits
    - Comprehensive health monitoring
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        queue_key: str = "trading_signals",
        max_retries: int = 3,
        dedup_ttl_seconds: int = 3600,
        dlq_key: str = "signals:dlq",
        backpressure_threshold: int = 1000,
    ):
        """
        Initialize the hardened signal queue.

        Args:
            redis_client: Redis async client
            queue_key: Redis key for the signal queue
            max_retries: Maximum retry attempts before sending to DLQ
            dedup_ttl_seconds: TTL for deduplication cache
            dlq_key: Redis key for the dead letter queue
            backpressure_threshold: Queue depth threshold for backpressure
        """
        self.redis_client = redis_client
        self.queue_key = queue_key
        self.max_retries = max_retries
        self.dedup_ttl_seconds = dedup_ttl_seconds
        self.backpressure_threshold = backpressure_threshold

        # Initialize sub-components
        self.deduplicator = SignalDeduplicator(
            redis_client,
            key_prefix="dedup",
            default_ttl_seconds=dedup_ttl_seconds,
        )

        self.dlq = DeadLetterQueue(redis_client, queue_key=dlq_key)

        self.backpressure = BackpressureMonitor(
            redis_client,
            signal_queue_key=queue_key,
        )

    async def publish(self, signal: dict) -> bool:
        """
        Publish a signal to the queue with deduplication and backpressure checks.

        Args:
            signal: Signal dict to publish

        Returns:
            True if published successfully, False if rejected
        """
        try:
            signal_id = signal.get("signal_id", "unknown")

            # Check deduplication
            if await self.deduplicator.is_duplicate(signal_id):
                logger.warning(f"Rejecting duplicate signal: {signal_id}")
                return False

            # Check backpressure
            overloaded = await self.backpressure.is_overloaded(self.backpressure_threshold)
            if overloaded:
                logger.warning(f"Queue overloaded, rejecting signal: {signal_id}")
                return False

            # Serialize and push to queue
            signal_json = json.dumps(signal)
            await self.redis_client.lpush(self.queue_key, signal_json)

            # Mark as processed in dedup cache
            await self.deduplicator.mark_processed(
                signal_id,
                ttl_seconds=self.dedup_ttl_seconds,
            )

            logger.info(f"Published signal: {signal_id}")
            return True

        except Exception as e:
            logger.error(f"Error publishing signal: {e}")
            return False

    async def consume(self) -> AsyncIterator[dict]:
        """
        Consume signals from the queue with automatic retry and DLQ handling.

        Yields signals one at a time. On processing failure, automatically
        retries up to max_retries times. After max retries, sends to DLQ.

        Yields:
            Signal dict ready for processing

        Note:
            Caller is responsible for handling each signal and catching
            exceptions. Each yielded signal should be acknowledged or
            an error should be recorded.
        """
        while True:
            try:
                # Blocking pop with timeout
                result = await self.redis_client.brpop(
                    self.queue_key,
                    timeout=1,
                )

                if result is None:
                    continue

                _, signal_json = result

                try:
                    signal = json.loads(signal_json)
                    signal_id = signal.get("signal_id", "unknown")

                    # Record processing start time
                    start_time = time.time()

                    logger.debug(f"Consuming signal: {signal_id}")

                    # Add metadata for retry tracking
                    if "_retry_count" not in signal:
                        signal["_retry_count"] = 0

                    yield signal

                    # Record successful processing time
                    processing_time_ms = (time.time() - start_time) * 1000
                    await self.backpressure.record_signal_processed(
                        signal_id,
                        processing_time_ms,
                    )

                except json.JSONDecodeError as e:
                    logger.error(f"Failed to decode signal JSON: {e}")
                    # Skip malformed signals
                    continue

            except asyncio.CancelledError:
                logger.info("Signal consumer cancelled")
                break
            except (
                redis.ConnectionError,
                redis.TimeoutError,
                ConnectionResetError,
            ) as e:
                logger.error(f"Redis connection error during consume: {e}")
                # Caller should handle reconnection or exit
                raise
            except Exception as e:
                logger.error(f"Unexpected error in consume: {e}", exc_info=True)
                await asyncio.sleep(0.1)

    async def handle_processing_failure(
        self,
        signal: dict,
        error: str,
    ) -> bool:
        """
        Handle a signal processing failure.

        Either retries the signal or sends it to DLQ based on retry count.

        Args:
            signal: The signal that failed
            error: Error message/description

        Returns:
            True if signal was retried, False if sent to DLQ
        """
        try:
            signal_id = signal.get("signal_id", "unknown")
            retry_count = signal.get("_retry_count", 0)

            if retry_count < self.max_retries:
                # Retry: re-queue the signal
                signal["_retry_count"] = retry_count + 1
                signal_json = json.dumps(signal)

                await self.redis_client.lpush(self.queue_key, signal_json)
                logger.warning(
                    f"Signal {signal_id} failed, requeueing for retry "
                    f"({retry_count + 1}/{self.max_retries}): {error[:100]}"
                )

                return True
            else:
                # Send to DLQ: max retries exceeded
                await self.dlq.push(
                    signal,
                    error=error,
                    retry_count=retry_count,
                )
                logger.error(
                    f"Signal {signal_id} failed after {retry_count} retries, "
                    f"sent to DLQ: {error[:100]}"
                )

                return False

        except Exception as e:
            logger.error(f"Error handling processing failure: {e}")
            return False

    async def health(self) -> dict:
        """
        Get comprehensive health status of the signal queue.

        Returns:
            Dict with queue depth, DLQ size, dedup cache size, and backpressure status
        """
        try:
            depth = await self.backpressure.check_queue_depth()
            dlq_size = await self.dlq.get_size()
            dedup_size = await self.deduplicator.get_cache_size()
            is_overloaded = await self.backpressure.is_overloaded(
                self.backpressure_threshold
            )

            stats = await self.backpressure.get_stats()

            health = {
                "queue_depth": depth,
                "dlq_size": dlq_size,
                "dedup_cache_size": dedup_size,
                "is_overloaded": is_overloaded,
                "backpressure_threshold": self.backpressure_threshold,
                "max_retries": self.max_retries,
                "stats": stats,
            }

            return health

        except Exception as e:
            logger.error(f"Error getting queue health: {e}")
            return {
                "error": str(e),
                "queue_depth": 0,
                "dlq_size": 0,
            }

    async def flush_dedup(self) -> int:
        """
        Clear the deduplication cache.

        Allows signals that were recently processed to be reprocessed.
        Use with caution in production.

        Returns:
            Number of entries cleared
        """
        return await self.deduplicator.cleanup()

    async def retry_dlq(self) -> int:
        """
        Retry all signals in the dead letter queue.

        Moves all DLQ entries back to main queue with incremented retry counts.

        Returns:
            Number of signals retried
        """
        return await self.dlq.retry_all(max_queue_key=self.queue_key)

    async def purge_dlq(self) -> int:
        """
        Clear all entries from the dead letter queue.

        This is destructive and cannot be undone.

        Returns:
            Number of entries deleted
        """
        return await self.dlq.purge()


# Async import needed for consume()
import asyncio
