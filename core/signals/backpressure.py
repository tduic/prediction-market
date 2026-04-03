"""
Backpressure monitoring for signal queue.

Detects queue overload conditions and provides metrics for flow control.
Helps prevent system cascade failures under high signal volume.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)


class BackpressureMonitor:
    """
    Monitors signal queue depth and applies backpressure when overloaded.

    Features:
    - Real-time queue depth checking
    - Configurable overload threshold
    - Processing rate and latency tracking
    - Warning and throttling capabilities
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        signal_queue_key: str = "trading_signals",
        processing_stats_key: str = "signal_processing_stats",
    ):
        """
        Initialize the backpressure monitor.

        Args:
            redis_client: Redis async client
            signal_queue_key: Redis key for the main signal queue
            processing_stats_key: Redis key for storing processing statistics
        """
        self.redis_client = redis_client
        self.signal_queue_key = signal_queue_key
        self.processing_stats_key = processing_stats_key

    async def check_queue_depth(self) -> int:
        """
        Get the current depth of the signal queue.

        Returns:
            Number of signals waiting in the queue
        """
        try:
            depth = await self.redis_client.llen(self.signal_queue_key)
            return max(0, depth)  # LLEN returns -1 if key doesn't exist
        except Exception as e:
            logger.error(f"Error checking queue depth: {e}")
            return 0

    async def is_overloaded(self, max_depth: int = 1000) -> bool:
        """
        Check if the signal queue is overloaded.

        Args:
            max_depth: Threshold above which queue is considered overloaded

        Returns:
            True if current depth exceeds threshold
        """
        try:
            depth = await self.check_queue_depth()
            is_overloaded = depth > max_depth

            if is_overloaded:
                logger.warning(
                    f"Signal queue overloaded: {depth} > {max_depth} threshold"
                )
            else:
                logger.debug(f"Queue depth normal: {depth}/{max_depth}")

            return is_overloaded

        except Exception as e:
            logger.error(f"Error checking overload status: {e}")
            return False

    async def apply_backpressure(
        self,
        max_depth: int = 1000,
        pause_signal_generation: bool = True,
    ) -> dict:
        """
        Apply backpressure when queue is overloaded.

        This is typically called by signal generators to decide
        whether to continue generating signals or slow down.

        Args:
            max_depth: Overload threshold
            pause_signal_generation: If True, log a warning to pause generation

        Returns:
            Dict with backpressure action details
        """
        try:
            depth = await self.check_queue_depth()
            overloaded = depth > max_depth

            result = {
                "current_depth": depth,
                "threshold": max_depth,
                "overloaded": overloaded,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "action": None,
            }

            if overloaded:
                if pause_signal_generation:
                    result["action"] = "PAUSE_SIGNAL_GENERATION"
                    logger.warning(
                        f"Backpressure applied: pausing signal generation "
                        f"(queue depth: {depth}/{max_depth})"
                    )
                else:
                    result["action"] = "THROTTLE"
                    logger.warning(
                        f"Backpressure applied: throttling signals "
                        f"(queue depth: {depth}/{max_depth})"
                    )
            else:
                result["action"] = "CONTINUE"
                logger.debug(f"No backpressure needed (queue depth: {depth}/{max_depth})")

            return result

        except Exception as e:
            logger.error(f"Error applying backpressure: {e}")
            return {
                "error": str(e),
                "action": "ERROR",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    async def record_signal_processed(
        self,
        signal_id: str,
        processing_time_ms: float,
    ) -> bool:
        """
        Record that a signal was successfully processed.

        Tracks processing statistics for metrics calculation.

        Args:
            signal_id: The signal ID that was processed
            processing_time_ms: Time taken to process in milliseconds

        Returns:
            True if recorded successfully
        """
        try:
            # Store in a hash for aggregation
            stats_hash_key = f"{self.processing_stats_key}:latencies"

            # Store latency for this signal (for percentile calculations)
            # Use a limited-size FIFO list to avoid unbounded growth
            await self.redis_client.lpush(
                f"{self.processing_stats_key}:latency_history",
                processing_time_ms,
            )

            # Trim to keep only last 1000 values
            await self.redis_client.ltrim(
                f"{self.processing_stats_key}:latency_history",
                0,
                999,
            )

            # Update counter and sum for avg calculation
            await self.redis_client.hincrby(
                stats_hash_key,
                "processed_count",
                1,
            )
            await self.redis_client.hincrbyfloat(
                stats_hash_key,
                "total_latency_ms",
                processing_time_ms,
            )

            return True

        except Exception as e:
            logger.error(f"Error recording signal processing: {e}")
            return False

    async def get_stats(self) -> dict:
        """
        Get comprehensive queue and processing statistics.

        Returns:
            Dict with queue depth, rates, latencies, etc.
        """
        try:
            depth = await self.check_queue_depth()
            overloaded = depth > 1000

            stats = {
                "queue_depth": depth,
                "is_overloaded": overloaded,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            # Get processing stats
            try:
                stats_hash_key = f"{self.processing_stats_key}:latencies"
                stats_data = await self.redis_client.hgetall(stats_hash_key)

                if stats_data:
                    processed_count = int(stats_data.get(b"processed_count", 0))
                    total_latency = float(stats_data.get(b"total_latency_ms", 0))

                    stats["processed_count"] = processed_count
                    stats["avg_latency_ms"] = (
                        total_latency / processed_count if processed_count > 0 else 0
                    )

                # Get latency history for percentiles
                latency_history_key = f"{self.processing_stats_key}:latency_history"
                latencies = await self.redis_client.lrange(latency_history_key, 0, -1)

                if latencies:
                    latencies_float = [float(l) for l in latencies]
                    latencies_float.sort()

                    stats["p50_latency_ms"] = latencies_float[len(latencies_float) // 2]
                    stats["p95_latency_ms"] = latencies_float[
                        int(len(latencies_float) * 0.95)
                    ]
                    stats["p99_latency_ms"] = latencies_float[
                        int(len(latencies_float) * 0.99)
                    ]
                    stats["max_latency_ms"] = max(latencies_float)
                    stats["min_latency_ms"] = min(latencies_float)

            except (KeyError, ValueError, IndexError) as e:
                logger.debug(f"Could not calculate detailed stats: {e}")

            return stats

        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            return {
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

    async def reset_stats(self) -> bool:
        """
        Reset all processing statistics.

        Useful for testing or clearing old data.

        Returns:
            True if successful
        """
        try:
            stats_hash_key = f"{self.processing_stats_key}:latencies"
            latency_history_key = f"{self.processing_stats_key}:latency_history"

            await self.redis_client.delete(stats_hash_key)
            await self.redis_client.delete(latency_history_key)

            logger.info("Reset all processing statistics")
            return True

        except Exception as e:
            logger.error(f"Error resetting stats: {e}")
            return False
