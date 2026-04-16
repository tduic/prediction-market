"""
Tests for the FastAPI dashboard endpoints (scripts/dashboard_api.py).

Uses httpx AsyncClient against the real app (no mocking). The DB is an
in-memory aiosqlite connection seeded via the paper conftest fixtures.
All endpoints are tested for shape, type, and sensible defaults.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from httpx import ASGITransport, AsyncClient  # noqa: E402

from scripts.dashboard_api import create_dashboard_app  # noqa: E402

# ── Helpers ──────────────────────────────────────────────────────────────────


async def _seed_trade_outcome(db, strategy="P1_cross_market_arb", pnl=5.0, fees=0.10):
    now = datetime.now(timezone.utc).isoformat()
    sig_id = f"sig_{abs(pnl):.0f}_{strategy[:6]}"
    # Seed a minimal market row so trade_outcomes FK is satisfied
    await db.execute(
        "INSERT OR IGNORE INTO markets (id, platform, platform_id, title, status, created_at, updated_at) VALUES ('test_market', 'polymarket', 'test_market', 'Test', 'open', ?, ?)",
        (now, now),
    )
    await db.execute(
        """INSERT OR IGNORE INTO signals
           (id, violation_id, strategy, signal_type, market_id_a,
            model_edge, kelly_fraction, position_size_a, total_capital_at_risk,
            status, fired_at, updated_at)
           VALUES (?, NULL, ?, 'arb_pair', 'test_market',
                   0.05, 0.10, 5.0, 10.0, 'fired', ?, ?)""",
        (sig_id, strategy, now, now),
    )
    await db.execute(
        """INSERT OR IGNORE INTO trade_outcomes
           (id, signal_id, strategy, violation_id, market_id_a,
            predicted_edge, predicted_pnl, actual_pnl, fees_total,
            edge_captured_pct, signal_to_fill_ms, holding_period_ms,
            spread_at_signal, volume_at_signal, liquidity_at_signal,
            resolved_at, created_at)
           VALUES (?, ?, ?, NULL, 'test_market',
                   0.05, ?, ?, ?,
                   100.0, 50, 5000,
                   0.02, 10000.0, 10000.0,
                   ?, ?)""",
        (f"to_{sig_id}", sig_id, strategy, round(pnl * 0.9, 4), pnl, fees, now, now),
    )
    await db.commit()


async def _seed_pnl_snapshot(db, realized_pnl=100.0, capital=10100.0, fees=5.0):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO pnl_snapshots
           (total_capital, cash, open_positions_count, unrealized_pnl,
            realized_pnl_total, fees_total, snapshotted_at)
           VALUES (?, ?, 2, 10.0, ?, ?, ?)""",
        (capital, capital - 200, realized_pnl, fees, now),
    )
    await db.commit()


@pytest.fixture
async def app_and_client(db, tmp_path):
    """Build the dashboard app backed by a temp DB file seeded from the async db fixture."""
    # Write the in-memory DB to a temp file so create_dashboard_app can open it
    import aiosqlite

    db_path = str(tmp_path / "test_dashboard.db")

    # Re-apply migrations to the file-based DB
    from tests.integration.conftest import _apply_migrations

    file_db = await aiosqlite.connect(db_path)
    file_db.row_factory = aiosqlite.Row
    await _apply_migrations(file_db)
    await file_db.close()

    app = create_dashboard_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield app, client, db_path


# ── /health ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestHealthEndpoint:
    async def test_health_returns_ok(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ── /api/overview ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestOverviewEndpoint:
    async def test_returns_expected_keys(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/overview")
        assert resp.status_code == 200
        data = resp.json()
        for key in (
            "total_capital",
            "cash",
            "deployed",
            "open_positions",
            "unrealized_pnl",
            "realized_pnl_total",
            "total_fees",
            "net_return_pct",
        ):
            assert key in data, f"Missing key: {key}"

    async def test_empty_db_returns_defaults(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert data["realized_pnl_total"] == 0.0
        assert data["total_fees"] == 0.0

    async def test_reflects_trade_outcomes(self, app_and_client):
        _, client, db_path = app_and_client
        import aiosqlite

        async with aiosqlite.connect(db_path) as file_db:
            file_db.row_factory = aiosqlite.Row
            await _seed_trade_outcome(file_db, pnl=50.0, fees=1.0)

        resp = await client.get("/api/overview")
        assert resp.status_code == 200
        data = resp.json()
        assert data["realized_pnl_total"] == 50.0
        assert data["total_fees"] == 1.0

    async def test_snapshot_takes_precedence(self, app_and_client):
        _, client, db_path = app_and_client
        import aiosqlite

        async with aiosqlite.connect(db_path) as file_db:
            file_db.row_factory = aiosqlite.Row
            await _seed_pnl_snapshot(
                file_db, realized_pnl=200.0, capital=10200.0, fees=5.0
            )

        resp = await client.get("/api/overview")
        data = resp.json()
        assert data["total_capital"] == 10200.0


# ── /api/strategies ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestStrategiesEndpoint:
    async def test_returns_list(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/strategies")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_empty_db_returns_empty_list(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/strategies")
        assert resp.json() == []

    async def test_strategy_row_shape(self, app_and_client):
        _, client, db_path = app_and_client
        import aiosqlite

        async with aiosqlite.connect(db_path) as file_db:
            file_db.row_factory = aiosqlite.Row
            await _seed_trade_outcome(
                file_db, strategy="P3_calibration_bias", pnl=10.0, fees=0.5
            )

        resp = await client.get("/api/strategies")
        rows = resp.json()
        assert len(rows) == 1
        row = rows[0]
        for key in ("strategy", "trade_count", "win_count", "avg_pnl", "total_pnl"):
            assert key in row, f"Missing key: {key}"

    async def test_days_param_filters_results(self, app_and_client):
        """?days=1 vs ?days=30 — with fresh data, both return the same row."""
        _, client, db_path = app_and_client
        import aiosqlite

        async with aiosqlite.connect(db_path) as file_db:
            file_db.row_factory = aiosqlite.Row
            await _seed_trade_outcome(
                file_db, strategy="P1_cross_market_arb", pnl=5.0, fees=0.1
            )

        resp_30 = await client.get("/api/strategies?days=30")
        resp_1 = await client.get("/api/strategies?days=1")
        # Both should see the fresh trade
        assert len(resp_30.json()) >= 1
        assert len(resp_1.json()) >= 1


# ── /api/trades ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestTradesEndpoint:
    async def test_returns_list(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/trades")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_empty_db_no_trades(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/trades")
        assert resp.json() == []

    async def test_trade_row_shape(self, app_and_client):
        _, client, db_path = app_and_client
        import aiosqlite

        async with aiosqlite.connect(db_path) as file_db:
            file_db.row_factory = aiosqlite.Row
            await _seed_trade_outcome(file_db, pnl=7.5)

        resp = await client.get("/api/trades")
        rows = resp.json()
        assert len(rows) >= 1
        row = rows[0]
        assert "strategy" in row
        assert "actual_pnl" in row

    async def test_strategy_filter(self, app_and_client):
        _, client, db_path = app_and_client
        import aiosqlite

        async with aiosqlite.connect(db_path) as file_db:
            file_db.row_factory = aiosqlite.Row
            await _seed_trade_outcome(file_db, strategy="P3_calibration_bias", pnl=3.0)
            await _seed_trade_outcome(file_db, strategy="P1_cross_market_arb", pnl=5.0)

        resp = await client.get("/api/trades?strategy=P3_calibration_bias")
        rows = resp.json()
        assert all(r["strategy"] == "P3_calibration_bias" for r in rows)


# ── /api/risk ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestRiskEndpoint:
    async def test_returns_expected_keys(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/risk")
        assert resp.status_code == 200
        data = resp.json()
        for key in (
            "max_drawdown_pct",
            "max_drawdown",
            "concentration_pct",
            "daily_var",
            "sharpe_overall",
        ):
            assert key in data, f"Missing key: {key}"

    async def test_empty_db_returns_zeros(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/risk")
        data = resp.json()
        assert data["max_drawdown_pct"] == 0
        assert data["sharpe_overall"] == 0


# ── /api/fees ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestFeesEndpoint:
    async def test_returns_expected_keys(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/fees")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_fees" in data
        assert "by_platform" in data
        assert "by_strategy" in data

    async def test_empty_db_zero_fees(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/fees")
        data = resp.json()
        assert data["total_fees"] == 0.0


# ── /api/equity-curve ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestEquityCurveEndpoint:
    async def test_returns_list(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/equity-curve")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_empty_db_empty_list(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/equity-curve")
        assert resp.json() == []


# ── /api/circuit-breaker ──────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestCircuitBreakerEndpoint:
    async def test_returns_expected_keys(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/circuit-breaker")
        assert resp.status_code == 200
        data = resp.json()
        for key in (
            "tripped",
            "daily_loss",
            "daily_loss_limit",
            "daily_loss_limit_pct",
        ):
            assert key in data, f"Missing key: {key}"

    async def test_not_tripped_by_default(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/circuit-breaker")
        assert resp.json()["tripped"] is False
        assert resp.json()["daily_loss"] == 0.0


# ── /api/pnl-split ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPnlSplitEndpoint:
    async def test_pnl_split_endpoint_exists(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/pnl-split")
        assert resp.status_code == 200

    async def test_pnl_split_returns_both_models(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/pnl-split")
        data = resp.json()
        assert "realistic" in data
        assert "synthetic" in data

    async def test_pnl_split_numeric_fields(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/pnl-split")
        data = resp.json()
        for model in ("realistic", "synthetic"):
            assert isinstance(data[model]["trade_count"], int)
            assert isinstance(data[model]["total_pnl"], (int, float))
            assert isinstance(data[model]["total_fees"], (int, float))


# ── /api/invariants ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestInvariantsEndpoint:
    async def test_invariants_endpoint_exists(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/invariants")
        assert resp.status_code == 200

    async def test_invariants_endpoint_returns_expected_keys(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/invariants")
        data = resp.json()
        assert "violation_count" in data
        assert "recent_violations" in data

    async def test_invariants_empty_initially(self, app_and_client):
        _, client, _ = app_and_client
        resp = await client.get("/api/invariants")
        data = resp.json()
        assert data["violation_count"] == 0
        assert data["recent_violations"] == []
