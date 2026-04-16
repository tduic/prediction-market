"""
Core service entry point.

Initializes all components: database, event bus, ingestor, constraint engine,
signal generator, and schedulers. Runs the main event loop.

All configuration is loaded from environment variables via get_config().
"""

import asyncio
import logging
import signal as signal_module
import sys

from core.config import get_config
from core.constraints.engine import ConstraintEngine
from core.events.bus import EventBus
from core.ingestor.scheduler import IngestorScheduler
from core.signals.generator import SignalGenerator
from core.storage.db import Database

logger = logging.getLogger(__name__)


class CoreService:
    """Main core service orchestrator."""

    def __init__(self) -> None:
        """Initialize the core service from global config."""
        self.config = get_config()

        self.db: Database | None = None
        self.event_bus: EventBus | None = None
        self.constraint_engine: ConstraintEngine | None = None
        self.signal_generator: SignalGenerator | None = None
        self.scheduler: IngestorScheduler | None = None

        self.running = True

    async def initialize(self) -> None:
        """Initialize all service components."""
        cfg = self.config
        logger.info("Initializing core service")
        logger.info("  Execution mode: %s", cfg.execution.execution_mode)
        logger.info("  Database: %s", cfg.database.db_path)

        # Initialize database with migrations
        self.db = Database(
            cfg.database.db_path,
            migrations_dir=cfg.database.migrations_dir,
        )
        await self.db.init()

        # Initialize event bus
        self.event_bus = EventBus()

        # Initialize constraint engine
        self.constraint_engine = ConstraintEngine(
            event_bus=self.event_bus,
            db=self.db,
        )

        # Initialize signal generator
        self.signal_generator = SignalGenerator(
            event_bus=self.event_bus,
            db=self.db,
        )

        # Initialize ingestor scheduler
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
        signal_module.signal(signal_module.SIGINT, self._shutdown_handler)
        signal_module.signal(signal_module.SIGTERM, self._shutdown_handler)

        try:
            await self.initialize()

            if self.scheduler:
                await self.scheduler.start()
                logger.info("Scheduler started")

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
    from dotenv import load_dotenv

    from core.logging_config import configure_from_env

    load_dotenv()
    configure_from_env()

    service = CoreService()
    await service.run()


if __name__ == "__main__":
    asyncio.run(main())
