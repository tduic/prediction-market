"""
Message deduplication for trading signals using Redis SET with TTL.

Prevents duplicate signal processing within a configurable time window.
Uses Redis SET operations for O(1) lookups with automatic TTL expiration.
"""

import logging
from typing import Optional

import redis.asyncio as redis

logger = logging.getLogger(__name__)


class SignalDeduplicator:
    """
    Deduplicates trading signals using Redis SET with configurable TTL.

    Features:
    - O(1) duplicate checks using Redis SET membership
    - Automatic expiration via TTL
    - Manual purge capability for testing
    - Configurable TTL per signal
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        key_prefix: str = "dedup",
        default_ttl_seconds: int = 3600,
    ):
        """
        Initialize the signal deduplicator.

        Args:
            redis_client: Redis async client
            key_prefix: Redis key prefix (full key will be "{key_prefix}:{signal_id}")
            default_ttl_seconds: Default TTL for dedup cache entries
        """
        self.redis_client = redis_client
        self.key_prefix = key_prefix
        self.default_ttl_seconds = default_ttl_seconds

    def _make_key(self, signal_id: str) -> str:
        """
        Create a Redis key for a signal.

        Args:
            signal_id: The signal ID

        Returns:
            Formatted Redis key
        """
        return f"{self.key_prefix}:{signal_id}"

    async def is_duplicate(self, signal_id: str) -> bool:
        """
        Check if a signal has already been processed.

        Args:
            signal_id: The signal ID to check

        Returns:
            True if signal was previously marked as processed, False otherwise
        """
        try:
            key = self._make_key(signal_id)
            exists = await self.redis_client.exists(key)

            if exists:
                logger.debug(f"Duplicate signal detected: {signal_id}")
                return True

            return False
        except Exception as e:
            logger.error(f"Error checking if signal is duplicate: {e}")
            # Fail open: if we can't check, don't block the signal
            return False

    async def mark_processed(
        self,
        signal_id: str,
        ttl_seconds: Optional[int] = None,
    ) -> bool:
        """
        Mark a signal as processed.

        Adds the signal ID to the dedup cache with TTL.
        After TTL expires, the signal can be processed again.

        Args:
            signal_id: The signal ID to mark as processed
            ttl_seconds: TTL in seconds (uses default if not provided)

        Returns:
            True if successful, False otherwise
        """
        try:
            key = self._make_key(signal_id)
            ttl = ttl_seconds or self.default_ttl_seconds

            # Use SET with EX for atomic set + TTL operation
            # NX means only set if not exists (for idempotency)
            result = await self.redis_client.set(
                key,
                "1",
                ex=ttl,
                nx=True,
            )

            if result:
                logger.debug(f"Marked signal as processed: {signal_id} (TTL: {ttl}s)")
                return True
            else:
                logger.debug(f"Signal already marked as processed: {signal_id}")
                return False

        except Exception as e:
            logger.error(f"Error marking signal as processed: {e}")
            return False

    async def cleanup(self, pattern: str = "*") -> int:
        """
        Manually purge dedup cache entries.

        Note: Redis TTL handles automatic cleanup, but this allows
        manual purge for testing or admin operations.

        Args:
            pattern: Key pattern to match (default "*" clears all dedup keys)

        Returns:
            Number of keys deleted
        """
        try:
            full_pattern = f"{self.key_prefix}:{pattern}"

            # Use SCAN to avoid blocking with KEYS
            cursor = 0
            deleted_count = 0

            while True:
                cursor, keys = await self.redis_client.scan(
                    cursor,
                    match=full_pattern,
                    count=100,
                )

                if keys:
                    deleted_count += await self.redis_client.delete(*keys)

                if cursor == 0:
                    break

            if deleted_count > 0:
                logger.info(f"Purged {deleted_count} dedup cache entries")

            return deleted_count

        except Exception as e:
            logger.error(f"Error purging dedup cache: {e}")
            return 0

    async def get_cache_size(self) -> int:
        """
        Get the number of entries currently in the dedup cache.

        Returns:
            Number of cached signal IDs
        """
        try:
            pattern = f"{self.key_prefix}:*"
            cursor = 0
            count = 0

            while True:
                cursor, keys = await self.redis_client.scan(
                    cursor,
                    match=pattern,
                    count=100,
                )
                count += len(keys)

                if cursor == 0:
                    break

            return count

        except Exception as e:
            logger.error(f"Error getting cache size: {e}")
            return 0
