"""
In-memory position state management with write-through to database.

Tracks open positions, calculates PnL, and periodically flushes state to SQLite.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import List

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """Represents an open trading position."""

    position_id: str
    market_id: str
    platform: str
    side: str  # "BUY" or "SELL"
    quantity: float
    entry_price: float
    entry_timestamp: float
    current_price: float | None = None
    unrealized_pnl: float = 0.0

    def update_price(self, current_price: float) -> None:
        """
        Update current price and recalculate unrealized PnL.

        Args:
            current_price: The current market price
        """
        self.current_price = current_price

        if self.side == "BUY":
            self.unrealized_pnl = (current_price - self.entry_price) * self.quantity
        else:  # SELL
            self.unrealized_pnl = (self.entry_price - current_price) * self.quantity


class PositionStateManager:
    """Manages in-memory position state with database persistence."""

    def __init__(
        self,
        db_connection: aiosqlite.Connection,
        flush_interval_s: float = 10.0,
    ) -> None:
        """
        Initialize the position state manager.

        Args:
            db_connection: SQLite connection for persistence
            flush_interval_s: Interval for periodic database flush
        """
        self.db_connection = db_connection
        self.flush_interval_s = flush_interval_s

        # In-memory position store
        self.positions: dict[str, Position] = {}

        # Pending writes for batch flushing
        self.pending_writes: list[Position] = []

        self.last_flush_time = time.time()
        self.running = True

    async def track_fill(
        self,
        order_id: str,
        market_id: str,
        platform: str,
        side: str,
        quantity: float,
        fill_price: float,
    ) -> str:
        """
        Record a filled order and create/update position.

        Args:
            order_id: The order ID
            market_id: The market ID
            platform: The platform (polymarket or kalshi)
            side: The side (BUY or SELL)
            quantity: The filled quantity
            fill_price: The fill price

        Returns:
            Position ID for tracking
        """
        position_id = f"{market_id}-{platform}-{int(time.time() * 1000)}"

        position = Position(
            position_id=position_id,
            market_id=market_id,
            platform=platform,
            side=side,
            quantity=quantity,
            entry_price=fill_price,
            entry_timestamp=time.time(),
        )

        # Store in memory
        self.positions[position_id] = position
        self.pending_writes.append(position)

        logger.info(
            "Position tracked: %s (%s %f @ %f)",
            position_id,
            side,
            quantity,
            fill_price,
        )

        # Trigger flush if enough time has passed
        if time.time() - self.last_flush_time > self.flush_interval_s:
            await self.flush_to_db()

        return position_id

    async def update_pnl(
        self,
        market_id: str,
        current_price: float,
    ) -> dict[str, float]:
        """
        Update unrealized PnL for all positions in a market.

        Args:
            market_id: The market ID
            current_price: The current market price

        Returns:
            Dictionary of position_id -> unrealized_pnl
        """
        updated_pnl: dict[str, float] = {}

        for position_id, position in self.positions.items():
            if position.market_id == market_id:
                position.update_price(current_price)
                updated_pnl[position_id] = position.unrealized_pnl
                logger.debug(
                    "Updated PnL for %s: %f (price=%f)",
                    position_id,
                    position.unrealized_pnl,
                    current_price,
                )

        return updated_pnl

    def get_open_positions(self) -> list[Position]:
        """
        Get all currently open positions.

        Returns:
            List of open positions
        """
        return list(self.positions.values())

    def get_positions_by_market(self, market_id: str) -> list[Position]:
        """
        Get all positions for a specific market.

        Args:
            market_id: The market ID

        Returns:
            List of positions for that market
        """
        return [pos for pos in self.positions.values() if pos.market_id == market_id]

    def get_position(self, position_id: str) -> Position | None:
        """
        Get a specific position by ID.

        Args:
            position_id: The position ID

        Returns:
            Position if found, None otherwise
        """
        return self.positions.get(position_id)

    async def close_position(self, position_id: str) -> bool:
        """
        Close a position (remove from in-memory store).

        Args:
            position_id: The position ID to close

        Returns:
            True if position was closed, False if not found
        """
        if position_id in self.positions:
            position = self.positions.pop(position_id)
            logger.info(
                "Position closed: %s (realized_pnl=%f)",
                position_id,
                position.unrealized_pnl,
            )
            return True

        return False

    async def flush_to_db(self) -> None:
        """Flush all pending writes to SQLite database."""
        if not self.pending_writes:
            return

        try:
            logger.debug(
                "Flushing %d position updates to database",
                len(self.pending_writes),
            )

            for position in self.pending_writes:
                await self.db_connection.execute(
                    """
                    INSERT OR REPLACE INTO positions
                    (position_id, market_id, platform, side, quantity,
                     entry_price, entry_timestamp, current_price, unrealized_pnl,
                     updated_at_utc)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    (
                        position.position_id,
                        position.market_id,
                        position.platform,
                        position.side,
                        position.quantity,
                        position.entry_price,
                        position.entry_timestamp,
                        position.current_price,
                        position.unrealized_pnl,
                    ),
                )

            await self.db_connection.commit()
            self.pending_writes.clear()
            self.last_flush_time = time.time()

            logger.info("Position state flushed to database")

        except Exception as e:
            logger.error("Error flushing to database: %s", exc_info=e)

    async def periodic_flush(self) -> None:
        """Periodically flush state to database."""
        while self.running:
            try:
                await asyncio.sleep(self.flush_interval_s)
                await self.flush_to_db()
            except asyncio.CancelledError:
                logger.info("Periodic flush task cancelled")
                break
            except Exception as e:
                logger.error("Error in periodic flush: %s", exc_info=e)

    async def shutdown(self) -> None:
        """Shutdown the state manager and flush remaining data."""
        logger.info("Shutting down position state manager")
        self.running = False
        await self.flush_to_db()

    def get_net_exposure(self, market_id: str) -> float:
        """
        Calculate net exposure for a market.

        Args:
            market_id: The market ID

        Returns:
            Net quantity (sum of buys minus sells)
        """
        positions = self.get_positions_by_market(market_id)
        buy_quantity = sum(p.quantity for p in positions if p.side == "BUY")
        sell_quantity = sum(p.quantity for p in positions if p.side == "SELL")
        return buy_quantity - sell_quantity

    def get_total_unrealized_pnl(self) -> float:
        """
        Get total unrealized PnL across all positions.

        Returns:
            Total unrealized PnL
        """
        return sum(pos.unrealized_pnl for pos in self.positions.values())

    def get_market_exposure(self) -> dict[str, float]:
        """
        Get net exposure by market.

        Returns:
            Dictionary of market_id -> net_quantity
        """
        exposure: dict[str, float] = {}

        for position in self.positions.values():
            if position.market_id not in exposure:
                exposure[position.market_id] = 0.0

            if position.side == "BUY":
                exposure[position.market_id] += position.quantity
            else:
                exposure[position.market_id] -= position.quantity

        return exposure
