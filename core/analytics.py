"""
Post-trade analytics engine for the prediction market trading system.

This module closes the quant lifecycle loop by:
- Recording fills and creating positions
- Closing positions and tracking realized P&L
- Taking portfolio snapshots with strategy breakdown
- Computing performance metrics and analytics

All methods are async and work directly with aiosqlite.Connection.
"""

import logging
import math
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


class TradeLifecycleManager:
    """
    Manages the trade lifecycle from fill to position management to closure.

    Records fills as positions, closes positions with realized P&L, and tracks
    all trade outcomes with slippage, fees, and execution metrics.
    """

    def __init__(self, db_connection: aiosqlite.Connection):
        """
        Initialize the trade lifecycle manager.

        Args:
            db_connection: Direct aiosqlite.Connection instance (not Database wrapper)
        """
        self.db = db_connection
        self.logger = logging.getLogger(f"{__name__}.TradeLifecycleManager")

    async def record_fill(
        self,
        signal_id: str,
        order_id: str,
        market_id: str,
        platform: str,
        strategy: str,
        side: str,  # "BUY" or "SELL"
        fill_price: float,
        fill_size: float,
        fee_paid: float,
        slippage: float,
        fill_latency_ms: int,
        signal_edge: float,
        violation_id: str | None = None,
    ) -> str:
        """
        Record a filled order as a new position.

        Creates a position record in the positions table with initial entry price,
        size, and metadata from the fill. The position starts in 'open' status.

        Args:
            signal_id: Unique identifier for the signal that generated this trade
            order_id: Order identifier from the execution system
            market_id: Market identifier (e.g., market slug on Polymarket)
            platform: Trading platform ("polymarket", "kalshi", etc.)
            strategy: Strategy label (P1_constraint_arb, P2_event_model, etc.)
            side: Trade side ("BUY" or "SELL")
            fill_price: Actual execution price per unit
            fill_size: Number of units filled
            fee_paid: Total fees charged for this fill
            slippage: Slippage in price (actual - signal_price, can be negative)
            fill_latency_ms: Milliseconds from signal to fill
            signal_edge: Predicted edge from the signal (%)
            violation_id: Optional violation ID if this trade has risk violations

        Returns:
            Position ID (UUID string)

        Raises:
            Exception: If database insert fails
        """
        position_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        try:
            await self.db.execute(
                """
                INSERT INTO positions (
                    id, signal_id, market_id, strategy, side,
                    entry_price, entry_size, current_price, unrealized_pnl,
                    exit_price, exit_size, realized_pnl, fees_paid,
                    status, resolution_outcome, opened_at, closed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    signal_id,
                    market_id,
                    strategy,
                    side,
                    fill_price,
                    fill_size,
                    fill_price,  # current_price starts at entry
                    0.0,  # unrealized_pnl starts at 0
                    None,  # exit_price not set yet
                    None,  # exit_size not set yet
                    0.0,  # realized_pnl not set yet
                    fee_paid,
                    "open",
                    None,  # resolution_outcome not set yet
                    now,
                    None,  # closed_at not set yet
                    now,
                ),
            )
            await self.db.commit()

            self.logger.info(
                f"Recorded fill: position_id={position_id}, signal_id={signal_id}, "
                f"market_id={market_id}, strategy={strategy}, side={side}, "
                f"fill_price={fill_price}, fill_size={fill_size}, fee={fee_paid}, "
                f"slippage={slippage}, latency_ms={fill_latency_ms}"
            )

            return position_id

        except Exception as e:
            self.logger.error(
                f"Failed to record fill: signal_id={signal_id}, order_id={order_id}, "
                f"error={str(e)}"
            )
            raise

    async def close_position(
        self,
        position_id: str,
        exit_price: float,
        exit_size: float,
        fees_paid: float = 0.0,
        resolution_outcome: str | None = None,
    ) -> str:
        """
        Close an open position and record the trade outcome.

        Computes realized P&L, updates the position to 'closed' status, and creates
        a trade_outcomes record with full trade metrics including edge capture %.

        Args:
            position_id: ID of the position to close
            exit_price: Price at exit
            exit_size: Number of units exited
            fees_paid: Total fees for the exit
            resolution_outcome: Market resolution outcome if known (e.g., "YES", "NO")

        Returns:
            Trade outcome ID (UUID string)

        Raises:
            Exception: If position not found or database operation fails
        """
        outcome_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        try:
            # Fetch the position
            cursor = await self.db.execute(
                "SELECT * FROM positions WHERE id = ?", (position_id,)
            )
            row = await cursor.fetchone()

            if not row:
                raise ValueError(f"Position not found: {position_id}")

            # Parse position columns
            cols = [desc[0] for desc in cursor.description]
            pos_dict = dict(zip(cols, row))

            signal_id = pos_dict["signal_id"]
            strategy = pos_dict["strategy"]
            market_id = pos_dict["market_id"]
            side = pos_dict["side"]
            entry_price = pos_dict["entry_price"]
            entry_size = pos_dict["entry_size"]
            entry_fees = pos_dict["fees_paid"]
            opened_at = pos_dict["opened_at"]
            violation_id = (
                None  # Not stored in positions, would need to come from orders
            )

            # Compute realized P&L
            if side == "BUY":
                realized_pnl = (exit_price - entry_price) * exit_size
            else:  # SELL
                realized_pnl = (entry_price - exit_price) * exit_size

            # Subtract total fees
            total_fees = entry_fees + fees_paid
            realized_pnl -= total_fees

            # Compute edge captured %
            # This is how much of the predicted edge we actually captured
            # edge_captured_pct = (realized_pnl / (signal_edge * entry_size * entry_price)) * 100
            # If entry_price is very small, bound it
            max_theoretical_pnl = abs(entry_size * entry_price * 0.01)  # 1% of notional
            if max_theoretical_pnl > 0:
                edge_captured_pct = (realized_pnl / max_theoretical_pnl) * 100
            else:
                edge_captured_pct = 0.0

            # Compute holding period
            opened_dt = datetime.fromisoformat(opened_at)
            now_dt = datetime.fromisoformat(now)
            holding_period_ms = int((now_dt - opened_dt).total_seconds() * 1000)

            # Compute signal-to-fill latency (from opened_at to now, placeholder)
            # In real system, would use stored signal_to_fill time from fill record
            signal_to_fill_ms = 100  # Placeholder; should come from fill metadata

            # Use placeholders for market snapshot fields
            spread_at_signal = 0.001
            volume_at_signal = 1000.0
            liquidity_at_signal = 50000.0

            # Update position
            await self.db.execute(
                """
                UPDATE positions SET
                    exit_price = ?, exit_size = ?, realized_pnl = ?,
                    status = ?, resolution_outcome = ?,
                    closed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    exit_price,
                    exit_size,
                    realized_pnl,
                    "closed",
                    resolution_outcome,
                    now,
                    now,
                    position_id,
                ),
            )

            # Insert trade outcome
            await self.db.execute(
                """
                INSERT INTO trade_outcomes (
                    id, signal_id, strategy, violation_id,
                    market_id_a, market_id_b, predicted_edge, predicted_pnl,
                    actual_pnl, fees_total, edge_captured_pct,
                    signal_to_fill_ms, holding_period_ms,
                    spread_at_signal, volume_at_signal, liquidity_at_signal,
                    resolved_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome_id,
                    signal_id,
                    strategy,
                    violation_id,
                    market_id,
                    None,  # market_id_b only for arbs
                    0.0,  # predicted_edge (placeholder; would come from signal)
                    0.0,  # predicted_pnl (placeholder)
                    realized_pnl,
                    total_fees,
                    edge_captured_pct,
                    signal_to_fill_ms,
                    holding_period_ms,
                    spread_at_signal,
                    volume_at_signal,
                    liquidity_at_signal,
                    now,  # resolved_at
                    now,
                ),
            )

            await self.db.commit()

            self.logger.info(
                f"Closed position: position_id={position_id}, outcome_id={outcome_id}, "
                f"exit_price={exit_price}, exit_size={exit_size}, "
                f"realized_pnl={realized_pnl}, fees={total_fees}, "
                f"edge_captured={edge_captured_pct:.2f}%"
            )

            return outcome_id

        except Exception as e:
            self.logger.error(
                f"Failed to close position: position_id={position_id}, error={str(e)}"
            )
            raise

    async def take_pnl_snapshot(
        self,
        total_capital: float,
        cash: float,
    ) -> int:
        """
        Take a portfolio snapshot with breakdown by strategy.

        Queries all open positions, computes unrealized P&L, aggregates realized P&L
        from closed positions today, and breaks down P&L by strategy.

        Snapshot breakdown:
        - P1: Constraint arbitrage
        - P2: Event model
        - P3: Calibration
        - P4: Liquidity
        - P5: Latency

        Args:
            total_capital: Total account capital
            cash: Available cash

        Returns:
            Snapshot ID (primary key)

        Raises:
            Exception: If database operations fail
        """
        now = datetime.now(timezone.utc).isoformat()
        today_start = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

        try:
            # Get all open positions and compute unrealized P&L
            cursor = await self.db.execute("""
                SELECT id, strategy, side, entry_price, entry_size, current_price
                FROM positions WHERE status = 'open'
                """)
            open_rows = list(await cursor.fetchall())
            cols = [desc[0] for desc in cursor.description]

            open_notional = 0.0
            unrealized_pnl = 0.0
            strategy_pnl = {
                "P1_constraint_arb": 0.0,
                "P2_event_model": 0.0,
                "P3_calibration": 0.0,
                "P4_liquidity": 0.0,
                "P5_latency": 0.0,
            }

            for row in open_rows:
                pos_dict = dict(zip(cols, row))
                side = pos_dict["side"]
                entry_price = pos_dict["entry_price"]
                entry_size = pos_dict["entry_size"]
                current_price = pos_dict["current_price"]
                strategy = pos_dict["strategy"]

                notional = entry_price * entry_size
                open_notional += notional

                if side == "BUY":
                    pos_pnl = (current_price - entry_price) * entry_size
                else:  # SELL
                    pos_pnl = (entry_price - current_price) * entry_size

                unrealized_pnl += pos_pnl

                if strategy in strategy_pnl:
                    strategy_pnl[strategy] += pos_pnl

            # Get realized P&L from closed positions today
            cursor = await self.db.execute(
                """
                SELECT strategy, realized_pnl FROM positions
                WHERE status = 'closed' AND closed_at > ?
                """,
                (today_start,),
            )
            closed_rows = await cursor.fetchall()
            cols = [desc[0] for desc in cursor.description]

            realized_pnl_today = 0.0
            for row in closed_rows:
                closed_dict = dict(zip(cols, row))
                strategy = closed_dict["strategy"]
                realized_pnl = closed_dict["actual_pnl"]

                realized_pnl_today += realized_pnl
                if strategy in strategy_pnl:
                    strategy_pnl[strategy] += realized_pnl

            # Get total realized P&L across all time
            cursor = await self.db.execute(
                "SELECT SUM(realized_pnl) FROM positions WHERE status = 'closed'"
            )
            sum_row = await cursor.fetchone()
            realized_pnl_total = sum_row[0] if sum_row and sum_row[0] else 0.0

            # Get total fees today and all time
            cursor = await self.db.execute(
                """
                SELECT SUM(fees_paid) FROM positions WHERE closed_at > ?
                """,
                (today_start,),
            )
            sum_row = await cursor.fetchone()
            fees_today = sum_row[0] if sum_row and sum_row[0] else 0.0

            cursor = await self.db.execute("SELECT SUM(fees_paid) FROM positions")
            sum_row = await cursor.fetchone()
            fees_total = sum_row[0] if sum_row and sum_row[0] else 0.0

            # Allocate capital across platforms (placeholder)
            capital_polymarket = total_capital * 0.6
            capital_kalshi = total_capital * 0.4

            # Insert snapshot
            cursor = await self.db.execute(
                """
                INSERT INTO pnl_snapshots (
                    snapshot_type, total_capital, cash,
                    open_positions_count, open_notional,
                    unrealized_pnl, realized_pnl_today, realized_pnl_total,
                    fees_today, fees_total,
                    pnl_constraint_arb, pnl_event_model, pnl_calibration,
                    pnl_liquidity, pnl_latency,
                    capital_polymarket, capital_kalshi, snapshotted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "periodic",
                    total_capital,
                    cash,
                    len(open_rows),
                    open_notional,
                    unrealized_pnl,
                    realized_pnl_today,
                    realized_pnl_total,
                    fees_today,
                    fees_total,
                    strategy_pnl["P1_constraint_arb"],
                    strategy_pnl["P2_event_model"],
                    strategy_pnl["P3_calibration"],
                    strategy_pnl["P4_liquidity"],
                    strategy_pnl["P5_latency"],
                    capital_polymarket,
                    capital_kalshi,
                    now,
                ),
            )

            snapshot_id = cursor.lastrowid or 0
            await self.db.commit()

            self.logger.info(
                f"Snapshot taken: id={snapshot_id}, capital={total_capital}, "
                f"open_positions={len(open_rows)}, unrealized_pnl={unrealized_pnl:.2f}, "
                f"realized_today={realized_pnl_today:.2f}, total_realized={realized_pnl_total:.2f}"
            )

            return snapshot_id

        except Exception as e:
            self.logger.error(f"Failed to take PnL snapshot: {str(e)}")
            raise


class StrategyScorecard:
    """
    Compute performance metrics and analytics across strategies.

    Provides strategy summaries, portfolio analytics, daily P&L series, and
    strategy comparisons for charting and reporting.
    """

    def __init__(self, db_connection: aiosqlite.Connection):
        """
        Initialize the strategy scorecard.

        Args:
            db_connection: Direct aiosqlite.Connection instance
        """
        self.db = db_connection
        self.logger = logging.getLogger(f"{__name__}.StrategyScorecard")

    async def get_strategy_summary(
        self,
        strategy: str | None = None,
        days: int = 7,
    ) -> dict[str, Any]:
        """
        Get performance summary for a strategy (or all strategies if strategy=None).

        Args:
            strategy: Strategy label (e.g., "P1_constraint_arb") or None for all
            days: Look back period in days

        Returns:
            Dict with keys:
            - total_trades: Number of closed trades
            - win_rate: Winning trades / total trades (%)
            - avg_pnl: Average P&L per trade
            - total_pnl: Sum of all P&L
            - total_fees: Sum of all fees
            - sharpe_ratio: Annualized Sharpe ratio from daily P&L
            - max_drawdown: Max peak-to-trough drawdown (%)
            - avg_edge_captured_pct: Average edge capture
            - avg_execution_latency_ms: Average signal-to-fill latency
        """
        lookback = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        try:
            # Build query
            base_query = """
                SELECT
                    id, actual_pnl, fees_total, edge_captured_pct,
                    signal_to_fill_ms, created_at
                FROM trade_outcomes
                WHERE created_at > ?
            """
            params = [lookback]

            if strategy:
                base_query += " AND strategy = ?"
                params.append(strategy)

            cursor = await self.db.execute(base_query, params)
            rows = await cursor.fetchall()
            cols = [desc[0] for desc in cursor.description]

            if not rows:
                return {
                    "total_trades": 0,
                    "win_rate": 0.0,
                    "avg_pnl": 0.0,
                    "total_pnl": 0.0,
                    "total_fees": 0.0,
                    "sharpe_ratio": 0.0,
                    "max_drawdown": 0.0,
                    "avg_edge_captured_pct": 0.0,
                    "avg_execution_latency_ms": 0.0,
                }

            # Parse trades
            trades = []
            for row in rows:
                trade_dict = dict(zip(cols, row))
                trades.append(trade_dict)

            # Compute metrics
            total_trades = len(trades)
            win_count = sum(1 for t in trades if t["actual_pnl"] > 0)
            win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0.0

            pnl_values = [t["actual_pnl"] for t in trades]
            total_pnl = sum(pnl_values)
            avg_pnl = total_pnl / total_trades if total_trades > 0 else 0.0

            total_fees = sum(t["fees_total"] for t in trades)

            # Sharpe ratio from daily P&L
            sharpe = self._compute_sharpe_ratio(pnl_values)

            # Max drawdown
            max_dd = self._compute_max_drawdown(pnl_values)

            avg_edge_captured = (
                sum(t["edge_captured_pct"] for t in trades) / total_trades
                if total_trades > 0
                else 0.0
            )

            avg_latency = (
                sum(t["signal_to_fill_ms"] for t in trades) / total_trades
                if total_trades > 0
                else 0.0
            )

            result = {
                "total_trades": total_trades,
                "win_rate": round(win_rate, 2),
                "avg_pnl": round(avg_pnl, 4),
                "total_pnl": round(total_pnl, 4),
                "total_fees": round(total_fees, 4),
                "sharpe_ratio": round(sharpe, 4),
                "max_drawdown": round(max_dd, 4),
                "avg_edge_captured_pct": round(avg_edge_captured, 2),
                "avg_execution_latency_ms": round(avg_latency, 2),
            }

            self.logger.info(
                f"Strategy summary: strategy={strategy or 'all'}, "
                f"trades={total_trades}, win_rate={win_rate:.1f}%, "
                f"total_pnl={total_pnl:.4f}, sharpe={sharpe:.4f}"
            )

            return result

        except Exception as e:
            self.logger.error(
                f"Failed to get strategy summary: strategy={strategy}, error={str(e)}"
            )
            raise

    async def get_portfolio_summary(self, days: int = 7) -> dict[str, Any]:
        """
        Get overall portfolio performance summary across all strategies.

        Args:
            days: Look back period in days

        Returns:
            Dict with overall portfolio metrics (same structure as get_strategy_summary)
        """
        return await self.get_strategy_summary(strategy=None, days=days)

    async def get_daily_pnl_series(self, days: int = 30) -> list[dict[str, Any]]:
        """
        Get daily P&L series for charting.

        Aggregates all closed trades by day and computes daily P&L, cumulative P&L,
        and trade count.

        Args:
            days: Look back period in days

        Returns:
            List of dicts, each with:
            - date: ISO date string (YYYY-MM-DD)
            - daily_pnl: P&L realized that day
            - cumulative_pnl: Running cumulative P&L
            - num_trades: Number of closed trades that day
        """
        lookback = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        try:
            cursor = await self.db.execute(
                """
                SELECT DATE(resolved_at) as date, actual_pnl
                FROM trade_outcomes
                WHERE resolved_at > ?
                ORDER BY date ASC
                """,
                (lookback,),
            )
            rows = await cursor.fetchall()

            # Aggregate by date
            daily_dict = {}
            for date_str, pnl in rows:
                if date_str not in daily_dict:
                    daily_dict[date_str] = {"pnl": 0.0, "trades": 0}
                daily_dict[date_str]["pnl"] += pnl
                daily_dict[date_str]["trades"] += 1

            # Build series with cumulative
            series = []
            cumulative = 0.0
            for date_str in sorted(daily_dict.keys()):
                daily_pnl = daily_dict[date_str]["pnl"]
                num_trades = daily_dict[date_str]["trades"]
                cumulative += daily_pnl

                series.append(
                    {
                        "date": date_str,
                        "daily_pnl": round(daily_pnl, 4),
                        "cumulative_pnl": round(cumulative, 4),
                        "num_trades": num_trades,
                    }
                )

            self.logger.info(
                f"Daily P&L series: {len(series)} days, "
                f"total_pnl={cumulative:.4f}, avg_daily={cumulative / len(series):.4f if series else 0}"
            )

            return series

        except Exception as e:
            self.logger.error(f"Failed to get daily P&L series: {str(e)}")
            raise

    async def compare_strategies(self, days: int = 7) -> dict[str, dict[str, Any]]:
        """
        Compare performance across all active strategies.

        Args:
            days: Look back period in days

        Returns:
            Dict mapping strategy names to their summary metrics
        """
        strategies = [
            "P1_constraint_arb",
            "P2_event_model",
            "P3_calibration",
            "P4_liquidity",
            "P5_latency",
        ]

        comparison = {}
        for strat in strategies:
            try:
                summary = await self.get_strategy_summary(strategy=strat, days=days)
                if summary["total_trades"] > 0:  # Only include active strategies
                    comparison[strat] = summary
            except Exception as e:
                self.logger.warning(f"Failed to get summary for {strat}: {str(e)}")

        self.logger.info(
            f"Strategy comparison: {len(comparison)} active strategies over {days} days"
        )

        return comparison

    # Private helper methods

    def _compute_sharpe_ratio(
        self, pnl_values: list[float], risk_free_rate: float = 0.01
    ) -> float:
        """
        Compute annualized Sharpe ratio from daily P&L values.

        Args:
            pnl_values: List of daily P&L values
            risk_free_rate: Annual risk-free rate (default 1%)

        Returns:
            Annualized Sharpe ratio
        """
        if not pnl_values or len(pnl_values) < 2:
            return 0.0

        try:
            mean_pnl = statistics.mean(pnl_values)
            std_pnl = statistics.stdev(pnl_values)

            if std_pnl == 0:
                return 0.0

            # Annualize: assume 252 trading days per year
            daily_sharpe = (mean_pnl - (risk_free_rate / 252)) / std_pnl
            annual_sharpe = daily_sharpe * math.sqrt(252)

            return annual_sharpe
        except Exception as e:
            self.logger.warning(f"Failed to compute Sharpe ratio: {str(e)}")
            return 0.0

    def _compute_max_drawdown(self, pnl_values: list[float]) -> float:
        """
        Compute maximum peak-to-trough drawdown as a percentage.

        Args:
            pnl_values: List of P&L values

        Returns:
            Max drawdown as a percentage
        """
        if not pnl_values:
            return 0.0

        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0

        for pnl in pnl_values:
            cumulative += pnl
            if cumulative > peak:
                peak = cumulative

            drawdown = (peak - cumulative) / abs(peak) if peak != 0 else 0
            if drawdown > max_dd:
                max_dd = drawdown

        return max_dd * 100  # Return as percentage
