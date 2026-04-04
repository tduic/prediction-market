"""
FastAPI server for the prediction market dashboard.
Queries the SQLite trading database and serves JSON to the React frontend.

Can be run standalone:
    python scripts/dashboard_api.py --db prediction_market.db

Or embedded in paper_trading_session.py as a background asyncio task:
    from scripts.dashboard_api import create_dashboard_app, start_dashboard_server
"""

import argparse
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite
import uvicorn
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Module-level DB path (set by configure() or create_dashboard_app())
# ---------------------------------------------------------------------------
_DB_PATH: str = "./data/prediction_market.db"


def configure(db_path: str) -> None:
    """Set the database path for all endpoints. Call before starting server."""
    global _DB_PATH
    _DB_PATH = db_path


def _build_app(static_dir: Optional[str] = None) -> FastAPI:
    """
    Build the FastAPI application.

    Parameters
    ----------
    static_dir : str or None
        If provided, mount the React build directory at "/" so the
        frontend is served from the same process.
    """
    app = FastAPI(title="Prediction Market Dashboard API")

    # CORS middleware.
    #
    # Note: allow_origins=["*"] and allow_credentials=True are mutually
    # exclusive per the CORS spec — Starlette silently drops the wildcard
    # when credentials are enabled, which rejects every origin. The dashboard
    # doesn't use cookies or credentialed requests, so we keep the wildcard
    # and disable credentials.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── helpers ──────────────────────────────────────────────────────────

    async def get_db() -> aiosqlite.Connection:
        db = await aiosqlite.connect(_DB_PATH)
        db.row_factory = aiosqlite.Row
        return db

    async def close_db(db: aiosqlite.Connection) -> None:
        await db.close()

    async def get_latest_snapshot() -> Optional[Dict[str, Any]]:
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT * FROM pnl_snapshots ORDER BY snapshotted_at DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            return dict(row) if row else None
        finally:
            await close_db(db)

    # ── endpoints ────────────────────────────────────────────────────────

    @app.get("/api/overview")
    async def get_overview() -> Dict[str, Any]:
        PAPER_CAPITAL = 10_000  # Default paper trading capital

        snapshot = await get_latest_snapshot()
        if snapshot:
            total_capital = snapshot.get("total_capital", 0) or 0
            cash = snapshot.get("cash", 0) or 0
            unrealized_pnl = snapshot.get("unrealized_pnl", 0) or 0
            realized_pnl_total = snapshot.get("realized_pnl_total", 0) or 0
            total_fees = snapshot.get("fees_total", 0) or 0
            deployed = total_capital - cash if total_capital > 0 else 0
            open_positions = snapshot.get("open_positions_count", 0) or 0
            snapshotted_at = snapshot.get("snapshotted_at")
        else:
            # No snapshots yet — compute live from trade_outcomes
            db = await get_db()
            try:
                cursor = await db.execute(
                    "SELECT COALESCE(SUM(actual_pnl), 0), COALESCE(SUM(fees_total), 0) FROM trade_outcomes"
                )
                row = await cursor.fetchone()
                realized_pnl_total = row[0] if row else 0
                total_fees = row[1] if row else 0
                total_capital = PAPER_CAPITAL + realized_pnl_total - total_fees
                cash = total_capital
                deployed = 0
                unrealized_pnl = 0
                open_positions = 0
                snapshotted_at = None
            finally:
                await close_db(db)

        net_return_pct = 0
        if total_capital > 0:
            net_pnl = realized_pnl_total + unrealized_pnl - total_fees
            net_return_pct = (net_pnl / PAPER_CAPITAL) * 100

        return {
            "total_capital": round(total_capital, 2),
            "cash": round(cash, 2),
            "deployed": round(deployed, 2),
            "open_positions": open_positions,
            "unrealized_pnl": round(unrealized_pnl, 2),
            "realized_pnl_total": round(realized_pnl_total, 2),
            "total_fees": round(total_fees, 2),
            "net_return_pct": round(net_return_pct, 2),
            "snapshotted_at": snapshotted_at,
        }

    @app.get("/api/strategies")
    async def get_strategies(days: int = Query(30, ge=1)) -> List[Dict[str, Any]]:
        db = await get_db()
        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
            cursor = await db.execute(
                """
                SELECT
                    strategy,
                    COUNT(*) as trade_count,
                    SUM(CASE WHEN actual_pnl > 0 THEN 1 ELSE 0 END) as win_count,
                    COALESCE(AVG(actual_pnl), 0) as avg_pnl,
                    COALESCE(SUM(actual_pnl), 0) as total_pnl,
                    COALESCE(SUM(fees_total), 0) as total_fees,
                    COALESCE(AVG(edge_captured_pct), 0) as avg_edge_capture,
                    COALESCE(AVG(signal_to_fill_ms), 0) as avg_execution_time_ms
                FROM trade_outcomes
                WHERE created_at >= ?
                GROUP BY strategy
                """,
                (cutoff_date.isoformat(),),
            )
            rows = await cursor.fetchall()

            strategies = []
            for row in rows:
                row_dict = dict(row)
                trade_count = row_dict.get("trade_count", 0) or 0
                win_count = row_dict.get("win_count", 0) or 0
                win_rate = (win_count / trade_count) if trade_count > 0 else 0

                sharpe_ratio = 0
                if trade_count > 0:
                    cursor2 = await db.execute(
                        "SELECT actual_pnl FROM trade_outcomes WHERE strategy = ? AND created_at >= ?",
                        (row_dict["strategy"], cutoff_date.isoformat()),
                    )
                    pnl_rows = await cursor2.fetchall()
                    pnl_values = [
                        r["actual_pnl"] for r in pnl_rows if r["actual_pnl"] is not None
                    ]
                    if len(pnl_values) > 1:
                        mean_pnl = statistics.mean(pnl_values)
                        stdev_pnl = statistics.stdev(pnl_values)
                        if stdev_pnl > 0:
                            sharpe_ratio = mean_pnl / stdev_pnl

                strategies.append(
                    {
                        "strategy": row_dict.get("strategy"),
                        "trade_count": trade_count,
                        "win_count": win_count,
                        "win_rate": round(win_rate, 2),
                        "avg_pnl": round(row_dict.get("avg_pnl", 0) or 0, 2),
                        "total_pnl": round(row_dict.get("total_pnl", 0) or 0, 2),
                        "total_fees": round(row_dict.get("total_fees", 0) or 0, 2),
                        "sharpe_ratio": round(sharpe_ratio, 2),
                        "avg_edge_capture": round(
                            row_dict.get("avg_edge_capture", 0) or 0, 2
                        ),
                        "avg_execution_time_ms": round(
                            row_dict.get("avg_execution_time_ms", 0) or 0, 0
                        ),
                    }
                )

            return strategies
        finally:
            await close_db(db)

    @app.get("/api/strategies/pnl-series")
    async def get_strategies_pnl_series(
        days: int = Query(7, ge=1),
    ) -> List[Dict[str, Any]]:
        db = await get_db()
        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
            cursor = await db.execute(
                """
                SELECT
                    p.snapshotted_at, s.strategy,
                    s.realized_pnl, s.unrealized_pnl,
                    s.fees, s.trade_count, s.win_count
                FROM strategy_pnl_snapshots s
                JOIN pnl_snapshots p ON s.snapshot_id = p.id
                WHERE p.snapshotted_at >= ?
                ORDER BY p.snapshotted_at, s.strategy
                """,
                (cutoff_date.isoformat(),),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "snapshotted_at": dict(r).get("snapshotted_at"),
                    "strategy": dict(r).get("strategy"),
                    "realized_pnl": round(dict(r).get("realized_pnl", 0) or 0, 2),
                    "unrealized_pnl": round(dict(r).get("unrealized_pnl", 0) or 0, 2),
                    "fees": round(dict(r).get("fees", 0) or 0, 2),
                    "trade_count": dict(r).get("trade_count", 0) or 0,
                    "win_count": dict(r).get("win_count", 0) or 0,
                }
                for r in rows
            ]
        finally:
            await close_db(db)

    @app.get("/api/equity-curve")
    async def get_equity_curve(days: int = Query(30, ge=1)) -> List[Dict[str, Any]]:
        db = await get_db()
        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
            cursor = await db.execute(
                """
                SELECT snapshotted_at, total_capital, unrealized_pnl,
                       realized_pnl_total, fees_total
                FROM pnl_snapshots WHERE snapshotted_at >= ?
                ORDER BY snapshotted_at
                """,
                (cutoff_date.isoformat(),),
            )
            rows = await cursor.fetchall()
            return [
                {
                    "snapshotted_at": dict(r).get("snapshotted_at"),
                    "total_capital": round(dict(r).get("total_capital", 0) or 0, 2),
                    "unrealized_pnl": round(dict(r).get("unrealized_pnl", 0) or 0, 2),
                    "realized_pnl_total": round(
                        dict(r).get("realized_pnl_total", 0) or 0, 2
                    ),
                    "fees_total": round(dict(r).get("fees_total", 0) or 0, 2),
                }
                for r in rows
            ]
        finally:
            await close_db(db)

    @app.get("/api/trades")
    async def get_trades(
        strategy: Optional[str] = Query(None),
        days: int = Query(30, ge=1),
        limit: int = Query(200, ge=1, le=1000),
    ) -> List[Dict[str, Any]]:
        db = await get_db()
        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
            if strategy:
                cursor = await db.execute(
                    "SELECT * FROM trade_outcomes WHERE created_at >= ? AND strategy = ? ORDER BY created_at DESC LIMIT ?",
                    (cutoff_date.isoformat(), strategy, limit),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM trade_outcomes WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
                    (cutoff_date.isoformat(), limit),
                )
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                result.append(
                    {
                        "id": d.get("id"),
                        "signal_id": d.get("signal_id"),
                        "strategy": d.get("strategy"),
                        "violation_id": d.get("violation_id"),
                        "market_id_a": d.get("market_id_a"),
                        "market_id_b": d.get("market_id_b"),
                        "predicted_edge": round(d.get("predicted_edge", 0) or 0, 4),
                        "predicted_pnl": round(d.get("predicted_pnl", 0) or 0, 2),
                        "actual_pnl": round(d.get("actual_pnl", 0) or 0, 2),
                        "fees_total": round(d.get("fees_total", 0) or 0, 2),
                        "edge_captured_pct": round(
                            d.get("edge_captured_pct", 0) or 0, 2
                        ),
                        "signal_to_fill_ms": d.get("signal_to_fill_ms"),
                        "holding_period_ms": d.get("holding_period_ms"),
                        "spread_at_signal": round(d.get("spread_at_signal", 0) or 0, 4),
                        "volume_at_signal": round(d.get("volume_at_signal", 0) or 0, 2),
                        "liquidity_at_signal": round(
                            d.get("liquidity_at_signal", 0) or 0, 2
                        ),
                        "resolved_at": d.get("resolved_at"),
                        "created_at": d.get("created_at"),
                    }
                )
            return result
        finally:
            await close_db(db)

    @app.get("/api/risk")
    async def get_risk() -> Dict[str, Any]:
        PAPER_CAPITAL = 10_000
        db = await get_db()
        try:
            snapshot = await get_latest_snapshot()
            if snapshot:
                total_capital = snapshot.get("total_capital", 0) or PAPER_CAPITAL
            else:
                # Compute from trade_outcomes if no snapshots yet
                cursor = await db.execute(
                    "SELECT COALESCE(SUM(actual_pnl), 0), COALESCE(SUM(fees_total), 0) FROM trade_outcomes"
                )
                row = await cursor.fetchone()
                total_capital = (
                    PAPER_CAPITAL + (row[0] or 0) - (row[1] or 0)
                    if row
                    else PAPER_CAPITAL
                )

            cursor = await db.execute("""
                SELECT total_capital, realized_pnl_total + unrealized_pnl as net_pnl
                FROM pnl_snapshots ORDER BY snapshotted_at DESC LIMIT 100
                """)
            snapshots = await cursor.fetchall()

            max_drawdown = 0
            max_drawdown_dollar = 0
            if snapshots:
                peak_capital = None
                for snap in reversed(snapshots):
                    current_capital = snap["total_capital"] or 0
                    if peak_capital is None or current_capital > peak_capital:
                        peak_capital = current_capital
                    if peak_capital and peak_capital > 0:
                        dd_dollar = peak_capital - current_capital
                        drawdown = dd_dollar / peak_capital * 100
                        if drawdown > max_drawdown:
                            max_drawdown = drawdown
                            max_drawdown_dollar = dd_dollar

            cursor = await db.execute(
                "SELECT MAX(ABS(unrealized_pnl)) as max_position_pnl FROM positions WHERE status = 'open'"
            )
            row = await cursor.fetchone()
            max_position_pnl = row["max_position_pnl"] or 0 if row else 0
            concentration = (
                (abs(max_position_pnl) / total_capital * 100)
                if total_capital > 0
                else 0
            )

            cursor = await db.execute("""
                SELECT DATE(created_at) as trade_date, SUM(actual_pnl) as daily_pnl
                FROM trade_outcomes GROUP BY DATE(created_at)
                ORDER BY trade_date DESC LIMIT 30
                """)
            daily_rows = await cursor.fetchall()
            daily_pnls = [
                r["daily_pnl"] for r in daily_rows if r["daily_pnl"] is not None
            ]

            daily_var = 0
            if len(daily_pnls) > 1:
                daily_pnls_sorted = sorted(daily_pnls)
                percentile_5_idx = max(0, int(len(daily_pnls_sorted) * 0.05) - 1)
                daily_var = abs(daily_pnls_sorted[percentile_5_idx])

            cursor = await db.execute(
                "SELECT actual_pnl FROM trade_outcomes WHERE actual_pnl IS NOT NULL"
            )
            pnl_rows = await cursor.fetchall()
            pnl_values = [r["actual_pnl"] for r in pnl_rows]

            overall_sharpe = 0
            if len(pnl_values) > 1:
                mean_pnl = statistics.mean(pnl_values)
                stdev_pnl = statistics.stdev(pnl_values)
                if stdev_pnl > 0:
                    overall_sharpe = mean_pnl / stdev_pnl

            return {
                "max_drawdown": round(max_drawdown_dollar, 2),
                "max_drawdown_pct": round(max_drawdown, 2),
                "concentration_pct": round(concentration, 2),
                "daily_var": round(daily_var, 2),
                "sharpe_overall": round(overall_sharpe, 2),
            }
        finally:
            await close_db(db)

    @app.get("/api/fees")
    async def get_fees() -> Dict[str, Any]:
        db = await get_db()
        try:
            cursor = await db.execute("""
                SELECT platform, COALESCE(SUM(fee_paid), 0) as total_fees
                FROM orders WHERE fee_paid IS NOT NULL GROUP BY platform
                """)
            platform_rows = await cursor.fetchall()
            fees_by_platform = [
                {
                    "platform": row["platform"],
                    "total_fees": round(row["total_fees"] or 0, 2),
                }
                for row in platform_rows
            ]

            cursor = await db.execute("""
                SELECT strategy, COALESCE(SUM(fees_total), 0) as total_fees
                FROM trade_outcomes WHERE fees_total IS NOT NULL GROUP BY strategy
                """)
            strategy_rows = await cursor.fetchall()
            fees_by_strategy = [
                {
                    "strategy": row["strategy"],
                    "total_fees": round(row["total_fees"] or 0, 2),
                }
                for row in strategy_rows
            ]

            total_fees = sum(r["total_fees"] for r in fees_by_platform)
            return {
                "total_fees": round(total_fees, 2),
                "by_platform": fees_by_platform,
                "by_strategy": fees_by_strategy,
            }
        finally:
            await close_db(db)

    @app.get("/health")
    async def health_check() -> Dict[str, str]:
        return {"status": "ok"}

    # ── Serve React frontend if static_dir provided ─────────────────────
    if static_dir and Path(static_dir).is_dir():
        app.mount(
            "/",
            StaticFiles(directory=static_dir, html=True),
            name="frontend",
        )

    return app


def create_dashboard_app(
    db_path: str,
    static_dir: Optional[str] = None,
) -> FastAPI:
    """
    Public factory: create a fully configured dashboard app.

    Parameters
    ----------
    db_path : str
        Path to the SQLite database.
    static_dir : str or None
        Path to the React build directory (dashboard/dist/).
        If provided, the SPA is served at "/".
    """
    configure(db_path)
    return _build_app(static_dir=static_dir)


async def start_dashboard_server(
    app: FastAPI,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """
    Start uvicorn as an async coroutine (non-blocking).
    Designed to be launched as an asyncio.create_task() inside
    paper_trading_session.py.
    """
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


# ---------------------------------------------------------------------------
# Standalone mode
# ---------------------------------------------------------------------------
def main():
    """Run the FastAPI server standalone."""
    parser = argparse.ArgumentParser(description="Prediction Market Dashboard API")
    parser.add_argument(
        "--db",
        type=str,
        default="./data/prediction_market.db",
        help="Path to SQLite database",
    )
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind to")
    args = parser.parse_args()

    db_file = Path(args.db)
    if not db_file.parent.exists():
        db_file.parent.mkdir(parents=True, exist_ok=True)

    # Auto-detect dashboard/dist/ relative to this script
    script_dir = Path(__file__).resolve().parent
    dist_dir = script_dir.parent / "dashboard" / "dist"
    static_dir = str(dist_dir) if dist_dir.is_dir() else None

    app = create_dashboard_app(db_path=args.db, static_dir=static_dir)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
