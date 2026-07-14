"""
FastAPI server for the prediction market dashboard.
Queries the SQLite trading database and serves JSON to the React frontend.

Can be run standalone:
    python scripts/dashboard_api.py --db prediction_market.db

Or embedded in trading_session.py as a background asyncio task:
    from scripts.dashboard_api import create_dashboard_app, start_dashboard_server
"""

import argparse
import base64
import logging
import math
import os
import secrets
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiosqlite
import uvicorn
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from core.config import get_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level DB path (set by configure() or create_dashboard_app())
# ---------------------------------------------------------------------------
_DB_PATH: str = "./data/prediction_market.db"


def configure(db_path: str) -> None:
    """Set the database path for all endpoints. Call before starting server."""
    global _DB_PATH
    _DB_PATH = db_path


class _BasicAuthMiddleware(BaseHTTPMiddleware):
    """HTTP Basic Auth gate. Applied only when DASHBOARD_PASSWORD is set."""

    def __init__(self, app, username: str, password: str) -> None:
        super().__init__(app)
        self._username = username
        self._password = password

    async def dispatch(self, request: Request, call_next):
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8", errors="replace")
                user, _, pwd = decoded.partition(":")
                user_ok = secrets.compare_digest(user.encode(), self._username.encode())
                pass_ok = secrets.compare_digest(pwd.encode(), self._password.encode())
                if user_ok and pass_ok:
                    return await call_next(request)
            except Exception:
                pass
        return Response(
            "Unauthorized",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Predictor Dashboard"'},
        )


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

    # HTTP Basic Auth — enabled when DASHBOARD_PASSWORD env var is set.
    # Add before CORS so unauthenticated requests are rejected at the gate.
    _dash_password = os.getenv("DASHBOARD_PASSWORD", "")
    if _dash_password:
        _dash_user = os.getenv("DASHBOARD_USER", "admin")
        app.add_middleware(
            _BasicAuthMiddleware, username=_dash_user, password=_dash_password
        )

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

    # ── helpers ────────────────────────────────────────────────────────────────────────────────────────────────

    async def get_db() -> aiosqlite.Connection:
        db = await aiosqlite.connect(_DB_PATH)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        return db

    async def close_db(db: aiosqlite.Connection) -> None:
        await db.close()

    async def get_latest_snapshot(
        db: Optional[aiosqlite.Connection] = None,
    ) -> Optional[Dict[str, Any]]:
        own_db = db is None
        if db is None:
            db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT * FROM pnl_snapshots ORDER BY snapshotted_at DESC LIMIT 1"
            )
            row = await cursor.fetchone()
            return dict(row) if row else None
        finally:
            if own_db:
                await close_db(db)

    # ── endpoints ───────────────────────────────────────────────────────────────────────────────────────────

    @app.get("/api/overview")
    async def get_overview() -> Dict[str, Any]:
        PAPER_CAPITAL = get_config().risk_controls.starting_capital
        db = await get_db()
        try:
            snapshot = await get_latest_snapshot(db)
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

            net_return_pct = 0.0
            if total_capital > 0:
                net_pnl = realized_pnl_total + unrealized_pnl - total_fees
                net_return_pct = (net_pnl / PAPER_CAPITAL) * 100

            signals_24h = 0
            violations_24h = 0
            _cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            try:
                _sig_cursor = await db.execute(
                    "SELECT COUNT(*) FROM signals WHERE fired_at >= ?",
                    (_cutoff_24h,),
                )
                _sig_row = await _sig_cursor.fetchone()
                signals_24h = _sig_row[0] if _sig_row else 0
            except Exception as _sig_err:
                logger.debug("overview: signals_24h query failed: %s", _sig_err)

            try:
                _v_cursor = await db.execute(
                    "SELECT COUNT(*) FROM violations WHERE detected_at >= ?",
                    (_cutoff_24h,),
                )
                _v_row = await _v_cursor.fetchone()
                violations_24h = _v_row[0] if _v_row else 0
            except Exception as _v_err:
                logger.debug("overview: violations_24h query failed: %s", _v_err)

            daily_loss_pct_used = 0.0
            try:
                from datetime import date as _date

                _today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                _tomorrow = (
                    _date.fromisoformat(_today) + timedelta(days=1)
                ).isoformat()
                _dl_cursor = await db.execute(
                    "SELECT COALESCE(SUM(actual_pnl - COALESCE(fees_total, 0)), 0) "
                    "FROM trade_outcomes WHERE created_at >= ? AND created_at < ?",
                    (_today, _tomorrow),
                )
                _dl_row = await _dl_cursor.fetchone()
                _net_pnl_today = (
                    float(_dl_row[0]) if _dl_row and _dl_row[0] is not None else 0.0
                )
                _daily_loss = max(0.0, -_net_pnl_today)
                _cfg = get_config().risk_controls
                _daily_loss_limit = _cfg.starting_capital * _cfg.max_daily_loss_pct
                daily_loss_pct_used = round(
                    _daily_loss / _daily_loss_limit if _daily_loss_limit > 0 else 0.0, 4
                )
            except Exception as _dl_err:
                logger.debug("overview: daily_loss_pct_used query failed: %s", _dl_err)

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
                "signals_24h": signals_24h,
                "violations_24h": violations_24h,
                "daily_loss_pct_used": daily_loss_pct_used,
            }
        finally:
            await close_db(db)

    @app.get("/api/strategies")
    async def get_strategies(
        days: int = Query(30, ge=1, le=365),
    ) -> List[Dict[str, Any]]:
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
                    COALESCE(AVG(signal_to_fill_ms), 0) as avg_execution_time_ms,
                    COALESCE(SUM(actual_pnl * actual_pnl), 0) as sum_pnl_sq,
                    COUNT(actual_pnl) as pnl_count,
                    COALESCE(AVG(spread_at_signal), 0) as avg_spread_at_signal
                FROM trade_outcomes
                WHERE created_at >= ?
                GROUP BY strategy
                """,
                (cutoff_date.isoformat(),),
            )
            rows = await cursor.fetchall()

            cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            signals_cursor = await db.execute(
                "SELECT strategy, COUNT(*) as cnt FROM signals "
                "WHERE fired_at >= ? GROUP BY strategy",
                (cutoff_24h,),
            )
            signals_rows = await signals_cursor.fetchall()
            signals_24h_map = {r["strategy"]: r["cnt"] for r in signals_rows}

            strategies = []
            for row in rows:
                row_dict = dict(row)
                trade_count = row_dict.get("trade_count", 0) or 0
                win_count = row_dict.get("win_count", 0) or 0
                win_rate = (win_count / trade_count) if trade_count > 0 else 0

                sharpe_ratio = 0.0
                pnl_count = row_dict.get("pnl_count", 0) or 0
                if pnl_count > 1:
                    mean_pnl = row_dict.get("avg_pnl", 0) or 0
                    sum_sq = row_dict.get("sum_pnl_sq", 0) or 0
                    # Sample variance (Bessel's correction, N-1) — matches
                    # statistics.stdev() used in /api/risk for consistency.
                    # max(0.0) guards against tiny negatives from floating-point
                    # cancellation when all PnLs cluster near the same value.
                    variance = max(
                        0.0, (sum_sq - pnl_count * mean_pnl**2) / (pnl_count - 1)
                    )
                    if variance > 0:
                        sharpe_ratio = mean_pnl / math.sqrt(variance)

                strategies.append(
                    {
                        "strategy": row_dict.get("strategy"),
                        "trade_count": trade_count,
                        "win_count": win_count,
                        "win_rate": round(win_rate, 2),
                        "avg_pnl": round(row_dict.get("avg_pnl", 0) or 0, 2),
                        "total_pnl": round(row_dict.get("total_pnl", 0) or 0, 2),
                        "total_fees": round(row_dict.get("total_fees", 0) or 0, 2),
                        "net_pnl": round(
                            (row_dict.get("total_pnl", 0) or 0)
                            - (row_dict.get("total_fees", 0) or 0),
                            2,
                        ),
                        "sharpe_ratio": round(sharpe_ratio, 2),
                        "sharpe_note": "per-trade mean/stdev; not annualized",
                        "avg_edge_capture": round(
                            row_dict.get("avg_edge_capture", 0) or 0, 2
                        ),
                        "avg_execution_time_ms": round(
                            row_dict.get("avg_execution_time_ms", 0) or 0, 0
                        ),
                        "signals_24h": signals_24h_map.get(row_dict.get("strategy"), 0),
                        "avg_spread_at_signal": round(
                            row_dict.get("avg_spread_at_signal", 0) or 0, 4
                        ),
                    }
                )

            # Include strategies that fired signals but have no trade_outcomes yet.
            # Without this, risk-rejected strategies are invisible on the dashboard.
            seen_strategies = {s["strategy"] for s in strategies}
            signal_only_cursor = await db.execute(
                "SELECT DISTINCT strategy FROM signals WHERE fired_at >= ?",
                (cutoff_date.isoformat(),),
            )
            signal_only_rows = await signal_only_cursor.fetchall()
            for row in signal_only_rows:
                strat = row["strategy"]
                if strat not in seen_strategies:
                    strategies.append(
                        {
                            "strategy": strat,
                            "trade_count": 0,
                            "win_count": 0,
                            "win_rate": 0.0,
                            "avg_pnl": 0.0,
                            "total_pnl": 0.0,
                            "total_fees": 0.0,
                            "net_pnl": 0.0,
                            "sharpe_ratio": 0.0,
                            "sharpe_note": "per-trade mean/stdev; not annualized",
                            "avg_edge_capture": 0.0,
                            "avg_execution_time_ms": 0.0,
                            "signals_24h": signals_24h_map.get(strat, 0),
                            "avg_spread_at_signal": 0.0,
                        }
                    )

            return strategies
        finally:
            await close_db(db)

    @app.get("/api/strategies/pnl-series")
    async def get_strategies_pnl_series(
        days: int = Query(7, ge=1, le=365),
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
                    "snapshotted_at": d.get("snapshotted_at"),
                    "strategy": d.get("strategy"),
                    "realized_pnl": round(d.get("realized_pnl", 0) or 0, 2),
                    "unrealized_pnl": round(d.get("unrealized_pnl", 0) or 0, 2),
                    "fees": round(d.get("fees", 0) or 0, 2),
                    "trade_count": d.get("trade_count", 0) or 0,
                    "win_count": d.get("win_count", 0) or 0,
                }
                for r in rows
                for d in [dict(r)]
            ]
        finally:
            await close_db(db)

    @app.get("/api/equity-curve")
    async def get_equity_curve(
        days: int = Query(30, ge=1, le=365),
    ) -> List[Dict[str, Any]]:
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
                    "snapshotted_at": d.get("snapshotted_at"),
                    "total_capital": round(d.get("total_capital", 0) or 0, 2),
                    "unrealized_pnl": round(d.get("unrealized_pnl", 0) or 0, 2),
                    "realized_pnl_total": round(d.get("realized_pnl_total", 0) or 0, 2),
                    "fees_total": round(d.get("fees_total", 0) or 0, 2),
                }
                for r in rows
                for d in [dict(r)]
            ]
        finally:
            await close_db(db)

    @app.get("/api/trades")
    async def get_trades(
        strategy: Optional[str] = Query(None),
        days: int = Query(30, ge=1, le=365),
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
        PAPER_CAPITAL = get_config().risk_controls.starting_capital
        db = await get_db()
        try:
            snapshot = await get_latest_snapshot(db)
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

            _cutoff_90d = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
            cursor = await db.execute(
                """
                SELECT total_capital
                FROM pnl_snapshots
                WHERE snapshotted_at >= ?
                ORDER BY snapshotted_at ASC
                """,
                (_cutoff_90d,),
            )
            snapshots = list(await cursor.fetchall())

            max_drawdown = 0.0
            max_drawdown_dollar = 0.0
            if snapshots:
                peak_capital = None
                for snap in snapshots:
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
                "SELECT MAX(entry_price * entry_size) as max_pos FROM positions WHERE status = 'open'"
            )
            row = await cursor.fetchone()
            max_pos_exposure = row["max_pos"] or 0 if row else 0
            concentration = (
                (max_pos_exposure / total_capital * 100) if total_capital > 0 else 0
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

            # Minimum 20 daily observations required for a meaningful 5th-percentile
            # estimate. Below this threshold int(n * 0.05) == 0 for all n, which
            # returns the sample minimum rather than any percentile.
            _MIN_VAR_SAMPLE = 20
            daily_var = 0.0
            if len(daily_pnls) >= _MIN_VAR_SAMPLE:
                daily_pnls_sorted = sorted(daily_pnls)
                percentile_5_idx = max(0, int(len(daily_pnls_sorted) * 0.05))
                # VaR = magnitude of loss at 5th percentile.  A positive P&L at
                # the 5th percentile means no loss-tail risk, so VaR = 0.
                daily_var = max(0.0, -daily_pnls_sorted[percentile_5_idx])

            cursor = await db.execute(
                "SELECT actual_pnl FROM trade_outcomes WHERE actual_pnl IS NOT NULL"
                " ORDER BY created_at DESC LIMIT 500"
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
                "daily_var_sample_size": len(daily_pnls),
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

            fees_total_cursor = await db.execute(
                "SELECT COALESCE(SUM(fees_total), 0) as total FROM trade_outcomes"
            )
            fees_total_row = await fees_total_cursor.fetchone()
            total_fees = round(
                float(fees_total_row["total"] or 0) if fees_total_row else 0.0, 2
            )
            return {
                "total_fees": round(total_fees, 2),
                "by_platform": fees_by_platform,
                "by_strategy": fees_by_strategy,
            }
        finally:
            await close_db(db)

    @app.get("/api/signals")
    async def get_signals(
        strategy: Optional[str] = Query(None),
        days: int = Query(7, ge=1, le=365),
        limit: int = Query(200, ge=1, le=1000),
    ) -> List[Dict[str, Any]]:
        db = await get_db()
        try:
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
            if strategy:
                cursor = await db.execute(
                    "SELECT id, violation_id, strategy, signal_type, market_id_a, market_id_b, "
                    "model_edge, kelly_fraction, position_size_a, position_size_b, "
                    "total_capital_at_risk, status, fired_at, updated_at "
                    "FROM signals WHERE fired_at >= ? AND strategy = ? ORDER BY fired_at DESC LIMIT ?",
                    (cutoff_date.isoformat(), strategy, limit),
                )
            else:
                cursor = await db.execute(
                    "SELECT id, violation_id, strategy, signal_type, market_id_a, market_id_b, "
                    "model_edge, kelly_fraction, position_size_a, position_size_b, "
                    "total_capital_at_risk, status, fired_at, updated_at "
                    "FROM signals WHERE fired_at >= ? ORDER BY fired_at DESC LIMIT ?",
                    (cutoff_date.isoformat(), limit),
                )
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                result.append(
                    {
                        "id": d.get("id"),
                        "violation_id": d.get("violation_id"),
                        "strategy": d.get("strategy"),
                        "signal_type": d.get("signal_type"),
                        "market_id_a": d.get("market_id_a"),
                        "market_id_b": d.get("market_id_b"),
                        "model_edge": round(d.get("model_edge", 0) or 0, 4),
                        "kelly_fraction": round(d.get("kelly_fraction", 0) or 0, 4),
                        "position_size_a": round(d.get("position_size_a", 0) or 0, 2),
                        "position_size_b": round(d.get("position_size_b", 0) or 0, 2),
                        "total_capital_at_risk": round(
                            d.get("total_capital_at_risk", 0) or 0, 2
                        ),
                        "status": d.get("status"),
                        "fired_at": d.get("fired_at"),
                        "updated_at": d.get("updated_at"),
                    }
                )
            return result
        finally:
            await close_db(db)

    @app.get("/api/circuit-breaker")
    async def get_circuit_breaker() -> Dict[str, Any]:
        db = await get_db()
        try:
            cursor = await db.execute(
                "SELECT event_type, detail, occurred_at FROM system_events WHERE event_type IN ('CIRCUIT_BREAKER_TRIPPED','CIRCUIT_BREAKER_RESET') AND DATE(occurred_at) = DATE('now') ORDER BY id DESC LIMIT 1"
            )
            event_row = await cursor.fetchone()
            if event_row and event_row["event_type"] == "CIRCUIT_BREAKER_TRIPPED":
                tripped = True
                reason = event_row["detail"]
                tripped_at = event_row["occurred_at"]
            else:
                tripped = False
                reason = None
                tripped_at = None
            daily_loss_available = True
            daily_loss = 0.0
            try:
                from datetime import date as _date

                _today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                _tomorrow = (
                    _date.fromisoformat(_today) + timedelta(days=1)
                ).isoformat()
                cursor = await db.execute(
                    "SELECT COALESCE(SUM(actual_pnl - COALESCE(fees_total,0)),0) FROM trade_outcomes WHERE created_at >= ? AND created_at < ?",
                    (_today, _tomorrow),
                )
                pnl_row = await cursor.fetchone()
                net_pnl = pnl_row[0] if pnl_row else 0.0
                daily_loss = max(0.0, -net_pnl)
            except Exception as e:
                logger.warning(
                    "circuit-breaker endpoint: daily_loss query failed: %s", e
                )
                daily_loss_available = False
            cfg = get_config().risk_controls
            daily_loss_limit_pct = cfg.max_daily_loss_pct
            daily_loss_limit = cfg.starting_capital * daily_loss_limit_pct
            return {
                "tripped": tripped,
                "reason": reason,
                "tripped_at": tripped_at,
                "daily_loss": daily_loss,
                "daily_loss_available": daily_loss_available,
                "daily_loss_limit": daily_loss_limit,
                "daily_loss_limit_pct": daily_loss_limit_pct,
            }
        finally:
            await close_db(db)

    @app.get("/api/positions")
    async def get_positions(
        status: Optional[str] = Query(None),
        limit: int = Query(200, ge=1, le=1000),
    ) -> List[Dict[str, Any]]:
        db = await get_db()
        try:
            if status:
                cursor = await db.execute(
                    "SELECT * FROM positions WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                    (status, limit),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM positions ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
        finally:
            await close_db(db)

    @app.get("/api/pnl-split")
    async def get_pnl_split() -> Dict[str, Any]:
        """Realistic vs synthetic PnL totals across all closed positions.

        realistic: positions closed by mark_and_close_positions at real price.
        synthetic: legacy positions with pre-computed exit_price at open time.
        """
        db = await get_db()
        try:
            cursor = await db.execute("""SELECT
                       pnl_model,
                       COUNT(*) AS trade_count,
                       COALESCE(SUM(realized_pnl), 0.0) AS total_pnl,
                       COALESCE(SUM(fees_paid), 0.0) AS total_fees
                   FROM positions
                   WHERE status = 'closed'
                     AND realized_pnl IS NOT NULL
                   GROUP BY pnl_model""")
            rows = await cursor.fetchall()
            result: Dict[str, Any] = {
                "realistic": {"trade_count": 0, "total_pnl": 0.0, "total_fees": 0.0},
                "synthetic": {"trade_count": 0, "total_pnl": 0.0, "total_fees": 0.0},
            }
            for row in rows:
                model = row[0] or "synthetic"
                if model not in result:
                    result[model] = {
                        "trade_count": 0,
                        "total_pnl": 0.0,
                        "total_fees": 0.0,
                    }
                result[model] = {
                    "trade_count": row[1],
                    "total_pnl": round(row[2], 4),
                    "total_fees": round(row[3], 4),
                }
            return result
        finally:
            await close_db(db)

    @app.get("/api/invariants")
    async def get_invariants(limit: int = Query(20, ge=1, le=100)) -> Dict[str, Any]:
        """Invariant violation log: total count and most recent failures."""
        db = await get_db()
        try:
            count_cursor = await db.execute("SELECT COUNT(*) FROM invariant_violations")
            count_row = await count_cursor.fetchone()
            violation_count = count_row[0] if count_row else 0

            recent_cursor = await db.execute(
                """SELECT id, name, message, severity, violated_at
                   FROM invariant_violations
                   ORDER BY violated_at DESC
                   LIMIT ?""",
                (limit,),
            )
            rows = await recent_cursor.fetchall()
            recent = [dict(row) for row in rows]

            return {
                "violation_count": violation_count,
                "recent_violations": recent,
            }
        finally:
            await close_db(db)

    @app.get("/api/reconciliation")
    async def get_reconciliation(
        limit: int = Query(20, ge=1, le=100),
    ) -> Dict[str, Any]:
        """Reconciliation discrepancy log: counts by type and most recent rows."""
        db = await get_db()
        try:
            total_cursor = await db.execute("SELECT COUNT(*) FROM reconciliation_log")
            total_row = await total_cursor.fetchone()
            total_count = total_row[0] if total_row else 0

            discrepancy_cursor = await db.execute(
                "SELECT COUNT(*) FROM reconciliation_log WHERE status = 'discrepancy'"
            )
            discrepancy_row = await discrepancy_cursor.fetchone()
            discrepancy_count = discrepancy_row[0] if discrepancy_row else 0

            recent_cursor = await db.execute(
                """SELECT id, platform, check_type, discrepancy, status, detail, checked_at
                   FROM reconciliation_log
                   ORDER BY checked_at DESC
                   LIMIT ?""",
                (limit,),
            )
            rows = await recent_cursor.fetchall()
            recent = [dict(row) for row in rows]

            return {
                "total_count": total_count,
                "discrepancy_count": discrepancy_count,
                "recent_discrepancies": recent,
            }
        finally:
            await close_db(db)

    @app.get("/api/system-health")
    async def get_system_health() -> Dict[str, Any]:
        """Single rollup endpoint: circuit breaker + reconciliation + invariants + snapshot age.

        Each sub-query is isolated so one failure doesn't suppress others.
        Returns overall status: 'ok', 'warn', or 'critical'.
        """
        db = await get_db()
        issues: list[str] = []
        result: Dict[str, Any] = {}

        try:
            # Circuit breaker
            try:
                cb_cursor = await db.execute(
                    "SELECT event_type, detail FROM system_events "
                    "WHERE event_type IN ('CIRCUIT_BREAKER_TRIPPED','CIRCUIT_BREAKER_RESET') "
                    "AND DATE(occurred_at) = DATE('now') ORDER BY id DESC LIMIT 1"
                )
                cb_row = await cb_cursor.fetchone()
                cb_tripped = (
                    cb_row is not None and cb_row[0] == "CIRCUIT_BREAKER_TRIPPED"
                )
                result["circuit_breaker"] = {
                    "tripped": cb_tripped,
                    "reason": cb_row[1] if cb_tripped else None,
                }
                if cb_tripped:
                    issues.append("circuit_breaker_tripped")
            except Exception as e:
                result["circuit_breaker"] = {"error": str(e)}
                issues.append("circuit_breaker_query_failed")

            # Reconciliation discrepancies (last 24h)
            try:
                _cutoff_24h = (
                    datetime.now(timezone.utc) - timedelta(hours=24)
                ).isoformat()
                rec_cursor = await db.execute(
                    "SELECT COUNT(*) FROM reconciliation_log "
                    "WHERE status = 'discrepancy' AND checked_at >= ?",
                    (_cutoff_24h,),
                )
                rec_row = await rec_cursor.fetchone()
                rec_count = rec_row[0] if rec_row else 0
                result["reconciliation_discrepancies_24h"] = rec_count
                if rec_count > 0:
                    issues.append(f"reconciliation_discrepancies:{rec_count}")
            except Exception:
                result["reconciliation_discrepancies_24h"] = None
                issues.append("reconciliation_query_failed")

            # Invariant violations (last 24h)
            try:
                _cutoff_24h = (
                    datetime.now(timezone.utc) - timedelta(hours=24)
                ).isoformat()
                inv_cursor = await db.execute(
                    "SELECT COUNT(*) FROM invariant_violations WHERE violated_at >= ?",
                    (_cutoff_24h,),
                )
                inv_row = await inv_cursor.fetchone()
                inv_count = inv_row[0] if inv_row else 0
                result["invariant_violations_24h"] = inv_count
                if inv_count > 0:
                    issues.append(f"invariant_violations:{inv_count}")
            except Exception:
                result["invariant_violations_24h"] = None
                issues.append("invariant_query_failed")

            # Snapshot age
            try:
                snap_cursor = await db.execute(
                    "SELECT snapshotted_at FROM pnl_snapshots "
                    "ORDER BY snapshotted_at DESC LIMIT 1"
                )
                snap_row = await snap_cursor.fetchone()
                if snap_row and snap_row[0]:
                    last_snap = datetime.fromisoformat(snap_row[0])
                    if last_snap.tzinfo is None:
                        last_snap = last_snap.replace(tzinfo=timezone.utc)
                    age_s = int(
                        (datetime.now(timezone.utc) - last_snap).total_seconds()
                    )
                    result["last_snapshot_age_s"] = age_s
                    if age_s > 3600:
                        issues.append(f"snapshot_stale:{age_s}s")
                else:
                    result["last_snapshot_age_s"] = None
            except Exception:
                result["last_snapshot_age_s"] = None
                issues.append("snapshot_age_query_failed")

            # Signal liveness (last 24h)
            try:
                _cutoff_24h_sig = (
                    datetime.now(timezone.utc) - timedelta(hours=24)
                ).isoformat()
                sig_live_cursor = await db.execute(
                    "SELECT COUNT(*) FROM signals WHERE fired_at >= ?",
                    (_cutoff_24h_sig,),
                )
                sig_live_row = await sig_live_cursor.fetchone()
                signals_24h_count = sig_live_row[0] if sig_live_row else 0
                result["signals_24h"] = signals_24h_count
                if signals_24h_count == 0:
                    issues.append("no_signals_24h")
            except Exception:
                result["signals_24h"] = None
                issues.append("signals_liveness_query_failed")

            # Overall status
            if any("tripped" in i or "critical" in i for i in issues):
                result["status"] = "critical"
            elif issues:
                result["status"] = "warn"
            else:
                result["status"] = "ok"
            result["issues"] = issues
            return result
        finally:
            await close_db(db)

    @app.get("/health")
    async def health_check() -> Dict[str, Any]:
        db = None
        try:
            db = await get_db()
            await db.execute("SELECT 1")
            result: Dict[str, Any] = {"status": "ok"}
            try:
                cursor = await db.execute(
                    "SELECT snapshotted_at FROM pnl_snapshots"
                    " ORDER BY snapshotted_at DESC LIMIT 1"
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    last_snap = datetime.fromisoformat(row[0])
                    if last_snap.tzinfo is None:
                        last_snap = last_snap.replace(tzinfo=timezone.utc)
                    result["last_snapshot_age_s"] = int(
                        (datetime.now(timezone.utc) - last_snap).total_seconds()
                    )
                else:
                    result["last_snapshot_age_s"] = None
            except Exception:
                pass
            try:
                sig_cursor = await db.execute(
                    "SELECT fired_at FROM signals ORDER BY fired_at DESC LIMIT 1"
                )
                sig_row = await sig_cursor.fetchone()
                if sig_row and sig_row[0]:
                    last_sig = datetime.fromisoformat(sig_row[0])
                    if last_sig.tzinfo is None:
                        last_sig = last_sig.replace(tzinfo=timezone.utc)
                    result["last_signal_age_s"] = int(
                        (datetime.now(timezone.utc) - last_sig).total_seconds()
                    )
                else:
                    result["last_signal_age_s"] = None
            except Exception:
                pass
            return result
        except Exception as e:
            from fastapi.responses import JSONResponse

            return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)
        finally:
            if db is not None:
                await close_db(db)

    # ── Serve React frontend if static_dir provided ───────────────────────────────────────────────────────────────────────────────────────
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


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_loopback_host(host: str) -> bool:
    return host in _LOOPBACK_HOSTS


def _enforce_host_auth_policy(host: str) -> str:
    """Fail-closed auth guard for dashboard host binding.

    If the caller asked to bind to a non-loopback interface but no
    DASHBOARD_PASSWORD is set, refuse to expose the dashboard publicly and
    force-bind to 127.0.0.1 instead. Previously the auth middleware was only
    attached when DASHBOARD_PASSWORD was non-empty, so an unset password on
    0.0.0.0 silently served every endpoint unauthenticated.
    """
    if not _is_loopback_host(host) and not os.getenv("DASHBOARD_PASSWORD", ""):
        logger.error(
            "Refusing to bind dashboard to %s with no DASHBOARD_PASSWORD set — "
            "falling back to 127.0.0.1. Set DASHBOARD_PASSWORD in the "
            "environment to expose publicly.",
            host,
        )
        return "127.0.0.1"
    return host


async def start_dashboard_server(
    app: FastAPI,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> None:
    """
    Start uvicorn as an async coroutine (non-blocking).
    Designed to be launched as an asyncio.create_task() inside
    trading_session.py.
    """
    host = _enforce_host_auth_policy(host)

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

    host = _enforce_host_auth_policy(args.host)

    uvicorn.run(app, host=host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
