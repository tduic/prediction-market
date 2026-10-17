"""trade_outcomes.actual_pnl is already NET of fees (every writer subtracts
them: arb_engine, single_platform mark-to-market, resolution). fees_total is
informational. Readers must not subtract fees a second time."""

from datetime import UTC, datetime

import pytest

from core.signals.risk import check_daily_loss_limit, get_portfolio_value
from core.snapshots.pnl import take_trading_snapshot
from execution.circuit_breaker import DailyLossCircuitBreaker
from scripts.dashboard_api import _compute_daily_loss_today


@pytest.fixture
async def one_trade(db):
    """A winning trade: +0.10 net after 0.40 of fees (0.50 gross)."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO markets (id, platform, platform_id, title, created_at, updated_at)"
        " VALUES ('poly_x', 'polymarket', 'x', 't', ?, ?)",
        (now, now),
    )
    await db.execute(
        "INSERT INTO signals (id, strategy, signal_type, market_id_a, model_edge,"
        " kelly_fraction, position_size_a, total_capital_at_risk, status, fired_at,"
        " updated_at) VALUES ('s1', 'P1_cross_market_arb', 'arb_pair', 'poly_x',"
        " 0.05, 0.1, 10, 5, 'fired', ?, ?)",
        (now, now),
    )
    await db.execute(
        "INSERT INTO trade_outcomes (id, signal_id, strategy, market_id_a,"
        " actual_pnl, fees_total, resolved_at, created_at) VALUES"
        " ('t1', 's1', 'P1_cross_market_arb', 'poly_x', 0.10, 0.40, ?, ?)",
        (now, now),
    )
    await db.commit()
    return db


async def test_portfolio_value_counts_fees_once(one_trade):
    assert await get_portfolio_value(one_trade, 10_000.0) == pytest.approx(10_000.10)


async def test_daily_loss_check_sees_no_loss(one_trade):
    result = await check_daily_loss_limit(10_000.0, 0.02, one_trade)
    assert result.check_value == pytest.approx(0.0)


async def test_breaker_daily_loss_is_zero(one_trade):
    breaker = DailyLossCircuitBreaker(one_trade, 10_000.0, 0.02)
    assert await breaker._compute_daily_loss() == pytest.approx(0.0)


async def test_dashboard_daily_loss_is_zero(one_trade):
    assert await _compute_daily_loss_today(one_trade) == pytest.approx(0.0)


async def test_snapshot_capital_counts_fees_once(one_trade):
    snapshot_id = await take_trading_snapshot(one_trade)
    cursor = await one_trade.execute(
        "SELECT total_capital FROM pnl_snapshots WHERE id = ?", (snapshot_id,)
    )
    assert (await cursor.fetchone())[0] == pytest.approx(10_000.10)


async def test_dashboard_overview_and_strategies_count_fees_once(one_trade, tmp_path):
    import aiosqlite
    from httpx import ASGITransport, AsyncClient

    from scripts.dashboard_api import create_dashboard_app
    from tests.integration.conftest import _apply_migrations

    # Copy the seeded rows into a file DB the app can open.
    path = tmp_path / "dash.db"
    async with aiosqlite.connect(path) as file_db:
        await _apply_migrations(file_db)
        for table in ("markets", "signals", "trade_outcomes"):
            cursor = await one_trade.execute(f"SELECT * FROM {table}")
            rows = await cursor.fetchall()
            cols = len(rows[0])
            await file_db.executemany(
                f"INSERT INTO {table} VALUES ({','.join('?' * cols)})",
                [tuple(r) for r in rows],
            )
        await file_db.commit()
    app = create_dashboard_app(db_path=str(path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        overview = (await c.get("/api/overview")).json()
        strategies = (await c.get("/api/strategies")).json()
    assert overview["net_pnl"] == pytest.approx(0.10)
    rows = strategies if isinstance(strategies, list) else strategies["strategies"]
    assert rows[0]["net_pnl"] == pytest.approx(0.10)
