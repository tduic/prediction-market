"""
Database connection pool and migration runner for aiosqlite.
Implements single-writer pattern with concurrent read support via WAL mode.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any, List, Optional, Dict

import aiosqlite

logger = logging.getLogger(__name__)


class Database:
    """
    Async database connection pool with migration support.

    Features:
    - Connection pooling for efficient resource usage
    - Single writer pattern using asyncio.Lock
    - WAL mode for concurrent reads
    - Automatic schema migration on initialization
    """

    def __init__(self, db_path: str, migrations_dir: str = "./migrations"):
        """
        Initialize database connection.

        Args:
            db_path: Path to SQLite database file
            migrations_dir: Directory containing SQL migration files
        """
        self.db_path = db_path
        self.migrations_dir = Path(migrations_dir)
        self._conn: Optional[aiosqlite.Connection] = None
        self._write_lock = asyncio.Lock()
        self._initialized = False

    async def init(self) -> None:
        """
        Initialize the database connection and run migrations.

        Must be called before any database operations.
        """
        if self._initialized:
            logger.warning("Database already initialized")
            return

        # Create connection
        self._conn = await aiosqlite.connect(self.db_path)

        # Enable WAL mode for concurrent reads
        await self._conn.execute("PRAGMA journal_mode=WAL")

        # Enable foreign keys
        await self._conn.execute("PRAGMA foreign_keys=ON")

        # Set reasonable timeout
        self._conn.timeout = 30.0

        # Run migrations
        await self._run_migrations()

        self._initialized = True
        logger.info(f"Database initialized at {self.db_path}")

    async def _run_migrations(self) -> None:
        """
        Run all SQL migrations in order.

        Migrations are expected to be named 001_*, 002_*, etc.
        """
        if not self.migrations_dir.exists():
            logger.warning(f"Migrations directory not found: {self.migrations_dir}")
            return

        migration_files = sorted(self.migrations_dir.glob("*.sql"))

        for migration_file in migration_files:
            logger.info(f"Running migration: {migration_file.name}")

            async with self._write_lock:
                sql_content = migration_file.read_text()
                try:
                    await self._conn.executescript(sql_content)
                    await self._conn.commit()
                except aiosqlite.Error as e:
                    await self._conn.rollback()
                    logger.error(
                        f"Migration failed: {migration_file.name}: {e}", exc_info=True
                    )
                    raise

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._initialized = False
            logger.info("Database connection closed")

    async def execute(
        self,
        sql: str,
        params: Optional[tuple] = None,
    ) -> int:
        """
        Execute a single SQL statement (INSERT, UPDATE, DELETE).

        Uses write lock to ensure single-writer pattern.

        Args:
            sql: SQL statement to execute
            params: Query parameters

        Returns:
            Last row ID for inserts
        """
        if not self._initialized:
            raise RuntimeError("Database not initialized")

        async with self._write_lock:
            try:
                cursor = await self._conn.execute(sql, params or ())
                await self._conn.commit()
                return cursor.lastrowid
            except aiosqlite.Error as e:
                await self._conn.rollback()
                logger.error(f"Execute error: {e}", exc_info=True)
                raise

    async def executemany(
        self,
        sql: str,
        params_list: List[tuple],
    ) -> int:
        """
        Execute multiple SQL statements in a transaction.

        Args:
            sql: SQL statement to execute
            params_list: List of query parameter tuples

        Returns:
            Last row ID
        """
        if not self._initialized:
            raise RuntimeError("Database not initialized")

        async with self._write_lock:
            try:
                cursor = await self._conn.executemany(sql, params_list)
                await self._conn.commit()
                return cursor.lastrowid
            except aiosqlite.Error as e:
                await self._conn.rollback()
                logger.error(f"Executemany error: {e}", exc_info=True)
                raise

    async def fetch_one(
        self,
        sql: str,
        params: Optional[tuple] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch a single row as a dictionary.

        Args:
            sql: SELECT query
            params: Query parameters

        Returns:
            Dictionary representation of row, or None if no results
        """
        if not self._initialized:
            raise RuntimeError("Database not initialized")

        try:
            # Set row_factory for dict-like access
            self._conn.row_factory = aiosqlite.Row

            cursor = await self._conn.execute(sql, params or ())
            row = await cursor.fetchone()

            if row:
                return dict(row)
            return None
        except aiosqlite.Error as e:
            logger.error(f"Fetch one error: {e}", exc_info=True)
            raise
        finally:
            self._conn.row_factory = None

    async def fetch_all(
        self,
        sql: str,
        params: Optional[tuple] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch all rows as dictionaries.

        Args:
            sql: SELECT query
            params: Query parameters

        Returns:
            List of dictionary representations of rows
        """
        if not self._initialized:
            raise RuntimeError("Database not initialized")

        try:
            # Set row_factory for dict-like access
            self._conn.row_factory = aiosqlite.Row

            cursor = await self._conn.execute(sql, params or ())
            rows = await cursor.fetchall()

            return [dict(row) for row in rows]
        except aiosqlite.Error as e:
            logger.error(f"Fetch all error: {e}", exc_info=True)
            raise
        finally:
            self._conn.row_factory = None

    async def fetch_val(
        self,
        sql: str,
        params: Optional[tuple] = None,
    ) -> Any:
        """
        Fetch a single scalar value.

        Args:
            sql: SELECT query
            params: Query parameters

        Returns:
            The first column of the first row
        """
        if not self._initialized:
            raise RuntimeError("Database not initialized")

        try:
            cursor = await self._conn.execute(sql, params or ())
            row = await cursor.fetchone()
            return row[0] if row else None
        except aiosqlite.Error as e:
            logger.error(f"Fetch val error: {e}", exc_info=True)
            raise

    async def execute_script(self, sql: str) -> None:
        """
        Execute a multi-statement SQL script.

        Useful for migrations and bulk operations.

        Args:
            sql: SQL script to execute
        """
        if not self._initialized:
            raise RuntimeError("Database not initialized")

        async with self._write_lock:
            try:
                await self._conn.executescript(sql)
                await self._conn.commit()
            except aiosqlite.Error as e:
                await self._conn.rollback()
                logger.error(f"Script execution error: {e}", exc_info=True)
                raise

    async def transaction(self):
        """
        Context manager for explicit transactions.

        Example:
            async with db.transaction():
                await db.execute(...)
                await db.execute(...)
        """

        class TransactionContext:
            def __init__(self, db: "Database"):
                self.db = db

            async def __aenter__(self):
                await self.db._write_lock.acquire()
                await self.db._conn.execute("BEGIN")
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                try:
                    if exc_type:
                        await self.db._conn.rollback()
                    else:
                        await self.db._conn.commit()
                finally:
                    self.db._write_lock.release()

        return TransactionContext(self)

    async def vacuum(self) -> None:
        """Vacuum the database to optimize storage."""
        async with self._write_lock:
            await self._conn.execute("VACUUM")
            await self._conn.commit()
            logger.info("Database vacuumed")

    async def checkpoint(self) -> None:
        """Checkpoint the WAL, merging it into the main database file."""
        async with self._write_lock:
            await self._conn.execute("PRAGMA wal_checkpoint(RESTART)")
            await self._conn.commit()
            logger.info("WAL checkpoint completed")
