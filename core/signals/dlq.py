"""
Dead Letter Queue (DLQ) for failed signal processing.

Captures signals that fail processing with error context,
enabling manual review, analysis, and retry capabilities.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)


class DeadLetterQueue:
    """
    Manages failed signals in a Redis list (DLQ).

    Features:
    - Stores original signal data with error context
    - Tracks retry counts and timestamps
    - Supports batch operations (list, retry, purge)
    - Maintains FIFO ordering for oldest-first retry
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        queue_key: str = "signals:dlq",
    ):
        """
        Initialize the dead letter queue.

        Args:
            redis_client: Redis async client
            queue_key: Redis list key for DLQ
        """
        self.redis_client = redis_client
        self.queue_key = queue_key

    async def push(
        self,
        signal: dict,
        error: str,
        retry_count: int = 0,
    ) -> bool:
        """
        Push a failed signal to the DLQ.

        Stores the original signal data along with error info and metadata.

        Args:
            signal: Original signal dict
            error: Error message/description
            retry_count: Number of retry attempts made

        Returns:
            True if successful, False otherwise
        """
        try:
            dlq_entry = {
                "signal_id": signal.get("signal_id", "unknown"),
                "signal_data": signal,
                "error": error,
                "retry_count": retry_count,
                "failed_at": datetime.now(timezone.utc).isoformat(),
            }

            # Serialize to JSON and push to list
            entry_json = json.dumps(dlq_entry)
            await self.redis_client.rpush(self.queue_key, entry_json)

            logger.info(
                f"Pushed signal {dlq_entry['signal_id']} to DLQ "
                f"(retries: {retry_count}, error: {error[:100]})"
            )

            return True

        except Exception as e:
            logger.error(f"Error pushing to DLQ: {e}")
            return False

    async def pop(self) -> Optional[dict]:
        """
        Retrieve the oldest failed signal from the DLQ.

        Uses LPOP for FIFO (oldest first) behavior.

        Returns:
            DLQ entry dict or None if empty
        """
        try:
            entry_json = await self.redis_client.lpop(self.queue_key)

            if entry_json is None:
                return None

            # Decode and parse
            entry = json.loads(entry_json)
            logger.debug(f"Popped signal {entry.get('signal_id')} from DLQ")

            return entry

        except json.JSONDecodeError as e:
            logger.error(f"Error decoding DLQ entry: {e}")
            return None
        except Exception as e:
            logger.error(f"Error popping from DLQ: {e}")
            return None

    async def list_failed(self, limit: int = 50) -> list[dict]:
        """
        List the most recent failed signals in the DLQ.

        Uses LRANGE to peek without removing entries.
        Shows oldest entries first (index 0).

        Args:
            limit: Maximum number of entries to return

        Returns:
            List of DLQ entry dicts (empty list if DLQ is empty)
        """
        try:
            # LRANGE from 0 to (limit-1) for FIFO ordering
            entries_json = await self.redis_client.lrange(
                self.queue_key,
                0,
                limit - 1,
            )

            if not entries_json:
                return []

            entries = []
            for entry_json in entries_json:
                try:
                    entry = json.loads(entry_json)
                    entries.append(entry)
                except json.JSONDecodeError as e:
                    logger.warning(f"Skipping malformed DLQ entry: {e}")
                    continue

            logger.debug(f"Listed {len(entries)} failed signals from DLQ")
            return entries

        except Exception as e:
            logger.error(f"Error listing failed signals: {e}")
            return []

    async def retry_all(self, max_queue_key: str = "trading_signals") -> int:
        """
        Move all DLQ items back to the main signal queue for retry.

        This is a manual operation for admin retry of failed signals.
        Each signal's retry_count is incremented.

        Args:
            max_queue_key: Redis key for main signal queue

        Returns:
            Number of signals moved back
        """
        try:
            moved_count = 0

            while True:
                entry = await self.pop()

                if entry is None:
                    break

                # Increment retry count and push back to main queue
                entry["retry_count"] += 1
                signal_data = entry.get("signal_data", {})

                # Serialize signal (as JSON string) for queue
                signal_json = json.dumps(signal_data)
                await self.redis_client.lpush(max_queue_key, signal_json)

                moved_count += 1
                logger.info(
                    f"Retrying signal {entry.get('signal_id')} "
                    f"(attempt {entry['retry_count']})"
                )

            if moved_count > 0:
                logger.info(f"Moved {moved_count} signals from DLQ to main queue")

            return moved_count

        except Exception as e:
            logger.error(f"Error during DLQ retry_all: {e}")
            return 0

    async def purge(self) -> int:
        """
        Clear all entries from the DLQ.

        This is a destructive operation and cannot be undone.

        Returns:
            Number of entries deleted
        """
        try:
            # Get the count before deleting (redis DELETE returns key count, not list length)
            deleted_count = await self.redis_client.llen(self.queue_key)
            await self.redis_client.delete(self.queue_key)
            logger.warning(f"Purged {deleted_count} entries from DLQ")
            return deleted_count

        except Exception as e:
            logger.error(f"Error purging DLQ: {e}")
            return 0

    async def get_size(self) -> int:
        """
        Get the number of entries in the DLQ.

        Returns:
            Number of failed signals in queue
        """
        try:
            size = await self.redis_client.llen(self.queue_key)
            return size

        except Exception as e:
            logger.error(f"Error getting DLQ size: {e}")
            return 0

    async def get_stats(self) -> dict:
        """
        Get statistics about the DLQ.

        Returns:
            Dict with size, oldest entry info, etc.
        """
        try:
            size = await self.get_size()

            stats = {
                "dlq_size": size,
                "queue_key": self.queue_key,
            }

            # Get oldest entry if available
            if size > 0:
                oldest_entries = await self.list_failed(limit=1)
                if oldest_entries:
                    oldest = oldest_entries[0]
                    stats["oldest_signal_id"] = oldest.get("signal_id")
                    stats["oldest_failed_at"] = oldest.get("failed_at")
                    stats["oldest_retry_count"] = oldest.get("retry_count")

            return stats

        except Exception as e:
            logger.error(f"Error getting DLQ stats: {e}")
            return {"dlq_size": 0, "error": str(e)}
