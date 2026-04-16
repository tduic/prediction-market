"""
Async SQLite database wrapper with migration support.

Provides a single-writer pattern using asyncio locks and
automatic migration execution on startup.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


class Database:
    """
    Async SQLite database wrapper.

    Features:
    - Automatic WAL mode and foreign key enforcement
    - Write lock for single-writer pattern
    - Ordered migration execution with history tracking
    - Row-factory dict access for fetch operations
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
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self._initialized = False

    def _require_conn(self) -> aiosqlite.Connection:
        """Return the active aiosqlite connection, or raise if not initialized.

        Centralises the None check so public methods can use the returned
        value without triggering union-attr errors under mypy.
        """
        if self._conn is None:
            raise RuntimeError("Database not initialized")
        return self._conn

    async def init(self) -> None:
        """
        Initialize the database connection and run migrations.

        Must be called before any database operations.
        """
        if self._initialized:
            logger.warning("Database already initialized")
            return

        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=30000")

        await self._run_migrations()

        self._initialized = True
        logger.info(f"Database initialized at {self.db_path}")

    async def _run_migrations(self) -> None:
        """
        Run all SQL migrations in order, skipping already-applied ones.

        Migrations are expected to be named 001_*, 002_*, etc.
        Uses a migration_history table to track what has been applied.
        """
        if not self.migrations_dir.exists():
            logger.warning(f"Migrations directory not found: {self.migrations_dir}")
            return

        conn = self._require_conn()

        # Ensure migration history table exists
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS migration_history (
                filename TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """)
        await conn.commit()

        # Get already-applied migrations
        cursor = await conn.execute("SELECT filename FROM migration_history")
        rows = await cursor.fetchall()
        applied = {row[0] for row in rows}

        migration_files = sorted(self.migrations_dir.glob("*.sql"))

        for migration_file in migration_files:
            if migration_file.name in applied:
                logger.debug(
                    f"Skipping already-applied migration: {migration_file.name}"
                )
                continue

            logger.info(f"Running migration: {migration_file.name}")

            async with self._write_lock:
                sql_content = migration_file.read_text()

                # Handle ALTER TABLE ADD COLUMN separately (not idempotent in SQLite)
                sql_content = await self._apply_alter_statements(sql_content)

                try:
                    await conn.executescript(sql_content)
                    await conn.execute(
                        "INSERT INTO migration_history (filename) VALUES (?)",
                        (migration_file.name,),
                    )
                    await conn.commit()
                except aiosqlite.Error as e:
                    await conn.rollback()
                    logger.error(
                        f"Migration failed: {migration_file.name}: {e}",
                        exc_info=True,
                    )
                    raise

    async def _apply_alter_statements(self, sql_content: str) -> str:
        """
        Extract and safely execute ALTER TABLE ADD COLUMN statements.

        SQLite ALTER TABLE ADD COLUMN fails if the column already exists
        and there is no IF NOT EXISTS syntax for it. We handle each one
        individually, catching the 'duplicate column' error, then remove
        them from the script so executescript doesn't re-run them.

        Returns the SQL content with ALTER statements removed.
        """
        conn = self._require_conn()
        lines_out = []
        for line in sql_content.split("\n"):
            stripped = line.strip()
            if (
                stripped.upper().startswith("ALTER TABLE")
                and "ADD COLUMN" in stripped.upper()
            ):
                try:
                    await conn.execute(stripped)
                    await conn.commit()
                    logger.debug(f"Applied: {stripped}")
                except Exception as e:
                    if "duplicate column" in str(e).lower():
                        logger.debug(f"Column already exists, skipping: {stripped}")
                    else:
                        raise
                # Either way, don't include in the script
                lines_out.append(f"-- (applied separately) {stripped}")
            else:
                lines_out.append(line)

        return "\n".join(lines_out)

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            self._initialized = False
            logger.info("Database connection closed")

    async def execute(self, sql: str, params: tuple = ()) -> int:
        """
        Execute a single SQL statement (INSERT, UPDATE, DELETE).

        Uses write lock to ensure single-writer pattern.

        Args:
            sql: SQL statement to execute
            params: Query parameters

        Returns:
            Last row ID for inserts; 0 for UPDATE/DELETE (where lastrowid is
            undefined).
        """
        conn = self._require_conn()
        async with self._write_lock:
            try:
                cursor = await conn.execute(sql, params)
                await conn.commit()
                return cursor.lastrowid or 0
            except aiosqlite.Error as e:
                await conn.rollback()
                logger.error(f"Execute error: {e}")
                raise

    async def executemany(self, sql: str, params_list: list[tuple]) -> int:
        """
        Execute multiple SQL statements in a transaction.

        Args:
            sql: SQL statement to execute
            params_list: List of query parameter tuples

        Returns:
            Last row ID, or 0 when undefined.
        """
        conn = self._require_conn()
        async with self._write_lock:
            try:
                cursor = await conn.executemany(sql, params_list)
                await conn.commit()
                return cursor.lastrowid or 0
            except aiosqlite.Error as e:
                await conn.rollback()
                logger.error(f"Executemany error: {e}")
                raise

    async def fetch_one(self, sql: str, params: tuple = ()) -> dict | None:
        """
        Fetch a single row as a dictionary.

        Args:
            sql: SELECT query
            params: Query parameters

        Returns:
            Dictionary representation of row, or None if no results
        """
        conn = self._require_conn()
        try:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(sql, params)
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return None
        except aiosqlite.Error as e:
            logger.error(f"Fetch one error: {e}")
            raise

    async def fetch_all(self, sql: str, params: tuple = ()) -> list[dict]:
        """
        Fetch all rows as dictionaries.

        Args:
            sql: SELECT query
            params: Query parameters

        Returns:
            List of dictionary representations of rows
        """
        conn = self._require_conn()
        try:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        except aiosqlite.Error as e:
            logger.error(f"Fetch all error: {e}")
            raise

    async def fetch_val(self, sql: str, params: tuple = ()) -> Any:
        """
        Fetch a single scalar value.

        Args:
            sql: SELECT query
            params: Query parameters

        Returns:
            The first column of the first row, or None if no rows match.
        """
        conn = self._require_conn()
        try:
            cursor = await conn.execute(sql, params)
            row = await cursor.fetchone()
            if row:
                return row[0]
            return None
        except aiosqlite.Error as e:
            logger.error(f"Fetch val error: {e}")
            raise

    async def execute_script(self, sql: str) -> None:
        """
        Execute a multi-statement SQL script.

        Useful for migrations and bulk operations.

        Args:
            sql: SQL script to execute
        """
        conn = self._require_conn()
        async with self._write_lock:
            try:
                await conn.executescript(sql)
                await conn.commit()
            except aiosqlite.Error as e:
                await conn.rollback()
                logger.error(f"Script execution error: {e}")
                raise

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """
        Context manager for explicit transactions.

        Example:
            async with db.transaction():
                await db.execute(...)
                await db.execute(...)
        """
        conn = self._require_conn()
        async with self._write_lock:
            try:
                yield conn
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def vacuum(self) -> None:
        """Vacuum the database to optimize storage."""
        conn = self._require_conn()
        async with self._write_lock:
            await conn.execute("VACUUM")
            await conn.commit()
            logger.info("Database vacuumed")

    async def checkpoint(self) -> None:
        """Checkpoint the WAL, merging it into the main database file."""
        conn = self._require_conn()
        async with self._write_lock:
            await conn.execute("PRAGMA wal_checkpoint(RESTART)")
            await conn.commit()
            logger.info("WAL checkpoint completed")
