"""
Unit tests for signal queue hardening features.

Tests deduplication, DLQ, backpressure, and the unified queue.
"""

import json

import pytest
import redis.asyncio as redis

from core.signals.backpressure import BackpressureMonitor
from core.signals.dedup import SignalDeduplicator
from core.signals.dlq import DeadLetterQueue
from core.signals.queue import HardenedSignalQueue


@pytest.fixture
async def redis_client():
    """Create a test Redis client."""
    client = await redis.from_url("redis://localhost:6379")
    # Flush test data
    await client.flushdb()
    yield client
    await client.close()


class TestSignalDeduplicator:
    """Test the deduplication module."""

    @pytest.mark.asyncio
    async def test_is_duplicate_false_initially(self, redis_client):
        """Signal should not be marked as duplicate initially."""
        dedup = SignalDeduplicator(redis_client)
        is_dup = await dedup.is_duplicate("signal_123")
        assert is_dup is False

    @pytest.mark.asyncio
    async def test_mark_and_check_duplicate(self, redis_client):
        """After marking, signal should be detected as duplicate."""
        dedup = SignalDeduplicator(redis_client)
        signal_id = "signal_456"

        # Mark as processed
        await dedup.mark_processed(signal_id, ttl_seconds=10)

        # Should now be detected as duplicate
        is_dup = await dedup.is_duplicate(signal_id)
        assert is_dup is True

    @pytest.mark.asyncio
    async def test_dedup_cache_size(self, redis_client):
        """Should track cache size correctly."""
        dedup = SignalDeduplicator(redis_client)

        # Add multiple signals
        for i in range(5):
            await dedup.mark_processed(f"signal_{i}")

        size = await dedup.get_cache_size()
        assert size == 5

    @pytest.mark.asyncio
    async def test_cleanup_purges_entries(self, redis_client):
        """Cleanup should remove entries."""
        dedup = SignalDeduplicator(redis_client)

        # Add signals
        for i in range(3):
            await dedup.mark_processed(f"signal_{i}")

        # Cleanup
        deleted = await dedup.cleanup()
        assert deleted == 3

        # Cache should be empty
        size = await dedup.get_cache_size()
        assert size == 0


class TestDeadLetterQueue:
    """Test the dead letter queue module."""

    @pytest.mark.asyncio
    async def test_push_and_pop_single(self, redis_client):
        """Should push and pop signals."""
        dlq = DeadLetterQueue(redis_client)
        signal = {"signal_id": "sig_001", "data": "test"}

        # Push
        result = await dlq.push(signal, error="Test error", retry_count=1)
        assert result is True

        # Pop
        entry = await dlq.pop()
        assert entry is not None
        assert entry["signal_id"] == "sig_001"
        assert entry["error"] == "Test error"
        assert entry["retry_count"] == 1

    @pytest.mark.asyncio
    async def test_list_failed(self, redis_client):
        """Should list failed signals."""
        dlq = DeadLetterQueue(redis_client)

        # Push multiple
        for i in range(3):
            signal = {"signal_id": f"sig_{i:03d}"}
            await dlq.push(signal, error=f"Error {i}", retry_count=i)

        # List
        entries = await dlq.list_failed(limit=10)
        assert len(entries) == 3
        assert entries[0]["signal_id"] == "sig_000"

    @pytest.mark.asyncio
    async def test_get_size(self, redis_client):
        """Should track DLQ size."""
        dlq = DeadLetterQueue(redis_client)

        # Empty
        size = await dlq.get_size()
        assert size == 0

        # Add
        await dlq.push({"signal_id": "sig_1"}, error="err1")
        size = await dlq.get_size()
        assert size == 1

    @pytest.mark.asyncio
    async def test_purge_dlq(self, redis_client):
        """Purge should clear all entries."""
        dlq = DeadLetterQueue(redis_client)

        # Add entries
        for i in range(5):
            await dlq.push({"signal_id": f"sig_{i}"}, error=f"err_{i}")

        # Purge
        deleted = await dlq.purge()
        assert deleted == 5

        # Should be empty
        size = await dlq.get_size()
        assert size == 0


class TestBackpressureMonitor:
    """Test the backpressure monitoring module."""

    @pytest.mark.asyncio
    async def test_check_queue_depth(self, redis_client):
        """Should report queue depth."""
        monitor = BackpressureMonitor(redis_client)

        # Empty queue
        depth = await monitor.check_queue_depth()
        assert depth == 0

        # Add signals
        for i in range(5):
            signal_json = json.dumps({"signal_id": f"sig_{i}"})
            await redis_client.lpush("trading_signals", signal_json)

        depth = await monitor.check_queue_depth()
        assert depth == 5

    @pytest.mark.asyncio
    async def test_is_overloaded(self, redis_client):
        """Should detect overload condition."""
        monitor = BackpressureMonitor(redis_client)

        # Not overloaded
        overloaded = await monitor.is_overloaded(max_depth=100)
        assert overloaded is False

        # Simulate overload by adding signals
        for i in range(150):
            signal_json = json.dumps({"signal_id": f"sig_{i}"})
            await redis_client.lpush("trading_signals", signal_json)

        # Should be overloaded with threshold of 100
        overloaded = await monitor.is_overloaded(max_depth=100)
        assert overloaded is True

    @pytest.mark.asyncio
    async def test_record_signal_processed(self, redis_client):
        """Should record processing metrics."""
        monitor = BackpressureMonitor(redis_client)

        # Record some signals
        await monitor.record_signal_processed("sig_1", 10.5)
        await monitor.record_signal_processed("sig_2", 20.3)

        # Check stats
        stats = await monitor.get_stats()
        assert stats["processed_count"] == 2
        assert abs(stats["avg_latency_ms"] - 15.4) < 0.1


class TestHardenedSignalQueue:
    """Test the unified hardened signal queue."""

    @pytest.mark.asyncio
    async def test_publish_signal(self, redis_client):
        """Should publish a signal."""
        queue = HardenedSignalQueue(redis_client)
        signal = {
            "signal_id": "sig_001",
            "strategy": "test",
            "data": "test_data",
        }

        result = await queue.publish(signal)
        assert result is True

        # Verify in Redis
        depth = await redis_client.llen("trading_signals")
        assert depth == 1

    @pytest.mark.asyncio
    async def test_dedup_prevents_duplicate_publish(self, redis_client):
        """Deduplication should prevent duplicate publishing."""
        queue = HardenedSignalQueue(redis_client)
        signal = {"signal_id": "sig_dup"}

        # First publish should succeed
        result1 = await queue.publish(signal)
        assert result1 is True

        # Duplicate should be rejected
        result2 = await queue.publish(signal)
        assert result2 is False

    @pytest.mark.asyncio
    async def test_health_status(self, redis_client):
        """Should report queue health."""
        queue = HardenedSignalQueue(redis_client)

        # Add some signals
        for i in range(3):
            await queue.publish({"signal_id": f"sig_{i}"})

        health = await queue.health()
        assert health["queue_depth"] == 3
        assert health["dlq_size"] == 0
        assert health["is_overloaded"] is False

    @pytest.mark.asyncio
    async def test_backpressure_rejects_on_overload(self, redis_client):
        """Should reject signals when overloaded."""
        queue = HardenedSignalQueue(
            redis_client,
            backpressure_threshold=5,
        )

        # Fill queue to threshold
        for i in range(6):
            signal_json = json.dumps({"signal_id": f"pre_sig_{i}"})
            await redis_client.lpush("trading_signals", signal_json)

        # Try to publish should be rejected due to backpressure
        signal = {"signal_id": "sig_overload"}
        result = await queue.publish(signal)
        assert result is False

    @pytest.mark.asyncio
    async def test_handle_processing_failure_retries(self, redis_client):
        """Should retry failed signals."""
        queue = HardenedSignalQueue(redis_client, max_retries=2)

        signal = {"signal_id": "sig_fail", "_retry_count": 0}

        # First failure should retry
        retry = await queue.handle_processing_failure(
            signal,
            error="Test error",
        )
        assert retry is True

        # Verify signal is back in queue
        depth = await redis_client.llen("trading_signals")
        assert depth == 1

    @pytest.mark.asyncio
    async def test_handle_processing_failure_dlq(self, redis_client):
        """Should send to DLQ after max retries."""
        queue = HardenedSignalQueue(redis_client, max_retries=2)

        signal = {"signal_id": "sig_max_fail", "_retry_count": 2}

        # Should go to DLQ (max retries exceeded)
        retry = await queue.handle_processing_failure(
            signal,
            error="Max retries exceeded",
        )
        assert retry is False

        # Verify in DLQ
        dlq_size = await queue.dlq.get_size()
        assert dlq_size == 1

    @pytest.mark.asyncio
    async def test_flush_dedup(self, redis_client):
        """Should clear dedup cache."""
        queue = HardenedSignalQueue(redis_client)

        # Mark some as processed
        for i in range(3):
            await queue.deduplicator.mark_processed(f"sig_{i}")

        # Flush
        cleared = await queue.flush_dedup()
        assert cleared == 3

        # Should allow reprocessing now
        is_dup = await queue.deduplicator.is_duplicate("sig_0")
        assert is_dup is False

    @pytest.mark.asyncio
    async def test_retry_dlq(self, redis_client):
        """Should retry all DLQ entries."""
        queue = HardenedSignalQueue(redis_client)

        # Manually add to DLQ
        signal = {"signal_id": "sig_retry"}
        await queue.dlq.push(signal, error="test", retry_count=1)

        # Retry
        count = await queue.retry_dlq()
        assert count == 1

        # Should be back in main queue
        depth = await redis_client.llen("trading_signals")
        assert depth == 1

    @pytest.mark.asyncio
    async def test_purge_dlq(self, redis_client):
        """Should clear all DLQ entries."""
        queue = HardenedSignalQueue(redis_client)

        # Add to DLQ
        for i in range(3):
            await queue.dlq.push(
                {"signal_id": f"sig_{i}"},
                error=f"error_{i}",
            )

        # Purge
        count = await queue.purge_dlq()
        assert count == 3

        # Should be empty
        size = await queue.dlq.get_size()
        assert size == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
