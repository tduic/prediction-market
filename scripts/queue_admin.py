#!/usr/bin/env python3
"""
CLI admin tool for managing the hardened signal queue.

Provides operational visibility and control over:
- Queue health and status
- Dead letter queue management
- Deduplication cache maintenance
"""

import asyncio
from typing import Optional

import click
import redis.asyncio as redis
from tabulate import tabulate  # type: ignore[import-untyped]

from core.signals.backpressure import BackpressureMonitor
from core.signals.dedup import SignalDeduplicator
from core.signals.dlq import DeadLetterQueue
from core.signals.queue import HardenedSignalQueue


class QueueAdmin:
    """Admin tool for signal queue operations."""

    def __init__(self, redis_url: str):
        """Initialize with Redis connection URL."""
        self.redis_url = redis_url
        self.redis_client: Optional[redis.Redis] = None

    async def connect(self) -> None:
        """Connect to Redis."""
        self.redis_client = await redis.from_url(self.redis_url)
        await self.redis_client.ping()

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        if self.redis_client:
            await self.redis_client.close()

    async def show_status(self) -> None:
        """Display queue health status."""
        if not self.redis_client:
            return

        queue = HardenedSignalQueue(self.redis_client)
        health = await queue.health()

        # Format main stats
        main_stats = [
            ["Queue Depth", health.get("queue_depth", 0)],
            ["DLQ Size", health.get("dlq_size", 0)],
            ["Dedup Cache Size", health.get("dedup_cache_size", 0)],
            ["Overloaded", "YES" if health.get("is_overloaded") else "NO"],
            ["Max Retries", health.get("max_retries", 0)],
        ]

        print("\n=== Signal Queue Status ===\n")
        print(tabulate(main_stats, headers=["Metric", "Value"], tablefmt="grid"))

        # Format processing stats if available
        stats = health.get("stats", {})
        if stats and "processed_count" in stats:
            processing_stats = [
                ["Signals Processed", stats.get("processed_count", 0)],
                ["Avg Latency (ms)", f"{stats.get('avg_latency_ms', 0):.2f}"],
                ["P50 Latency (ms)", f"{stats.get('p50_latency_ms', 0):.2f}"],
                ["P95 Latency (ms)", f"{stats.get('p95_latency_ms', 0):.2f}"],
                ["P99 Latency (ms)", f"{stats.get('p99_latency_ms', 0):.2f}"],
                ["Min Latency (ms)", f"{stats.get('min_latency_ms', 0):.2f}"],
                ["Max Latency (ms)", f"{stats.get('max_latency_ms', 0):.2f}"],
            ]

            print("\n=== Processing Statistics ===\n")
            print(
                tabulate(processing_stats, headers=["Metric", "Value"], tablefmt="grid")
            )

    async def list_dlq(self, limit: int = 50) -> None:
        """List failed signals in the DLQ."""
        if not self.redis_client:
            return

        dlq = DeadLetterQueue(self.redis_client)
        failed_signals = await dlq.list_failed(limit=limit)

        if not failed_signals:
            print("\nDead Letter Queue is empty.\n")
            return

        # Format for tabular display
        rows = []
        for entry in failed_signals:
            signal_id = entry.get("signal_id", "unknown")
            error = entry.get("error", "unknown")[:50]  # Truncate long errors
            retry_count = entry.get("retry_count", 0)
            failed_at = entry.get("failed_at", "unknown")

            rows.append([signal_id, error, retry_count, failed_at])

        print(f"\n=== Dead Letter Queue ({len(failed_signals)} entries) ===\n")
        print(
            tabulate(
                rows,
                headers=["Signal ID", "Error", "Retries", "Failed At"],
                tablefmt="grid",
            )
        )

    async def retry_dlq(self) -> None:
        """Retry all signals in the DLQ."""
        if not self.redis_client:
            return

        queue = HardenedSignalQueue(self.redis_client)
        count = await queue.retry_dlq()

        print(f"\nRetried {count} signals from DLQ back to main queue.\n")

    async def purge_dlq(self, confirm: bool = False) -> None:
        """Purge all entries from the DLQ."""
        if not self.redis_client:
            return

        if not confirm:
            print("\nWARNING: This will permanently delete all DLQ entries.")
            response = input("Type 'yes' to confirm: ").strip().lower()
            if response != "yes":
                print("Cancelled.\n")
                return

        queue = HardenedSignalQueue(self.redis_client)
        count = await queue.purge_dlq()

        print(f"\nPurged {count} entries from DLQ.\n")

    async def flush_dedup(self, confirm: bool = False) -> None:
        """Clear the deduplication cache."""
        if not self.redis_client:
            return

        if not confirm:
            print("\nWARNING: This will clear all deduplication cache.")
            print("Previously processed signals may be reprocessed.")
            response = input("Type 'yes' to confirm: ").strip().lower()
            if response != "yes":
                print("Cancelled.\n")
                return

        queue = HardenedSignalQueue(self.redis_client)
        count = await queue.flush_dedup()

        print(f"\nCleared {count} entries from dedup cache.\n")

    async def show_dlq_stats(self) -> None:
        """Display DLQ statistics."""
        if not self.redis_client:
            return

        dlq = DeadLetterQueue(self.redis_client)
        stats = await dlq.get_stats()

        rows = [
            ["Queue Key", stats.get("queue_key", "unknown")],
            ["Size", stats.get("dlq_size", 0)],
            ["Oldest Signal ID", stats.get("oldest_signal_id", "N/A")],
            ["Oldest Failed At", stats.get("oldest_failed_at", "N/A")],
            ["Oldest Retry Count", stats.get("oldest_retry_count", 0)],
        ]

        print("\n=== DLQ Statistics ===\n")
        print(tabulate(rows, headers=["Metric", "Value"], tablefmt="grid"))

    async def show_dedup_stats(self) -> None:
        """Display deduplication cache statistics."""
        if not self.redis_client:
            return

        dedup = SignalDeduplicator(self.redis_client)
        cache_size = await dedup.get_cache_size()

        rows = [
            ["Cache Size", cache_size],
            ["Default TTL (seconds)", dedup.default_ttl_seconds],
        ]

        print("\n=== Deduplication Cache ===\n")
        print(tabulate(rows, headers=["Metric", "Value"], tablefmt="grid"))

    async def show_backpressure_stats(self) -> None:
        """Display backpressure monitoring statistics."""
        if not self.redis_client:
            return

        backpressure = BackpressureMonitor(self.redis_client)
        stats = await backpressure.get_stats()

        rows = [
            ["Queue Depth", stats.get("queue_depth", 0)],
            ["Overloaded", "YES" if stats.get("is_overloaded") else "NO"],
            ["Processed Count", stats.get("processed_count", 0)],
            ["Avg Latency (ms)", f"{stats.get('avg_latency_ms', 0):.2f}"],
            ["P50 Latency (ms)", f"{stats.get('p50_latency_ms', 0):.2f}"],
            ["P95 Latency (ms)", f"{stats.get('p95_latency_ms', 0):.2f}"],
            ["P99 Latency (ms)", f"{stats.get('p99_latency_ms', 0):.2f}"],
        ]

        print("\n=== Backpressure Monitor ===\n")
        print(tabulate(rows, headers=["Metric", "Value"], tablefmt="grid"))


@click.group()
@click.option(
    "--redis-url",
    envvar="REDIS_URL",
    default="redis://localhost:6379",
    help="Redis connection URL",
)
@click.pass_context
def cli(ctx: click.Context, redis_url: str) -> None:
    """Signal queue administration CLI."""
    ctx.ensure_object(dict)
    ctx.obj["admin"] = QueueAdmin(redis_url)


async def run_async_command(coro) -> None:
    """Run an async command."""
    await coro


@cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show queue health status."""
    admin = ctx.obj["admin"]

    async def run():
        await admin.connect()
        try:
            await admin.show_status()
        finally:
            await admin.disconnect()

    asyncio.run(run())


@cli.command()
@click.option(
    "--limit",
    default=50,
    help="Maximum number of entries to list",
)
@click.pass_context
def dlq_list(ctx: click.Context, limit: int) -> None:
    """List failed signals in the DLQ."""
    admin = ctx.obj["admin"]

    async def run():
        await admin.connect()
        try:
            await admin.list_dlq(limit=limit)
        finally:
            await admin.disconnect()

    asyncio.run(run())


@cli.command()
@click.pass_context
def dlq_retry(ctx: click.Context) -> None:
    """Retry all signals in the DLQ."""
    admin = ctx.obj["admin"]

    async def run():
        await admin.connect()
        try:
            await admin.retry_dlq()
        finally:
            await admin.disconnect()

    asyncio.run(run())


@cli.command()
@click.option(
    "--force",
    is_flag=True,
    help="Skip confirmation prompt",
)
@click.pass_context
def dlq_purge(ctx: click.Context, force: bool) -> None:
    """Clear all entries from the DLQ."""
    admin = ctx.obj["admin"]

    async def run():
        await admin.connect()
        try:
            await admin.purge_dlq(confirm=force)
        finally:
            await admin.disconnect()

    asyncio.run(run())


@cli.command()
@click.option(
    "--force",
    is_flag=True,
    help="Skip confirmation prompt",
)
@click.pass_context
def flush_dedup(ctx: click.Context, force: bool) -> None:
    """Clear the deduplication cache."""
    admin = ctx.obj["admin"]

    async def run():
        await admin.connect()
        try:
            await admin.flush_dedup(confirm=force)
        finally:
            await admin.disconnect()

    asyncio.run(run())


@cli.command()
@click.pass_context
def dlq_stats(ctx: click.Context) -> None:
    """Show DLQ statistics."""
    admin = ctx.obj["admin"]

    async def run():
        await admin.connect()
        try:
            await admin.show_dlq_stats()
        finally:
            await admin.disconnect()

    asyncio.run(run())


@cli.command()
@click.pass_context
def dedup_stats(ctx: click.Context) -> None:
    """Show deduplication cache statistics."""
    admin = ctx.obj["admin"]

    async def run():
        await admin.connect()
        try:
            await admin.show_dedup_stats()
        finally:
            await admin.disconnect()

    asyncio.run(run())


@cli.command()
@click.pass_context
def backpressure_stats(ctx: click.Context) -> None:
    """Show backpressure monitor statistics."""
    admin = ctx.obj["admin"]

    async def run():
        await admin.connect()
        try:
            await admin.show_backpressure_stats()
        finally:
            await admin.disconnect()

    asyncio.run(run())


if __name__ == "__main__":
    cli(obj={})
