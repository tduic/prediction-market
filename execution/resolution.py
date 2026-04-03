"""
Resolution monitor for market resolution and position closure.

Monitors market resolutions, closes positions automatically, and records
trade outcomes when markets resolve. Handles both automatic resolution-based
closure and manual position exits.
"""

import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from execution.state import PositionStateManager

logger = logging.getLogger(__name__)


class ResolutionMonitor:
    """Monitor market resolutions and close positions automatically."""

    def __init__(
        self,
        db_connection: aiosqlite.Connection,
        position_state: PositionStateManager,
    ) -> None:
        """
        Initialize the resolution monitor.

        Args:
            db_connection: SQLite connection for persistence
            position_state: Position state manager for in-memory tracking
        """
        self.db_connection = db_connection
        self.position_state = position_state

    async def check_resolutions(self) -> dict[str, int | float]:
        """
        Check for resolved markets and close associated positions.

        Queries all open positions, checks market status, and automatically
        closes positions when their markets resolve. Computes realized PnL
        and writes trade outcome records.

        Returns:
            Summary dict with: checked (int), closed (int), total_realized_pnl (float)
        """
        summary = {
            "checked": 0,
            "closed": 0,
            "total_realized_pnl": 0.0,
        }

        try:
            # Query all open positions from DB
            cursor = await self.db_connection.execute(
                """
                SELECT id, market_id, side, entry_price, entry_size,
                       signal_id, strategy
                FROM positions
                WHERE status = 'open'
                """
            )
            open_positions = await cursor.fetchall()
            summary["checked"] = len(open_positions)

            if not open_positions:
                logger.debug("No open positions to check for resolution")
                return summary

            logger.info("Checking resolution status for %d open positions", len(open_positions))

            for position_row in open_positions:
                (
                    position_id,
                    market_id,
                    side,
                    entry_price,
                    entry_size,
                    signal_id,
                    strategy,
                ) = position_row

                try:
                    # Check market status
                    market_cursor = await self.db_connection.execute(
                        """
                        SELECT status, outcome, outcome_value
                        FROM markets
                        WHERE id = ?
                        """,
                        (market_id,),
                    )
                    market_row = await market_cursor.fetchone()

                    if not market_row:
                        logger.warning(
                            "Market not found for position: %s (market_id=%s)",
                            position_id,
                            market_id,
                        )
                        continue

                    market_status, outcome, outcome_value = market_row

                    # Process if market is resolved or closed
                    if market_status in ("resolved", "closed"):
                        # Compute exit price: 1.0 if outcome matches position side, 0.0 otherwise
                        # For binary markets, outcome_value is the probability
                        if outcome_value is not None:
                            # outcome_value is the probability/price of the predicted outcome
                            exit_price = float(outcome_value)
                        else:
                            # Fallback: if outcome matches the side expectation, use 1.0, else 0.0
                            exit_price = 1.0 if outcome else 0.0

                        # Compute realized PnL
                        if side == "BUY":
                            realized_pnl = (exit_price - entry_price) * entry_size
                        else:  # SELL
                            realized_pnl = (entry_price - exit_price) * entry_size

                        # Update positions table
                        await self.db_connection.execute(
                            """
                            UPDATE positions
                            SET status = 'closed',
                                exit_price = ?,
                                exit_size = ?,
                                realized_pnl = ?,
                                resolution_outcome = ?,
                                closed_at = ?
                            WHERE id = ?
                            """,
                            (
                                exit_price,
                                entry_size,
                                realized_pnl,
                                outcome,
                                datetime.now(timezone.utc).isoformat(),
                                position_id,
                            ),
                        )

                        # Write trade outcome record
                        await self._write_trade_outcome(
                            position_id,
                            signal_id,
                            strategy,
                            market_id,
                            exit_price,
                            entry_price,
                            entry_size,
                            realized_pnl,
                        )

                        # Remove from in-memory position store
                        await self.position_state.close_position(
                            position_id,
                            exit_price,
                            resolution_outcome=outcome,
                        )

                        summary["closed"] += 1
                        summary["total_realized_pnl"] += realized_pnl

                        logger.info(
                            "Position closed by market resolution: %s (pnl=%.6f, outcome=%s)",
                            position_id,
                            realized_pnl,
                            outcome,
                        )

                except Exception as e:
                    logger.error(
                        "Error processing position closure: %s (position_id=%s)",
                        e,
                        position_id,
                        exc_info=True,
                    )
                    continue

            await self.db_connection.commit()
            logger.info(
                "Resolution check completed: closed=%d, total_pnl=%.6f",
                summary["closed"],
                summary["total_realized_pnl"],
            )

        except Exception as e:
            logger.error("Error checking resolutions: %s", e, exc_info=True)

        return summary

    async def force_close_position(
        self,
        position_id: str,
        exit_price: float,
        reason: str = "manual",
    ) -> dict[str, Any] | None:
        """
        Manually close a specific position at a given price.

        Used for stop losses, manual exits, or emergency closures.

        Args:
            position_id: The position ID to close
            exit_price: The exit price
            reason: Reason for closure (manual, stop_loss, liquidation, etc.)

        Returns:
            Dict with closure details or None if position not found
        """
        try:
            # Get position from DB
            cursor = await self.db_connection.execute(
                """
                SELECT id, market_id, side, entry_price, entry_size,
                       signal_id, strategy, status
                FROM positions
                WHERE id = ?
                """,
                (position_id,),
            )
            position_row = await cursor.fetchone()

            if not position_row:
                logger.warning("Position not found for force close: %s", position_id)
                return None

            (
                pos_id,
                market_id,
                side,
                entry_price,
                entry_size,
                signal_id,
                strategy,
                status,
            ) = position_row

            if status != "open":
                logger.warning(
                    "Cannot close non-open position: %s (status=%s)",
                    position_id,
                    status,
                )
                return None

            # Compute realized PnL
            if side == "BUY":
                realized_pnl = (exit_price - entry_price) * entry_size
            else:  # SELL
                realized_pnl = (entry_price - exit_price) * entry_size

            # Update positions table
            await self.db_connection.execute(
                """
                UPDATE positions
                SET status = 'closed',
                    exit_price = ?,
                    exit_size = ?,
                    realized_pnl = ?,
                    resolution_outcome = ?,
                    closed_at = ?
                WHERE id = ?
                """,
                (
                    exit_price,
                    entry_size,
                    realized_pnl,
                    reason,
                    datetime.now(timezone.utc).isoformat(),
                    position_id,
                ),
            )

            # Write trade outcome record
            await self._write_trade_outcome(
                position_id,
                signal_id,
                strategy,
                market_id,
                exit_price,
                entry_price,
                entry_size,
                realized_pnl,
            )

            # Remove from in-memory position store
            await self.position_state.close_position(
                position_id,
                exit_price,
                resolution_outcome=reason,
            )

            await self.db_connection.commit()

            closure_details = {
                "position_id": position_id,
                "market_id": market_id,
                "side": side,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "entry_size": entry_size,
                "realized_pnl": realized_pnl,
                "reason": reason,
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }

            logger.info(
                "Position force closed: %s (exit_price=%.6f, pnl=%.6f, reason=%s)",
                position_id,
                exit_price,
                realized_pnl,
                reason,
            )

            return closure_details

        except Exception as e:
            logger.error(
                "Error force closing position: %s (position_id=%s)",
                e,
                position_id,
                exc_info=True,
            )
            return None

    async def get_stale_positions(
        self,
        threshold_hours: float = 72.0,
    ) -> list[dict[str, Any]]:
        """
        Get positions that have been open longer than threshold.

        Useful for monitoring and alerting on long-held positions.

        Args:
            threshold_hours: Hours threshold (default 72)

        Returns:
            List of stale position dicts
        """
        try:
            # Calculate timestamp threshold
            threshold_seconds = threshold_hours * 3600
            cursor = await self.db_connection.execute(
                """
                SELECT id, market_id, side, entry_price, entry_size,
                       opened_at, current_price, unrealized_pnl,
                       (strftime('%s', 'now') - strftime('%s', opened_at)) as age_seconds
                FROM positions
                WHERE status = 'open'
                  AND (strftime('%s', 'now') - strftime('%s', opened_at)) > ?
                ORDER BY age_seconds DESC
                """,
                (threshold_seconds,),
            )
            stale_rows = await cursor.fetchall()

            stale_positions = []
            for row in stale_rows:
                (
                    pos_id,
                    market_id,
                    side,
                    entry_price,
                    entry_size,
                    opened_at,
                    current_price,
                    unrealized_pnl,
                    age_seconds,
                ) = row

                stale_positions.append({
                    "position_id": pos_id,
                    "market_id": market_id,
                    "side": side,
                    "entry_price": entry_price,
                    "entry_size": entry_size,
                    "current_price": current_price,
                    "unrealized_pnl": unrealized_pnl,
                    "opened_at": opened_at,
                    "age_hours": age_seconds / 3600.0,
                })

            logger.debug(
                "Found %d stale positions (threshold=%f hours)",
                len(stale_positions),
                threshold_hours,
            )

            return stale_positions

        except Exception as e:
            logger.error("Error fetching stale positions: %s", e, exc_info=True)
            return []

    async def _write_trade_outcome(
        self,
        position_id: str,
        signal_id: str,
        strategy: str,
        market_id: str,
        exit_price: float,
        entry_price: float,
        entry_size: float,
        realized_pnl: float,
    ) -> None:
        """
        Write a trade outcome record to the database.

        Args:
            position_id: The position ID
            signal_id: The signal ID
            strategy: The strategy name
            market_id: The market ID
            exit_price: The exit price
            entry_price: The entry price
            entry_size: The entry size
            realized_pnl: The realized PnL
        """
        try:
            trade_outcome_id = f"outcome-{position_id}"

            await self.db_connection.execute(
                """
                INSERT OR REPLACE INTO trade_outcomes
                (id, signal_id, strategy, market_id_a,
                 predicted_pnl, actual_pnl, resolved_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade_outcome_id,
                    signal_id,
                    strategy,
                    market_id,
                    0.0,  # predicted_pnl would come from signal, using 0 as placeholder
                    realized_pnl,
                    datetime.now(timezone.utc).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

        except Exception as e:
            logger.error(
                "Error writing trade outcome: %s (position_id=%s)",
                e,
                position_id,
                exc_info=True,
            )
