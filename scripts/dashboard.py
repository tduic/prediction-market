#!/usr/bin/env python3
"""
Prediction Market Trading System - Analytics Dashboard CLI

Displays comprehensive trading performance metrics, strategy breakdowns,
execution quality, risk metrics, and recent trades in a professional
formatted terminal output.

Usage:
    python scripts/dashboard.py --db-path ./data/mock_session.db
    python scripts/dashboard.py --db-path ./data/live.db --days 30 --format json
"""

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict
import statistics

import aiosqlite


# ANSI color codes
class Color:
    """ANSI color codes for terminal output."""

    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    WHITE = "\033[97m"
    DIM = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    @classmethod
    def format(cls, text: str, color: str, bold: bool = False) -> str:
        """Format text with color if stdout is a TTY."""
        if not sys.stdout.isatty():
            return text
        prefix = cls.BOLD if bold else ""
        return f"{prefix}{color}{text}{cls.RESET}"


@dataclass
class PortfolioMetrics:
    """Portfolio-level metrics."""

    total_capital: float
    cash: float
    deployed: float
    open_positions: int
    unrealized_pnl: float
    realized_pnl_today: float
    realized_pnl_total: float
    fees_total: float
    net_return_pct: float


@dataclass
class StrategyMetrics:
    """Per-strategy metrics."""

    strategy: str
    trades: int
    wins: int
    avg_pnl: float
    total_pnl: float
    fees: float
    sharpe: float
    avg_edge_cap: float


@dataclass
class ExecutionMetrics:
    """Order execution quality metrics."""

    total_orders: int
    filled_orders: int
    fill_rate: float
    avg_submission_latency_ms: float
    avg_fill_latency_ms: float
    avg_slippage: float
    total_slippage_cost: float
    partial_fills: int
    rejections: int
    polymarket_fill_rate: float
    kalshi_fill_rate: float


@dataclass
class RiskMetrics:
    """Risk and exposure metrics."""

    max_loss: float
    max_drawdown: float
    largest_win: float
    win_loss_ratio: float
    avg_holding_period_hours: float
    max_concentration_pct: float


class Dashboard:
    """Analytics dashboard for prediction market trading system."""

    def __init__(self, db_path: str, days: int = 7, format_type: str = "terminal"):
        """Initialize dashboard with database path."""
        self.db_path = db_path
        self.days = days
        self.format_type = format_type
        self.generated_at = datetime.now(timezone.utc)

    async def run(self):
        """Execute dashboard and print report."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Determine mode from database
            mode = await self._detect_mode(db)

            # Gather metrics
            portfolio = await self._get_portfolio_metrics(db)
            strategies = await self._get_strategy_metrics(db)
            execution = await self._get_execution_metrics(db)
            risk = await self._get_risk_metrics(db)
            recent_trades = await self._get_recent_trades(db, limit=10)
            violations = await self._get_violations_summary(db)

            if self.format_type == "json":
                self._output_json(
                    portfolio,
                    strategies,
                    execution,
                    risk,
                    recent_trades,
                    violations,
                    mode,
                )
            else:
                self._output_terminal(
                    portfolio,
                    strategies,
                    execution,
                    risk,
                    recent_trades,
                    violations,
                    mode,
                )

    def _output_terminal(
        self,
        portfolio: PortfolioMetrics,
        strategies: list[StrategyMetrics],
        execution: ExecutionMetrics,
        risk: RiskMetrics,
        recent_trades: list[Dict],
        violations: Dict,
        mode: str,
    ):
        """Output formatted terminal report."""
        # Header
        self._print_header(mode)

        # Portfolio Summary
        self._print_portfolio_summary(portfolio)

        # Strategy Breakdown
        self._print_strategy_breakdown(strategies)

        # Execution Quality
        self._print_execution_quality(execution)

        # Risk Metrics
        self._print_risk_metrics(risk)

        # Recent Trades
        self._print_recent_trades(recent_trades)

        # Violations & Signals
        self._print_violations_summary(violations)

    def _print_header(self, mode: str):
        """Print formatted header."""
        header_line = "=" * 70
        print(f"\n{Color.format(header_line, Color.CYAN, bold=True)}")
        print(
            Color.format(
                " PREDICTION MARKET TRADING SYSTEM — PERFORMANCE DASHBOARD",
                Color.WHITE,
                bold=True,
            )
        )
        generated_str = self.generated_at.strftime("%Y-%m-%dT%H:%M:%S")
        db_name = Path(self.db_path).name
        info_line = f" Generated: {generated_str}  |  Mode: {mode}  |  DB: {db_name}"
        print(Color.format(info_line, Color.DIM))
        print(f"{Color.format(header_line, Color.CYAN, bold=True)}\n")

    def _print_portfolio_summary(self, portfolio: PortfolioMetrics):
        """Print portfolio summary section."""
        print(Color.format("PORTFOLIO SUMMARY", Color.WHITE, bold=True))
        print(Color.format("─" * 70, Color.DIM))

        data = [
            ("Total Capital", f"${portfolio.total_capital:,.2f}"),
            ("Cash Available", f"${portfolio.cash:,.2f}"),
            ("Capital Deployed", f"${portfolio.deployed:,.2f}"),
            ("Open Positions", f"{portfolio.open_positions}"),
            (
                "Unrealized P&L",
                Color.format(
                    f"${portfolio.unrealized_pnl:,.2f}",
                    Color.GREEN if portfolio.unrealized_pnl >= 0 else Color.RED,
                ),
            ),
            (
                "Realized P&L (Today)",
                Color.format(
                    f"${portfolio.realized_pnl_today:,.2f}",
                    Color.GREEN if portfolio.realized_pnl_today >= 0 else Color.RED,
                ),
            ),
            (
                "Realized P&L (Total)",
                Color.format(
                    f"${portfolio.realized_pnl_total:,.2f}",
                    Color.GREEN if portfolio.realized_pnl_total >= 0 else Color.RED,
                ),
            ),
            ("Total Fees Paid", f"${portfolio.fees_total:,.2f}"),
            (
                "Net Return %",
                Color.format(
                    f"{portfolio.net_return_pct:.2f}%",
                    Color.GREEN if portfolio.net_return_pct >= 0 else Color.RED,
                    bold=True,
                ),
            ),
        ]

        for label, value in data:
            print(f"  {label:<25} {value:>40}")

        print(f"{Color.format('─' * 70, Color.DIM)}\n")

    def _print_strategy_breakdown(self, strategies: list[StrategyMetrics]):
        """Print strategy breakdown table."""
        print(Color.format("STRATEGY BREAKDOWN", Color.WHITE, bold=True))
        print(Color.format("─" * 100, Color.DIM))

        # Header row
        header = (
            f"{'Strategy':<25} {'Trades':>8} {'Win%':>8} {'Avg PnL':>12} "
            f"{'Total PnL':>12} {'Fees':>10} {'Sharpe':>8} {'Avg Edge Cap':>12}"
        )
        print(Color.format(header, Color.CYAN))
        print(Color.format("─" * 100, Color.DIM))

        if not strategies:
            print(Color.format("  No strategies executed", Color.DIM))
        else:
            for strategy in strategies:
                win_pct = (
                    (strategy.wins / strategy.trades * 100)
                    if strategy.trades > 0
                    else 0
                )

                # Color code the PnL values
                total_pnl_str = Color.format(
                    f"${strategy.total_pnl:,.2f}",
                    Color.GREEN if strategy.total_pnl >= 0 else Color.RED,
                )

                row = (
                    f"{strategy.strategy:<25} {strategy.trades:>8} {win_pct:>7.1f}% "
                    f"${strategy.avg_pnl:>10.2f}  {total_pnl_str:>12}  "
                    f"${strategy.fees:>8.2f}  {strategy.sharpe:>7.2f}  "
                    f"{strategy.avg_edge_cap:>11.1f}%"
                )
                print(row)

            # Total row
            total_trades = sum(s.trades for s in strategies)
            total_wins = sum(s.wins for s in strategies)
            total_avg_pnl = (
                sum(s.total_pnl for s in strategies) / total_trades
                if total_trades > 0
                else 0
            )
            total_pnl = sum(s.total_pnl for s in strategies)
            total_fees = sum(s.fees for s in strategies)
            total_win_pct = (total_wins / total_trades * 100) if total_trades > 0 else 0
            avg_sharpe = (
                statistics.mean(s.sharpe for s in strategies if s.sharpe > 0)
                if any(s.sharpe > 0 for s in strategies)
                else 0
            )
            avg_cap = (
                statistics.mean(s.avg_edge_cap for s in strategies) if strategies else 0
            )

            total_pnl_str = Color.format(
                f"${total_pnl:,.2f}",
                Color.GREEN if total_pnl >= 0 else Color.RED,
                bold=True,
            )

            print(Color.format("─" * 100, Color.DIM))
            row = (
                f"{'TOTAL':<25} {total_trades:>8} {total_win_pct:>7.1f}% "
                f"${total_avg_pnl:>10.2f}  {total_pnl_str:>12}  "
                f"${total_fees:>8.2f}  {avg_sharpe:>7.2f}  {avg_cap:>11.1f}%"
            )
            print(Color.format(row, Color.WHITE, bold=True))

        print(f"{Color.format('─' * 100, Color.DIM)}\n")

    def _print_execution_quality(self, execution: ExecutionMetrics):
        """Print execution quality metrics."""
        print(Color.format("EXECUTION QUALITY", Color.WHITE, bold=True))
        print(Color.format("─" * 70, Color.DIM))

        data = [
            ("Total Orders", f"{execution.total_orders}"),
            ("Filled Orders", f"{execution.filled_orders}"),
            (
                "Fill Rate",
                Color.format(
                    f"{execution.fill_rate:.1f}%",
                    Color.GREEN if execution.fill_rate > 85 else Color.YELLOW,
                ),
            ),
            ("Avg Submission Latency", f"{execution.avg_submission_latency_ms:.1f} ms"),
            ("Avg Fill Latency", f"{execution.avg_fill_latency_ms:.1f} ms"),
            (
                "Avg Slippage",
                Color.format(
                    f"${execution.avg_slippage:.4f}",
                    Color.RED if execution.avg_slippage > 0.01 else Color.GREEN,
                ),
            ),
            ("Total Slippage Cost", f"${execution.total_slippage_cost:,.2f}"),
            ("Partial Fills", f"{execution.partial_fills}"),
            ("Rejections", f"{execution.rejections}"),
        ]

        for label, value in data:
            print(f"  {label:<30} {value:>35}")

        print("\n  Platform Breakdown:")
        print(
            f"    Polymarket Fill Rate:  "
            f"{Color.format(f'{execution.polymarket_fill_rate:.1f}%', Color.GREEN if execution.polymarket_fill_rate > 85 else Color.YELLOW)}"
        )
        print(
            f"    Kalshi Fill Rate:      "
            f"{Color.format(f'{execution.kalshi_fill_rate:.1f}%', Color.GREEN if execution.kalshi_fill_rate > 85 else Color.YELLOW)}"
        )

        print(f"{Color.format('─' * 70, Color.DIM)}\n")

    def _print_risk_metrics(self, risk: RiskMetrics):
        """Print risk metrics."""
        print(Color.format("RISK METRICS", Color.WHITE, bold=True))
        print(Color.format("─" * 70, Color.DIM))

        data = [
            (
                "Max Single-Trade Loss",
                Color.format(f"${risk.max_loss:,.2f}", Color.RED),
            ),
            (
                "Max Drawdown",
                Color.format(f"{risk.max_drawdown:.2f}%", Color.RED),
            ),
            (
                "Largest Winning Trade",
                Color.format(f"${risk.largest_win:,.2f}", Color.GREEN),
            ),
            ("Win/Loss Ratio", f"{risk.win_loss_ratio:.2f}"),
            ("Avg Holding Period", f"{risk.avg_holding_period_hours:.1f} hours"),
            (
                "Max Capital Concentration",
                Color.format(
                    f"{risk.max_concentration_pct:.1f}%",
                    Color.RED if risk.max_concentration_pct > 20 else Color.YELLOW,
                ),
            ),
        ]

        for label, value in data:
            print(f"  {label:<30} {value:>35}")

        print(f"{Color.format('─' * 70, Color.DIM)}\n")

    def _print_recent_trades(self, trades: list[Dict]):
        """Print recent trades table."""
        print(Color.format("RECENT TRADES (Last 10)", Color.WHITE, bold=True))
        print(Color.format("─" * 110, Color.DIM))

        if not trades:
            print(Color.format("  No trades to display", Color.DIM))
        else:
            # Header
            header = (
                f"{'Time':<20} {'Market':<30} {'Strategy':<18} {'Side':<6} "
                f"{'Entry':>8} {'Exit':>8} {'P&L':>10} {'Fees':>8}"
            )
            print(Color.format(header, Color.CYAN))
            print(Color.format("─" * 110, Color.DIM))

            for trade in trades:
                pnl = trade.get("realized_pnl", 0) or 0
                time_str = trade.get("closed_at", "")[:19] or "—"
                market = (trade.get("market_title", "") or "Unknown")[:30]
                strategy = trade.get("strategy", "")[:18]
                side = trade.get("side", "")
                entry = f"${trade.get('entry_price', 0):.3f}"
                exit_price = f"${trade.get('exit_price', 0):.3f}"

                pnl_str = Color.format(
                    f"${pnl:,.2f}", Color.GREEN if pnl >= 0 else Color.RED
                )
                fees = f"${trade.get('fees_paid', 0):.2f}"

                row = (
                    f"{time_str:<20} {market:<30} {strategy:<18} {side:<6} "
                    f"{entry:>8} {exit_price:>8} {pnl_str:>10} {fees:>8}"
                )
                print(row)

        print(f"{Color.format('─' * 110, Color.DIM)}\n")

    def _print_violations_summary(self, violations: Dict):
        """Print violations and signals summary."""
        print(Color.format("VIOLATIONS & SIGNALS SUMMARY", Color.WHITE, bold=True))
        print(Color.format("─" * 70, Color.DIM))

        data = [
            ("Total Violations Detected", f"{violations['total']:,}"),
            ("Converted to Signals", f"{violations['signals_created']:,}"),
            ("Successfully Executed", f"{violations['executed']:,}"),
            ("Conversion Rate", f"{violations['conversion_rate']:.1f}%"),
            ("Execution Rate", f"{violations['execution_rate']:.1f}%"),
            (
                "Avg Violation→Signal Time",
                f"{violations['avg_detect_to_signal_ms']:.0f} ms",
            ),
            ("Avg Signal→Fill Time", f"{violations['avg_signal_to_fill_ms']:.0f} ms"),
            (
                "Total Pipeline Latency",
                f"{violations['total_pipeline_latency_ms']:.0f} ms",
            ),
        ]

        for label, value in data:
            print(f"  {label:<35} {value:>30}")

        if violations["rejection_reasons"]:
            print(f"\n  {Color.format('Signal Rejection Reasons:', Color.YELLOW)}")
            for reason, count in sorted(
                violations["rejection_reasons"].items(), key=lambda x: -x[1]
            ):
                print(f"    {reason:<40} {count:>5}")

        print(f"{Color.format('─' * 70, Color.DIM)}\n")

    def _output_json(
        self,
        portfolio: PortfolioMetrics,
        strategies: list[StrategyMetrics],
        execution: ExecutionMetrics,
        risk: RiskMetrics,
        recent_trades: list[Dict],
        violations: Dict,
        mode: str,
    ):
        """Output JSON format."""
        output = {
            "timestamp": self.generated_at.isoformat(),
            "mode": mode,
            "portfolio": {
                "total_capital": portfolio.total_capital,
                "cash": portfolio.cash,
                "deployed": portfolio.deployed,
                "open_positions": portfolio.open_positions,
                "unrealized_pnl": portfolio.unrealized_pnl,
                "realized_pnl_today": portfolio.realized_pnl_today,
                "realized_pnl_total": portfolio.realized_pnl_total,
                "fees_total": portfolio.fees_total,
                "net_return_pct": portfolio.net_return_pct,
            },
            "strategies": [
                {
                    "name": s.strategy,
                    "trades": s.trades,
                    "wins": s.wins,
                    "win_rate": (s.wins / s.trades * 100) if s.trades > 0 else 0,
                    "avg_pnl": s.avg_pnl,
                    "total_pnl": s.total_pnl,
                    "fees": s.fees,
                    "sharpe_ratio": s.sharpe,
                    "avg_edge_cap": s.avg_edge_cap,
                }
                for s in strategies
            ],
            "execution": {
                "total_orders": execution.total_orders,
                "filled_orders": execution.filled_orders,
                "fill_rate": execution.fill_rate,
                "avg_submission_latency_ms": execution.avg_submission_latency_ms,
                "avg_fill_latency_ms": execution.avg_fill_latency_ms,
                "avg_slippage": execution.avg_slippage,
                "total_slippage_cost": execution.total_slippage_cost,
                "partial_fills": execution.partial_fills,
                "rejections": execution.rejections,
                "platform_breakdown": {
                    "polymarket_fill_rate": execution.polymarket_fill_rate,
                    "kalshi_fill_rate": execution.kalshi_fill_rate,
                },
            },
            "risk": {
                "max_loss": risk.max_loss,
                "max_drawdown": risk.max_drawdown,
                "largest_win": risk.largest_win,
                "win_loss_ratio": risk.win_loss_ratio,
                "avg_holding_period_hours": risk.avg_holding_period_hours,
                "max_concentration_pct": risk.max_concentration_pct,
            },
            "violations": violations,
            "recent_trades": recent_trades,
        }
        print(json.dumps(output, indent=2, default=str))

    async def _detect_mode(self, db: aiosqlite.Connection) -> str:
        """Detect if database is in mock or live mode."""
        # Simple heuristic: check for high number of recent trades with same timestamps
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM orders WHERE submitted_at > datetime('now', '-1 day')"
        )
        row = await cursor.fetchone()
        if row and row["cnt"] > 100:
            return "mock"
        return "live"

    async def _get_portfolio_metrics(
        self, db: aiosqlite.Connection
    ) -> PortfolioMetrics:
        """Get portfolio-level metrics from pnl_snapshots."""
        cursor = await db.execute("""
            SELECT
                total_capital, cash, open_positions_count,
                unrealized_pnl, realized_pnl_today, realized_pnl_total, fees_total
            FROM pnl_snapshots
            ORDER BY snapshotted_at DESC
            LIMIT 1
            """)
        row = await cursor.fetchone()

        if row:
            total_capital = row["total_capital"] or 0
            cash = row["cash"] or 0
            deployed = total_capital - cash
            unrealized = row["unrealized_pnl"] or 0
            realized_today = row["realized_pnl_today"] or 0
            realized_total = row["realized_pnl_total"] or 0
            fees = row["fees_total"] or 0
            net_return = (
                (realized_total / total_capital * 100) if total_capital > 0 else 0
            )

            return PortfolioMetrics(
                total_capital=total_capital,
                cash=cash,
                deployed=deployed,
                open_positions=row["open_positions_count"] or 0,
                unrealized_pnl=unrealized,
                realized_pnl_today=realized_today,
                realized_pnl_total=realized_total,
                fees_total=fees,
                net_return_pct=net_return,
            )

        # Default empty metrics
        return PortfolioMetrics(
            total_capital=0,
            cash=0,
            deployed=0,
            open_positions=0,
            unrealized_pnl=0,
            realized_pnl_today=0,
            realized_pnl_total=0,
            fees_total=0,
            net_return_pct=0,
        )

    async def _get_strategy_metrics(
        self, db: aiosqlite.Connection
    ) -> list[StrategyMetrics]:
        """Get per-strategy metrics from trade_outcomes and signals."""
        cursor = await db.execute(
            """
            SELECT
                t.strategy,
                COUNT(t.id) as trade_count,
                SUM(CASE WHEN t.actual_pnl > 0 THEN 1 ELSE 0 END) as wins,
                AVG(t.actual_pnl) as avg_pnl,
                SUM(t.actual_pnl) as total_pnl,
                SUM(t.fees_total) as total_fees,
                AVG(CAST(t.actual_pnl AS FLOAT) / NULLIF(t.predicted_pnl, 0)) as edge_captured_pct,
                AVG(s.model_edge) as avg_edge
            FROM trade_outcomes t
            LEFT JOIN signals s ON t.signal_id = s.id
            WHERE t.resolved_at > datetime('now', '-' || ? || ' days')
            GROUP BY t.strategy
            ORDER BY total_pnl DESC
            """,
            (self.days,),
        )
        rows = await cursor.fetchall()

        metrics = []
        for row in rows:
            trade_count = row["trade_count"] or 0
            if trade_count == 0:
                continue

            wins = row["wins"] or 0
            avg_pnl = row["avg_pnl"] or 0
            total_pnl = row["total_pnl"] or 0
            total_fees = row["total_fees"] or 0
            edge_cap = row["edge_captured_pct"] or 0
            _avg_edge = row["avg_edge"] or 0

            # Calculate Sharpe ratio (SQLite lacks STDEV, compute in Python)
            cursor2 = await db.execute(
                """
                SELECT actual_pnl FROM trade_outcomes
                WHERE strategy = ? AND resolved_at > datetime('now', '-' || ? || ' days')
                """,
                (row["strategy"], self.days),
            )
            pnl_rows = await cursor2.fetchall()
            if len(pnl_rows) > 1:
                pnl_values = [
                    r["actual_pnl"] for r in pnl_rows if r["actual_pnl"] is not None
                ]
                mean_pnl = sum(pnl_values) / len(pnl_values) if pnl_values else 0
                variance = (
                    sum((x - mean_pnl) ** 2 for x in pnl_values) / len(pnl_values)
                    if pnl_values
                    else 0
                )
                pnl_std = variance**0.5
            else:
                pnl_std = 0
            sharpe = (avg_pnl / pnl_std) if pnl_std > 0 else 0

            # Capital deployment
            avg_edge_cap = (edge_cap * 100) if edge_cap else 0

            metrics.append(
                StrategyMetrics(
                    strategy=row["strategy"] or "unknown",
                    trades=trade_count,
                    wins=wins,
                    avg_pnl=avg_pnl,
                    total_pnl=total_pnl,
                    fees=total_fees,
                    sharpe=sharpe,
                    avg_edge_cap=avg_edge_cap,
                )
            )

        return metrics

    async def _get_execution_metrics(
        self, db: aiosqlite.Connection
    ) -> ExecutionMetrics:
        """Get execution quality metrics from orders table."""
        # Overall metrics
        cursor = await db.execute(
            """
            SELECT
                COUNT(*) as total_orders,
                SUM(CASE WHEN status = 'filled' THEN 1 ELSE 0 END) as filled_orders,
                AVG(submission_latency_ms) as avg_submission_latency,
                AVG(fill_latency_ms) as avg_fill_latency,
                AVG(ABS(slippage)) as avg_slippage,
                SUM(ABS(slippage)) as total_slippage_cost,
                SUM(CASE WHEN filled_size < requested_size AND filled_size > 0 THEN 1 ELSE 0 END) as partial_fills,
                SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) as rejections
            FROM orders
            WHERE submitted_at > datetime('now', '-' || ? || ' days')
            """,
            (self.days,),
        )
        row = await cursor.fetchone()

        total_orders = row["total_orders"] or 0
        filled_orders = row["filled_orders"] or 0
        fill_rate = (filled_orders / total_orders * 100) if total_orders > 0 else 0

        # Platform-specific metrics
        cursor = await db.execute(
            """
            SELECT
                platform,
                SUM(CASE WHEN status = 'filled' THEN 1 ELSE 0 END) as filled,
                COUNT(*) as total
            FROM orders
            WHERE submitted_at > datetime('now', '-' || ? || ' days')
            GROUP BY platform
            """,
            (self.days,),
        )
        platform_rows = await cursor.fetchall()

        polymarket_rate = 0
        kalshi_rate = 0
        for prow in platform_rows:
            platform = prow["platform"] or ""
            total = prow["total"] or 0
            filled = prow["filled"] or 0
            rate = (filled / total * 100) if total > 0 else 0
            if "polymarket" in platform.lower():
                polymarket_rate = rate
            elif "kalshi" in platform.lower():
                kalshi_rate = rate

        return ExecutionMetrics(
            total_orders=total_orders,
            filled_orders=filled_orders,
            fill_rate=fill_rate,
            avg_submission_latency_ms=row["avg_submission_latency"] or 0,
            avg_fill_latency_ms=row["avg_fill_latency"] or 0,
            avg_slippage=row["avg_slippage"] or 0,
            total_slippage_cost=row["total_slippage_cost"] or 0,
            partial_fills=row["partial_fills"] or 0,
            rejections=row["rejections"] or 0,
            polymarket_fill_rate=polymarket_rate,
            kalshi_fill_rate=kalshi_rate,
        )

    async def _get_risk_metrics(self, db: aiosqlite.Connection) -> RiskMetrics:
        """Get risk metrics from trade_outcomes and positions."""
        # Max loss and largest win
        cursor = await db.execute(
            """
            SELECT
                MIN(actual_pnl) as max_loss,
                MAX(actual_pnl) as largest_win,
                SUM(CASE WHEN actual_pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN actual_pnl < 0 THEN 1 ELSE 0 END) as losses,
                AVG(holding_period_ms / 3600000.0) as avg_holding_hours
            FROM trade_outcomes
            WHERE resolved_at > datetime('now', '-' || ? || ' days')
            """,
            (self.days,),
        )
        row = await cursor.fetchone()

        max_loss = row["max_loss"] or 0
        largest_win = row["largest_win"] or 0
        wins = row["wins"] or 0
        losses = row["losses"] or 0
        avg_holding_hours = row["avg_holding_hours"] or 0

        win_loss_ratio = (wins / losses) if losses > 0 else (wins if wins > 0 else 0)

        # Max drawdown from daily PnL
        cursor = await db.execute(
            """
            SELECT
                DATE(snapshotted_at) as date,
                (unrealized_pnl + realized_pnl_today) as daily_pnl
            FROM pnl_snapshots
            WHERE snapshotted_at > datetime('now', '-' || ? || ' days')
            ORDER BY snapshotted_at ASC
            """,
            (self.days,),
        )
        pnl_rows = await cursor.fetchall()

        max_drawdown = 0.0
        if pnl_rows:
            running_max = float(pnl_rows[0]["daily_pnl"] or 0)
            for prow in pnl_rows:
                current = float(prow["daily_pnl"] or 0)
                drawdown = (running_max - current) / max(abs(running_max), 1)
                max_drawdown = max(max_drawdown, drawdown * 100)
                running_max = max(running_max, current)

        # Max concentration
        cursor = await db.execute("""
            SELECT
                SUM(p.entry_size * p.entry_price) /
                COALESCE((SELECT total_capital FROM pnl_snapshots ORDER BY snapshotted_at DESC LIMIT 1), 1) as max_conc
            FROM positions p
            WHERE p.status = 'open'
            GROUP BY p.market_id
            ORDER BY max_conc DESC
            LIMIT 1
            """)
        conc_row = await cursor.fetchone()
        max_concentration = (conc_row["max_conc"] or 0) * 100 if conc_row else 0

        return RiskMetrics(
            max_loss=max_loss,
            max_drawdown=max_drawdown,
            largest_win=largest_win,
            win_loss_ratio=win_loss_ratio,
            avg_holding_period_hours=avg_holding_hours,
            max_concentration_pct=max_concentration,
        )

    async def _get_recent_trades(
        self, db: aiosqlite.Connection, limit: int = 10
    ) -> list[Dict]:
        """Get last N closed trades with market info."""
        cursor = await db.execute(
            """
            SELECT
                p.id,
                p.strategy,
                p.side,
                p.entry_price,
                p.exit_price,
                p.realized_pnl,
                p.fees_paid,
                p.closed_at,
                m.title as market_title
            FROM positions p
            LEFT JOIN markets m ON p.market_id = m.id
            WHERE p.status = 'closed' AND p.closed_at IS NOT NULL
            ORDER BY p.closed_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()

        trades = []
        for row in rows:
            trades.append(
                {
                    "id": row["id"],
                    "strategy": row["strategy"] or "—",
                    "side": row["side"] or "—",
                    "entry_price": row["entry_price"] or 0,
                    "exit_price": row["exit_price"] or 0,
                    "realized_pnl": row["realized_pnl"] or 0,
                    "fees_paid": row["fees_paid"] or 0,
                    "closed_at": row["closed_at"] or "—",
                    "market_title": row["market_title"] or "Unknown",
                }
            )

        return trades

    async def _get_violations_summary(self, db: aiosqlite.Connection) -> Dict:
        """Get violations and signals pipeline metrics."""
        # Total violations
        cursor = await db.execute(
            """
            SELECT COUNT(*) as total
            FROM violations
            WHERE detected_at > datetime('now', '-' || ? || ' days')
            """,
            (self.days,),
        )
        row = await cursor.fetchone()
        total_violations = row["total"] or 0

        # Signals created
        cursor = await db.execute(
            """
            SELECT COUNT(*) as total
            FROM signals
            WHERE fired_at > datetime('now', '-' || ? || ' days')
            """,
            (self.days,),
        )
        row = await cursor.fetchone()
        signals_created = row["total"] or 0

        # Executed signals (with filled orders)
        cursor = await db.execute(
            """
            SELECT COUNT(DISTINCT s.id) as executed
            FROM signals s
            INNER JOIN orders o ON s.id = o.signal_id
            WHERE s.fired_at > datetime('now', '-' || ? || ' days')
            AND o.status = 'filled'
            """,
            (self.days,),
        )
        row = await cursor.fetchone()
        executed = row["executed"] or 0

        conversion_rate = (
            (signals_created / total_violations * 100) if total_violations > 0 else 0
        )
        execution_rate = (
            (executed / signals_created * 100) if signals_created > 0 else 0
        )

        # Pipeline latencies
        cursor = await db.execute(
            """
            SELECT
                AVG(CAST((SELECT fired_at FROM signals WHERE id = v.id LIMIT 1) AS REAL) -
                    CAST(v.detected_at AS REAL)) as detect_to_signal_ms,
                AVG(o.fill_latency_ms) as signal_to_fill_ms
            FROM violations v
            LEFT JOIN signals s ON v.id = s.violation_id
            LEFT JOIN orders o ON s.id = o.signal_id
            WHERE v.detected_at > datetime('now', '-' || ? || ' days')
            """,
            (self.days,),
        )
        timing_row = await cursor.fetchone()

        detect_to_signal = (timing_row["detect_to_signal_ms"] or 0) if timing_row else 0
        signal_to_fill = (timing_row["signal_to_fill_ms"] or 0) if timing_row else 0
        total_latency = detect_to_signal + signal_to_fill

        # Rejection reasons
        cursor = await db.execute(
            """
            SELECT rejection_reason, COUNT(*) as count
            FROM violations
            WHERE rejection_reason IS NOT NULL
            AND detected_at > datetime('now', '-' || ? || ' days')
            GROUP BY rejection_reason
            ORDER BY count DESC
            """,
            (self.days,),
        )
        rejection_rows = await cursor.fetchall()
        rejection_reasons = {
            row["rejection_reason"]: row["count"] for row in rejection_rows
        }

        return {
            "total": total_violations,
            "signals_created": signals_created,
            "executed": executed,
            "conversion_rate": conversion_rate,
            "execution_rate": execution_rate,
            "avg_detect_to_signal_ms": detect_to_signal,
            "avg_signal_to_fill_ms": signal_to_fill,
            "total_pipeline_latency_ms": total_latency,
            "rejection_reasons": rejection_reasons,
        }


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Prediction Market Trading System — Analytics Dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/dashboard.py --db-path ./data/mock_session.db
  python scripts/dashboard.py --db-path ./data/live.db --days 30
  python scripts/dashboard.py --db-path ./data/live.db --format json
        """,
    )

    parser.add_argument(
        "--db-path",
        default="prediction_market.db",
        help="Path to SQLite database file (default: prediction_market.db)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Number of days to analyze (default: 7)",
    )
    parser.add_argument(
        "--format",
        choices=["terminal", "json"],
        default="terminal",
        help="Output format (default: terminal)",
    )

    args = parser.parse_args()

    # Verify database exists
    db_path = Path(args.db_path)
    if not db_path.exists():
        print(f"Error: Database file not found: {args.db_path}", file=sys.stderr)
        sys.exit(1)

    # Run dashboard
    dashboard = Dashboard(args.db_path, args.days, args.format)
    await dashboard.run()


if __name__ == "__main__":
    asyncio.run(main())
