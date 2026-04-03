# Signal Queue Hardening

This document describes the hardening features added to the trading signal queue to improve reliability, observability, and operational control.

## Overview

The signal queue has been enhanced with four production-ready features:

1. **Deduplication (dedup.py)** — Prevents duplicate signal processing
2. **Dead Letter Queue (dlq.py)** — Captures and manages failed signals
3. **Backpressure Monitoring (backpressure.py)** — Detects and reports queue overload
4. **Unified Queue (queue.py)** — Combines all features into a single interface

## Architecture

```
┌─────────────────────┐
│  Signal Generator   │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────────────────────────┐
│   HardenedSignalQueue                   │
├─────────────────────────────────────────┤
│ ┌────────────────────────────────────┐  │
│ │ Deduplication Check                │  │
│ │ (prevents duplicate processing)    │  │
│ └────────────────────────────────────┘  │
│ ┌────────────────────────────────────┐  │
│ │ Backpressure Check                 │  │
│ │ (rejects on queue overload)        │  │
│ └────────────────────────────────────┘  │
│ ┌────────────────────────────────────┐  │
│ │ Redis Queue (lpush)                │  │
│ │ Main signal queue list             │  │
│ └────────────────────────────────────┘  │
└─────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────┐
│   Execution Service                     │
├─────────────────────────────────────────┤
│ ┌────────────────────────────────────┐  │
│ │ consume() — brpop from queue       │  │
│ │ handle_processing_failure() — retry│  │
│ │ or send to DLQ                     │  │
│ └────────────────────────────────────┘  │
└─────────────────────────────────────────┘
           │
       ┌───┴───┐
       │       │
       ▼       ▼
   SUCCESS    FAILURE
       │       │
       │       ▼
       │   ┌──────────────────┐
       │   │ Dead Letter Queue │
       │   │ (Redis list)     │
       │   └──────────────────┘
       │       │
       │       └─► Manual retry via queue_admin.py
       │
       ▼
   Position Updated
```

## Key Features

### 1. Deduplication (`SignalDeduplicator`)

**Purpose:** Prevent the same signal from being processed multiple times within a time window.

**Redis Storage:** SET with TTL
- Key format: `dedup:{signal_id}`
- Default TTL: 3600 seconds (1 hour)
- O(1) lookup time

**Usage:**

```python
from core.signals.dedup import SignalDeduplicator

dedup = SignalDeduplicator(redis_client)

# Check if signal was already processed
if await dedup.is_duplicate("signal_abc"):
    logger.info("Skipping duplicate signal")
    return

# Mark as processed (automatic TTL expiration)
await dedup.mark_processed("signal_abc", ttl_seconds=3600)

# Manual cache cleanup (automatic via TTL, but available for testing)
cleared = await dedup.cleanup()
```

### 2. Dead Letter Queue (`DeadLetterQueue`)

**Purpose:** Capture failed signals for manual review, analysis, and retry.

**Redis Storage:** LIST (Redis list/queue)
- Key format: `signals:dlq`
- Stores original signal + error context
- FIFO ordering (oldest first)

**Stored Data:**
```json
{
  "signal_id": "sig_123",
  "signal_data": { /* original signal */ },
  "error": "Order execution timeout",
  "retry_count": 2,
  "failed_at": "2026-03-26T15:30:45.123456+00:00"
}
```

**Usage:**

```python
from core.signals.dlq import DeadLetterQueue

dlq = DeadLetterQueue(redis_client)

# Push failed signal to DLQ
await dlq.push(
    signal={"signal_id": "sig_123", ...},
    error="Connection timeout",
    retry_count=3
)

# Retrieve oldest failed signal
entry = await dlq.pop()
if entry:
    print(f"Failed signal: {entry['signal_id']}")

# List recent failures for review
failures = await dlq.list_failed(limit=50)

# Manually retry all DLQ signals
count = await dlq.retry_all(max_queue_key="trading_signals")

# Clear the DLQ (destructive)
count = await dlq.purge()

# Get statistics
stats = await dlq.get_stats()
```

### 3. Backpressure Monitor (`BackpressureMonitor`)

**Purpose:** Detect queue overload and provide flow control guidance.

**Redis Storage:** HASH for statistics, LIST for latency history
- Statistics key: `signal_processing_stats:latencies`
- Latency history: `signal_processing_stats:latency_history`

**Usage:**

```python
from core.signals.backpressure import BackpressureMonitor

backpressure = BackpressureMonitor(redis_client)

# Check current queue depth
depth = await backpressure.check_queue_depth()
print(f"Queue has {depth} pending signals")

# Check if overloaded (threshold: 1000)
if await backpressure.is_overloaded(max_depth=1000):
    logger.warning("Queue overloaded, slowing down signal generation")

# Apply backpressure (log and decide action)
result = await backpressure.apply_backpressure(max_depth=1000)
if result["action"] == "PAUSE_SIGNAL_GENERATION":
    # Stop generating new signals until queue drains

# Record processing metrics
await backpressure.record_signal_processed(
    signal_id="sig_123",
    processing_time_ms=45.5
)

# Get comprehensive statistics
stats = await backpressure.get_stats()
print(f"P99 latency: {stats['p99_latency_ms']:.2f}ms")
```

### 4. Unified Hardened Queue (`HardenedSignalQueue`)

**Purpose:** Combine all hardening features into a production-ready queue.

**Features:**
- Automatic deduplication on publish
- Automatic backpressure check on publish
- Unified consume interface
- Automatic retry logic
- DLQ integration

**Usage:**

```python
from core.signals.queue import HardenedSignalQueue

# Create hardened queue
queue = HardenedSignalQueue(
    redis_client,
    queue_key="trading_signals",
    max_retries=3,
    dedup_ttl_seconds=3600,
    backpressure_threshold=1000,
)

# Publish a signal (with dedup + backpressure checks)
success = await queue.publish({
    "signal_id": "sig_123",
    "strategy": "arbitrage",
    "legs": [...]
})

if not success:
    logger.error("Signal rejected by dedup or backpressure")

# Consume signals
async for signal in queue.consume():
    try:
        await process_signal(signal)
    except Exception as e:
        # Automatically handle retry or send to DLQ
        is_retrying = await queue.handle_processing_failure(
            signal,
            error=str(e)
        )
        if not is_retrying:
            logger.error("Signal sent to DLQ for manual review")

# Check health
health = await queue.health()
print(f"Queue depth: {health['queue_depth']}")
print(f"DLQ size: {health['dlq_size']}")
print(f"Overloaded: {health['is_overloaded']}")

# Admin operations
await queue.retry_dlq()      # Retry all DLQ signals
await queue.purge_dlq()      # Clear DLQ
await queue.flush_dedup()    # Clear dedup cache
```

## Integration with Execution Service

Here's how to integrate the hardened queue with the execution service:

```python
# execution/main.py

from core.signals.queue import HardenedSignalQueue

class ExecutionService:
    async def connect(self) -> None:
        # ... existing code ...

        # Initialize hardened queue
        self.signal_queue = HardenedSignalQueue(
            self.redis_client,
            queue_key=self.config.redis.signal_queue_name,
            max_retries=self.config.execution.max_order_retries,
            dedup_ttl_seconds=self.config.risk_controls.duplicate_signal_window_s,
            backpressure_threshold=1000,
        )

    async def consume_queue(self) -> None:
        """Consume signals with hardened queue."""
        logger.info("Starting hardened signal consumer")

        try:
            async for signal in self.signal_queue.consume():
                try:
                    signal_id = signal.get("signal_id")
                    logger.debug(f"Processing signal: {signal_id}")

                    # Process the signal
                    await self.signal_handler.process_signal(signal)

                except Exception as e:
                    logger.error(f"Error processing signal: {e}")

                    # Automatically handle retry or DLQ
                    is_retrying = await self.signal_queue.handle_processing_failure(
                        signal,
                        error=str(e)
                    )

                    if is_retrying:
                        logger.info(f"Signal {signal.get('signal_id')} queued for retry")
                    else:
                        logger.error(
                            f"Signal {signal.get('signal_id')} sent to DLQ "
                            f"after {signal.get('_retry_count')} retries"
                        )

        except Exception as e:
            logger.error(f"Fatal error in consume: {e}")
            self.running = False
```

## CLI Admin Tool

The `queue_admin.py` script provides operational visibility and control:

```bash
# Show queue health status
python scripts/queue_admin.py --redis-url redis://localhost:6379 status

# List failed signals in DLQ
python scripts/queue_admin.py dlq-list --limit 50

# Retry all DLQ signals
python scripts/queue_admin.py dlq-retry

# Clear DLQ (requires confirmation)
python scripts/queue_admin.py dlq-purge --force

# Clear dedup cache (requires confirmation)
python scripts/queue_admin.py flush-dedup --force

# Show DLQ statistics
python scripts/queue_admin.py dlq-stats

# Show dedup cache statistics
python scripts/queue_admin.py dedup-stats

# Show backpressure monitor statistics
python scripts/queue_admin.py backpressure-stats
```

## Configuration

Key configuration options in `core/config.py`:

```python
redis_url: str = "redis://localhost:6379"
signal_queue_name: str = "trading_signals"
signal_queue_timeout_s: int = 5
duplicate_signal_window_s: int = 300  # Dedup TTL
max_order_retries: int = 3            # Max retries before DLQ
```

## Performance Characteristics

| Component | Complexity | Storage | Notes |
|-----------|-----------|---------|-------|
| Dedup check | O(1) | Memory (SET) | ~1KB per signal |
| DLQ push | O(1) | Memory (LIST) | ~5KB per entry (signal + metadata) |
| Backpressure check | O(1) | Memory (cached) | Queue depth from Redis |
| Queue publish | O(1) | Memory (LIST) | Standard Redis LPUSH |

**Example sizing:**
- 10,000 active signals in dedup cache: ~10MB
- 100 entries in DLQ: ~500KB
- 1-hour dedup window: automatic TTL cleanup

## Error Handling

All components handle Redis connection errors gracefully:

1. **Connection errors:** Return False or empty results, log error
2. **Timeout errors:** Retry logic at consumer level
3. **Serialization errors:** Log and skip malformed entries
4. **DLQ overflow:** No explicit limits; relies on Redis memory

## Monitoring & Alerting

Key metrics to monitor:

```
- Queue depth (should stay < backpressure_threshold)
- DLQ size (should stay small, ideally 0)
- Processing latency (p50, p95, p99)
- Retry rate (count of signals retried)
- Overload events (count of backpressure rejections)
- Dedup hits (duplicate signal rejections)
```

## Testing

Run unit tests:

```bash
pytest tests/unit/test_signal_hardening.py -v
```

Tests cover:
- Deduplication correctness
- DLQ push/pop/list operations
- Backpressure detection
- Unified queue with all features
- Retry logic
- Purge operations

## Future Enhancements

Potential improvements:

1. **Exponential backoff for retries** — Delay retries to avoid thundering herd
2. **Signal priority queue** — High-priority signals processed first
3. **Per-signal DLQ retention** — Configurable cleanup for DLQ entries
4. **Metrics export** — Prometheus metrics for monitoring
5. **Circuit breaker** — Pause signal generation on cascade failures
6. **Batch operations** — Consume/process multiple signals atomically
