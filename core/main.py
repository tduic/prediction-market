"""
Core service entry point.

Initializes all components: database, event bus, ingestor, constraint engine,
signal generator, model service, and schedulers. Runs the main event loop.
"""

import asyncio
import logging
import os
import signal as signal_module
import sys
from typing import Optional

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.event_bus import EventBus
from core.ingestor import Ingestor
from core.constraint_engine import ConstraintEngine
from core.signal_generator import SignalGenerator
from core.model_service import ModelService
from core.database import Database, run_migrations

logger = logging.getLogger(__name__)


class CoreService:
    """Main core service orchestrator."""

    def __init__(
        self,
        db_path: str = "prediction_market.db",
        redis_url: str = "redis://localhost:6379",
        signal_queue_name: str = "trading_signals",
        event_bus_channel: str = "market_events",
    ) -> None:
        """
        Initialize the core service.

        Args:
            db_path: Path to SQLite database
            redis_url: Redis connection URL
            signal_queue_name: Name of the Redis queue for signals
            event_bus_channel: Name of the event bus channel
        """
        self.db_path = db_path
        self.redis_url = redis_url
        self.signal_queue_name = signal_queue_name
        self.event_bus_channel = event_bus_channel

        self.db: Optional[Database] = None
        self.event_bus: Optional[EventBus] = None
        self.ingestor: Optional[Ingestor] = None
        self.constraint_engine: Optional[ConstraintEngine] = None
        self.signal_generator: Optional[SignalGenerator] = None
        self.model_service: Optional[ModelService] = None
        self.scheduler: Optional[AsyncIOScheduler] = None

        self.running = True

    async def initialize(self) -> None:
        """Initialize all service components."""
        logger.info("Initializing core service")

        # Initialize database
        logger.info("Initializing database at %s", self.db_path)
        self.db = Database(self.db_path)
        await self.db.connect()

        # Run migrations
        logger.info("Running database migrations")
        await run_migrations(self.db.connection)

        # Initialize event bus
        logger.info("Initializing event bus")
        self.event_bus = EventBus(
            redis_url=self.redis_url,
            channel_name=self.event_bus_channel,
        )
        await self.event_bus.connect()

        # Initialize ingestor
        logger.info("Initializing ingestor")
        self.ingestor = Ingestor(
            db_connection=self.db.connection,
            event_bus=self.event_bus,
        )

        # Initialize constraint engine
        logger.info("Initializing constraint engine")
        self.constraint_engine = ConstraintEngine(
            db_connection=self.db.connection,
        )

        # Subscribe constraint engine to market updates
        if self.event_bus:
            await self.event_bus.subscribe(
                event_type="MarketUpdated",
                handler=self.constraint_engine.on_market_updated,
            )

        # Initialize signal generator
        logger.info("Initializing signal generator")
        self.signal_generator = SignalGenerator(
            db_connection=self.db.connection,
            redis_client=self.event_bus.redis_client,
            signal_queue_name=self.signal_queue_name,
        )

        # Subscribe signal generator to violations
        if self.event_bus:
            await self.event_bus.subscribe(
                event_type="ViolationDetected",
                handler=self.signal_generator.on_violation_detected,
            )

        # Initialize model service
        logger.info("Initializing model service")
        self.model_service = ModelService(
            db_connection=self.db.connection,
        )

        # Initialize scheduler
        logger.info("Initializing scheduler")
        self.scheduler = AsyncIOScheduler()

        # Schedule periodic tasks
        self._schedule_tasks()

        logger.info("Core service initialization complete")

    def _schedule_tasks(self) -> None:
        """Schedule periodic background tasks."""
        if not self.scheduler:
            return

        ingest_interval = int(os.getenv("INGEST_INTERVAL_S", "5"))
        constraint_interval = int(os.getenv("CONSTRAINT_CHECK_INTERVAL_S", "10"))
        pnl_interval = int(os.getenv("PNL_SNAPSHOT_INTERVAL_S", "300"))
        refit_interval = int(os.getenv("MODEL_REFIT_INTERVAL_S", "3600"))

        # Schedule ingestor
        if self.ingestor:
            self.scheduler.add_job(
                self.ingestor.ingest_market_data,
                "interval",
                seconds=ingest_interval,
                id="ingest_market_data",
            )

        # Schedule constraint checking
        if self.constraint_engine:
            self.scheduler.add_job(
                self.constraint_engine.check_constraints,
                "interval",
                seconds=constraint_interval,
                id="check_constraints",
            )

        # Schedule PnL snapshots
        if self.db:
            self.scheduler.add_job(
                self._snapshot_pnl,
                "interval",
                seconds=pnl_interval,
                id="snapshot_pnl",
            )

        # Schedule model refit
        if self.model_service:
            self.scheduler.add_job(
                self.model_service.refit_models,
                "interval",
                seconds=refit_interval,
                id="refit_models",
            )

        logger.info("Scheduled %d background tasks", len(self.scheduler.get_jobs()))

    async def _snapshot_pnl(self) -> None:
        """Take a PnL snapshot."""
        if not self.db:
            return

        try:
            cursor = await self.db.connection.execute(
                """
                INSERT INTO pnl_snapshots
                (timestamp_utc, total_positions, unrealized_pnl,
                 realized_pnl, total_return)
                SELECT
                    datetime('now'),
                    COUNT(*),
                    SUM(unrealized_pnl),
                    0,
                    0
                FROM positions
                """
            )
            await self.db.connection.commit()
            logger.debug("PnL snapshot taken")
        except Exception as e:
            logger.error("Error taking PnL snapshot: %s", exc_info=e)

    def _shutdown_handler(self, signum: int, frame) -> None:
        """Handle shutdown signals."""
        logger.info("Received signal %d, initiating graceful shutdown", signum)
        self.running = False

    async def shutdown(self) -> None:
        """Shutdown all service components."""
        logger.info("Shutting down core service")

        if self.scheduler:
            self.scheduler.shutdown()
            logger.info("Scheduler shutdown")

        if self.ingestor:
            await self.ingestor.shutdown()
            logger.info("Ingestor shutdown")

        if self.event_bus:
            await self.event_bus.disconnect()
            logger.info("Event bus shutdown")

        if self.db:
            await self.db.disconnect()
            logger.info("Database shutdown")

        logger.info("Core service shutdown complete")

    async def run(self) -> None:
        """Run the core service."""
        # Register signal handlers
        signal_module.signal(signal_module.SIGINT, self._shutdown_handler)
        signal_module.signal(signal_module.SIGTERM, self._shutdown_handler)

        try:
            await self.initialize()

            if self.scheduler:
                self.scheduler.start()
                logger.info("Scheduler started")

            # Main loop
            logger.info("Core service running, waiting for shutdown signal")
            while self.running:
                await asyncio.sleep(1)

        except Exception as e:
            logger.error("Fatal error in core service: %s", exc_info=e)
            sys.exit(1)
        finally:
            await self.shutdown()


async def main() -> None:
    """Main entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    db_path = os.getenv("DB_PATH", "prediction_market.db")
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    signal_queue_name = os.getenv("SIGNAL_QUEUE_NAME", "trading_signals")
    event_bus_channel = os.getenv("EVENT_BUS_CHANNEL", "market_events")

    service = CoreService(
        db_path=db_path,
        redis_url=redis_url,
        signal_queue_name=signal_queue_name,
        event_bus_channel=event_bus_channel,
    )

    await service.run()


if __name__ == "__main__":
    asyncio.run(main())
