"""
Core service entry point.

Initializes all components: database, event bus, ingestor, constraint engine,
signal generator, and schedulers. Runs the main event loop.
"""

import asyncio
import logging
import os
import signal as signal_module
import sys


from core.events.bus import EventBus
from core.constraints.engine import ConstraintEngine
from core.signals.generator import SignalGenerator
from core.storage.db import Database
from core.ingestor.scheduler import IngestorScheduler

logger = logging.getLogger(__name__)


class CoreService:
    """Main core service orchestrator."""

    def __init__(
        self,
        db_path: str = "prediction_market.db",
        migrations_dir: str = "core/storage/migrations",
    ) -> None:
        """
        Initialize the core service.

        Args:
            db_path: Path to SQLite database
            migrations_dir: Path to SQL migration files
        """
        self.db_path = db_path
        self.migrations_dir = migrations_dir

        self.db: Database | None = None
        self.event_bus: EventBus | None = None
        self.constraint_engine: ConstraintEngine | None = None
        self.signal_generator: SignalGenerator | None = None
        self.scheduler: IngestorScheduler | None = None

        self.running = True

    async def initialize(self) -> None:
        """Initialize all service components."""
        logger.info("Initializing core service")

        # Initialize database
        logger.info("Initializing database at %s", self.db_path)
        self.db = Database(self.db_path, migrations_dir=self.migrations_dir)
        await self.db.init()

        # Initialize event bus
        logger.info("Initializing event bus")
        self.event_bus = EventBus()

        # Initialize constraint engine
        logger.info("Initializing constraint engine")
        self.constraint_engine = ConstraintEngine(
            event_bus=self.event_bus,
            db=self.db,
        )

        # Initialize signal generator
        logger.info("Initializing signal generator")
        self.signal_generator = SignalGenerator(
            event_bus=self.event_bus,
            db=self.db,
        )

        # Initialize ingestor scheduler
        logger.info("Initializing ingestor scheduler")
        self.scheduler = IngestorScheduler()

        logger.info("Core service initialization complete")

    async def _checkpoint_wal(self) -> None:
        """Checkpoint the WAL to keep it from growing unbounded."""
        if not self.db:
            return
        try:
            await self.db.checkpoint()
            logger.debug("WAL checkpoint completed")
        except Exception as e:
            logger.error("Error during WAL checkpoint: %s", e)

    async def _snapshot_pnl(self) -> None:
        """Take a PnL snapshot."""
        if not self.db:
            return

        try:
            await self.db.execute("""
                INSERT INTO pnl_snapshots
                (timestamp_utc, total_positions, unrealized_pnl,
                 realized_pnl, total_return)
                SELECT
                    datetime('now'),
                    COUNT(*),
                    COALESCE(SUM(unrealized_pnl), 0),
                    0,
                    0
                FROM positions
                """)
            logger.debug("PnL snapshot taken")
        except Exception as e:
            logger.error("Error taking PnL snapshot: %s", e)

    def _shutdown_handler(self, signum: int, frame) -> None:
        """Handle shutdown signals."""
        logger.info("Received signal %d, initiating graceful shutdown", signum)
        self.running = False

    async def shutdown(self) -> None:
        """Shutdown all service components."""
        logger.info("Shutting down core service")

        if self.scheduler:
            await self.scheduler.stop()
            logger.info("Scheduler shutdown")

        if self.db:
            await self.db.close()
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
                await self.scheduler.start()
                logger.info("Scheduler started")

            # Main loop
            logger.info("Core service running, waiting for shutdown signal")
            while self.running:
                await asyncio.sleep(1)

        except Exception as e:
            logger.error("Fatal error in core service: %s", e, exc_info=True)
            sys.exit(1)
        finally:
            await self.shutdown()


async def main() -> None:
    """Main entry point."""
    from core.logging_config import configure_from_env

    configure_from_env()

    db_path = os.getenv("DB_PATH", "prediction_market.db")
    migrations_dir = os.getenv("MIGRATIONS_DIR", "core/storage/migrations")

    service = CoreService(
        db_path=db_path,
        migrations_dir=migrations_dir,
    )

    await service.run()


if __name__ == "__main__":
    asyncio.run(main())
