"""APScheduler-based job scheduler for market ingestion."""

import logging
import os
from datetime import datetime
from typing import Any, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


class IngestorScheduler:
    """Manages polling job scheduling across all data sources."""

    def __init__(
        self,
        polymarket_poll_interval_s: int | None = None,
        kalshi_poll_interval_s: int | None = None,
        external_poll_interval_s: int | None = None,
    ):
        """
        Initialize scheduler with configurable intervals.

        Args:
            polymarket_poll_interval_s: Polymarket polling interval (default 30s)
            kalshi_poll_interval_s: Kalshi polling interval (default 30s)
            external_poll_interval_s: External feeds polling interval (default 300s)
        """
        self.polymarket_interval = polymarket_poll_interval_s or int(
            os.getenv("POLL_INTERVAL_POLYMARKET_S", "30")
        )
        self.kalshi_interval = kalshi_poll_interval_s or int(
            os.getenv("POLL_INTERVAL_KALSHI_S", "30")
        )
        self.external_interval = external_poll_interval_s or int(
            os.getenv("POLL_INTERVAL_EXTERNAL_S", "300")
        )

        self.scheduler = AsyncIOScheduler()
        self._job_ids: dict[str, str] = {}

    async def start(self) -> None:
        """Start the scheduler."""
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("Ingestor scheduler started")

    async def stop(self) -> None:
        """Stop the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown()
            logger.info("Ingestor scheduler stopped")

    def register_polymarket_job(
        self,
        job_func: Callable[..., Any],
        job_name: str = "poll_polymarket",
    ) -> str:
        """
        Register Polymarket polling job.

        Args:
            job_func: Async function to execute
            job_name: Job identifier

        Returns:
            Job ID
        """
        job_id = job_name
        if job_id in self._job_ids:
            self.scheduler.remove_job(self._job_ids[job_id])

        job = self.scheduler.add_job(
            job_func,
            trigger=IntervalTrigger(seconds=self.polymarket_interval),
            id=job_id,
            name=job_name,
            replace_existing=True,
            misfire_grace_time=10,
        )

        self._job_ids[job_id] = job.id
        logger.info(
            f"Registered Polymarket job '{job_name}' with {self.polymarket_interval}s interval"
        )
        return job.id

    def register_kalshi_job(
        self,
        job_func: Callable[..., Any],
        job_name: str = "poll_kalshi",
    ) -> str:
        """
        Register Kalshi polling job.

        Args:
            job_func: Async function to execute
            job_name: Job identifier

        Returns:
            Job ID
        """
        job_id = job_name
        if job_id in self._job_ids:
            self.scheduler.remove_job(self._job_ids[job_id])

        job = self.scheduler.add_job(
            job_func,
            trigger=IntervalTrigger(seconds=self.kalshi_interval),
            id=job_id,
            name=job_name,
            replace_existing=True,
            misfire_grace_time=10,
        )

        self._job_ids[job_id] = job.id
        logger.info(
            f"Registered Kalshi job '{job_name}' with {self.kalshi_interval}s interval"
        )
        return job.id

    def register_external_job(
        self,
        job_func: Callable[..., Any],
        job_name: str = "poll_external",
    ) -> str:
        """
        Register external feeds polling job.

        Args:
            job_func: Async function to execute
            job_name: Job identifier

        Returns:
            Job ID
        """
        job_id = job_name
        if job_id in self._job_ids:
            self.scheduler.remove_job(self._job_ids[job_id])

        job = self.scheduler.add_job(
            job_func,
            trigger=IntervalTrigger(seconds=self.external_interval),
            id=job_id,
            name=job_name,
            replace_existing=True,
            misfire_grace_time=10,
        )

        self._job_ids[job_id] = job.id
        logger.info(
            f"Registered external job '{job_name}' with {self.external_interval}s interval"
        )
        return job.id

    def register_custom_job(
        self,
        job_func: Callable[..., Any],
        interval_s: int,
        job_name: str,
    ) -> str:
        """
        Register a custom polling job.

        Args:
            job_func: Async function to execute
            interval_s: Polling interval in seconds
            job_name: Job identifier

        Returns:
            Job ID
        """
        job_id = job_name
        if job_id in self._job_ids:
            self.scheduler.remove_job(self._job_ids[job_id])

        job = self.scheduler.add_job(
            job_func,
            trigger=IntervalTrigger(seconds=interval_s),
            id=job_id,
            name=job_name,
            replace_existing=True,
            misfire_grace_time=10,
        )

        self._job_ids[job_id] = job.id
        logger.info(f"Registered job '{job_name}' with {interval_s}s interval")
        return job.id

    def unregister_job(self, job_name: str) -> bool:
        """
        Unregister and remove a job.

        Args:
            job_name: Job identifier

        Returns:
            True if job was removed, False if not found
        """
        if job_name in self._job_ids:
            self.scheduler.remove_job(self._job_ids[job_name])
            del self._job_ids[job_name]
            logger.info(f"Unregistered job '{job_name}'")
            return True
        return False

    def get_job_status(self, job_name: str) -> dict | None:
        """
        Get status of a registered job.

        Args:
            job_name: Job identifier

        Returns:
            Job status dict or None if not found
        """
        if job_name not in self._job_ids:
            return None

        job = self.scheduler.get_job(self._job_ids[job_name])
        if not job:
            return None

        return {
            "job_id": job.id,
            "name": job.name,
            "trigger": str(job.trigger),
            "next_run_time": job.next_run_time,
            "last_run_time": getattr(job, "last_run_time", None),
        }

    def list_jobs(self) -> dict[str, dict]:
        """
        List all registered jobs.

        Returns:
            Dict mapping job names to status dicts
        """
        result = {}
        for job_name in self._job_ids:
            status = self.get_job_status(job_name)
            if status:
                result[job_name] = status
        return result
