"""
Post-trade analytics engine for the prediction market trading system.

Provides StrategyScorecard for performance metrics across strategies
(summaries, daily P&L series, comparisons) used by the snapshots layer.

All methods are async and work directly with aiosqlite.Connection.
"""

import logging
import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


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
