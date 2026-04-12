"""
Tests for Phase 7: Invariants, alerting integration, and dashboard endpoints.

Covers:
  7.1  Invariant checks: PnL sanity, position duration, orphan positions,
       fee ratio, engine state consistency
  7.2  check_all_invariants orchestrator: warn-only vs halt mode
  7.3  Discord alert fired on invariant violation
  7.5  Dashboard endpoints: /api/pnl-split, /api/invariants
"""

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from httpx import ASGITransport, AsyncClient  # noqa: E402
from scripts.dashboard_api import create_dashboard_app  # noqa: E402
from tests.paper.conftest import _apply_migrations  # noqa: E402

from core.invariants import (  # noqa: E402
    InvariantResult,
    InvariantViolation,
    check_all_invariants,
    check_fee_ratio,
    check_orphan_positions,
    check_pnl_sanity,
    check_position_duration,
    check_engine_state,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def app_and_client(tmp_path):
    """Dashboard app backed by a temp file DB with full schema applied."""
    db_path = str(tmp_path / "test_p7.db")
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await _apply_migrations(conn)
    await conn.close()

    app = create_dashboard_app(db_path=db_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield app, client, db_path


# ── Helpers ───────────────────────────────────────────────────────────────────


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


async def _seed_signal(db, signal_id: str, market_id: str = "m1") -> None:
    await db.execute(
        """INSERT OR IGNORE INTO markets
           (id, platform, platform_id, title, status, created_at, updated_at)
           VALUES (?, 'polymarket', ?, 'Test market', 'open', ?, ?)""",
        (market_id, market_id, _now(), _now()),
    )
    await db.execute(
        """INSERT INTO signals
           (id, violation_id, strategy, signal_type, market_id_a,
            model_edge, kelly_fraction, position_size_a,
            total_capital_at_risk, status, fired_at, updated_at)
           VALUES (?, NULL, 'P3_calibration_bias', 'single', ?,
                   0.05, 0.25, 50.0, 50.0, 'fired', ?, ?)""",
        (signal_id, market_id, _now(), _now()),
    )


async def _seed_closed_position(
    db,
    *,
    signal_id: str,
    market_id: str = "m1",
    entry_price: float = 0.50,
    entry_size: float = 50.0,
    exit_price: float = 0.55,
    realized_pnl: float = 2.5,
    fees_paid: float = 1.0,
    held_seconds: float = 300.0,
    pnl_model: str = "realistic",
) -> str:
    pos_id = f"pos_{uuid.uuid4().hex[:8]}"
    opened = _ago(held_seconds)
    closed = _now()
    await db.execute(
        """INSERT INTO positions
           (id, signal_id, market_id, strategy, side, entry_price, entry_size,
            exit_price, realized_pnl, fees_paid, status,
            opened_at, closed_at, updated_at, pnl_model)
           VALUES (?, ?, ?, 'P3_calibration_bias', 'BUY', ?, ?, ?, ?, ?, 'closed',
                   ?, ?, ?, ?)""",
        (
            pos_id,
            signal_id,
            market_id,
            entry_price,
            entry_size,
            exit_price,
            realized_pnl,
            fees_paid,
            opened,
            closed,
            _now(),
            pnl_model,
        ),
    )
    await db.commit()
    return pos_id


# ── 7.1a PnL sanity ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPnLSanity:
    async def test_empty_db_passes(self, db):
        result = await check_pnl_sanity(db)
        assert result.passed

    async def test_normal_pnl_passes(self, db):
        await _seed_signal(db, "sig1")
        # edge = 0.05, size = 50 → pnl of 2.5 is reasonable
        await _seed_closed_position(
            db, signal_id="sig1", realized_pnl=2.5, entry_size=50.0
        )
        result = await check_pnl_sanity(db)
        assert result.passed

    async def test_inflated_pnl_fails(self, db):
        await _seed_signal(db, "sig1")
        # realized_pnl > entry_size is physically impossible
        await _seed_closed_position(
            db, signal_id="sig1", realized_pnl=999.0, entry_size=50.0
        )
        result = await check_pnl_sanity(db)
        assert not result.passed

    async def test_pnl_exactly_at_bound_passes(self, db):
        await _seed_signal(db, "sig1")
        # realized_pnl == entry_size is the theoretical max (bought at 0, resolved 1)
        await _seed_closed_position(
            db, signal_id="sig1", realized_pnl=50.0, entry_size=50.0
        )
        result = await check_pnl_sanity(db)
        assert result.passed

    async def test_result_names_offending_position(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(
            db, signal_id="sig1", realized_pnl=500.0, entry_size=50.0
        )
        result = await check_pnl_sanity(db)
        assert not result.passed
        assert "realized_pnl" in result.message.lower() or result.name


# ── 7.1b Position duration ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPositionDuration:
    async def test_empty_db_passes(self, db):
        result = await check_position_duration(db)
        assert result.passed

    async def test_adequate_duration_passes(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(db, signal_id="sig1", held_seconds=60.0)
        result = await check_position_duration(db)
        assert result.passed

    async def test_zero_duration_fails(self, db):
        # Insert a position where opened_at == closed_at
        await _seed_signal(db, "sig1")
        now = _now()
        pos_id = f"pos_{uuid.uuid4().hex[:8]}"
        await db.execute(
            """INSERT INTO positions
               (id, signal_id, market_id, strategy, side, entry_price, entry_size,
                exit_price, realized_pnl, fees_paid, status,
                opened_at, closed_at, updated_at, pnl_model)
               VALUES (?, 'sig1', 'm1', 'P3_calibration_bias', 'BUY',
                       0.50, 50.0, 0.55, 2.5, 1.0, 'closed', ?, ?, ?, 'realistic')""",
            (pos_id, now, now, now),
        )
        await db.commit()
        result = await check_position_duration(db)
        assert not result.passed

    async def test_open_positions_excluded(self, db):
        """Open positions have no closed_at; should not trigger duration check."""
        await _seed_signal(db, "sig1")
        pos_id = f"pos_{uuid.uuid4().hex[:8]}"
        await db.execute(
            """INSERT INTO positions
               (id, signal_id, market_id, strategy, side, entry_price, entry_size,
                status, opened_at, updated_at, pnl_model)
               VALUES (?, 'sig1', 'm1', 'P3_calibration_bias', 'BUY',
                       0.50, 50.0, 'open', ?, ?, 'realistic')""",
            (pos_id, _now(), _now()),
        )
        await db.commit()
        result = await check_position_duration(db)
        assert result.passed


# ── 7.1c Orphan positions ─────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestOrphanPositions:
    async def test_empty_db_passes(self, db):
        result = await check_orphan_positions(db)
        assert result.passed

    async def test_position_with_signal_passes(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(db, signal_id="sig1")
        result = await check_orphan_positions(db)
        assert result.passed

    async def test_orphan_reported_as_failure(self, db):
        """Orphan position (no matching signal) is caught by the soft check.

        Strategy: insert a valid position, then delete the signal with FK
        enforcement off so the cascade doesn't fire. The position survives
        as an orphan.
        """
        await _seed_signal(db, "sig_orphan_test")
        await _seed_closed_position(db, signal_id="sig_orphan_test")
        # Disable FK so we can delete the signal without cascading to the position
        await db.execute("PRAGMA foreign_keys = OFF")
        await db.execute("DELETE FROM signals WHERE id = 'sig_orphan_test'")
        await db.commit()
        result = await check_orphan_positions(db)
        assert not result.passed


# ── 7.1d Fee ratio ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestFeeRatio:
    async def test_empty_db_passes(self, db):
        result = await check_fee_ratio(db)
        assert result.passed

    async def test_normal_fees_pass(self, db):
        await _seed_signal(db, "sig1")
        # fees_paid=1.0, notional=entry_size*entry_price=50*0.50=25 → 4% fee rate
        await _seed_closed_position(
            db, signal_id="sig1", fees_paid=1.0, entry_size=50.0, entry_price=0.50
        )
        result = await check_fee_ratio(db)
        assert result.passed

    async def test_zero_fees_pass(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(db, signal_id="sig1", fees_paid=0.0)
        result = await check_fee_ratio(db)
        assert result.passed

    async def test_excessive_fees_fail(self, db):
        await _seed_signal(db, "sig1")
        # fees_paid=20.0 on notional=50*0.50=25 → 80% fee rate — clearly wrong
        await _seed_closed_position(
            db, signal_id="sig1", fees_paid=20.0, entry_size=50.0, entry_price=0.50
        )
        result = await check_fee_ratio(db)
        assert not result.passed


# ── 7.1e Engine state consistency ─────────────────────────────────────────────


class TestEngineState:
    def test_no_engine_passes(self):
        result = check_engine_state(arb_engine=None)
        assert result.passed

    def test_empty_engine_passes(self):
        engine = MagicMock()
        engine.fired_state = {}
        engine._pairs = {}
        result = check_engine_state(arb_engine=engine)
        assert result.passed

    def test_fired_state_within_pairs_passes(self):
        engine = MagicMock()
        engine.fired_state = {"pair1": object(), "pair2": object()}
        engine._pairs = {"pair1": None, "pair2": None, "pair3": None}
        result = check_engine_state(arb_engine=engine)
        assert result.passed

    def test_fired_state_exceeds_pairs_fails(self):
        engine = MagicMock()
        engine.fired_state = {"pair1": object(), "ghost": object()}
        engine._pairs = {"pair1": None}  # only 1 known pair but 2 in fired_state
        result = check_engine_state(arb_engine=engine)
        assert not result.passed


# ── 7.2 check_all_invariants orchestrator ─────────────────────────────────────


@pytest.mark.asyncio
class TestCheckAllInvariants:
    async def test_empty_db_all_pass(self, db):
        results = await check_all_invariants(db)
        assert all(r.passed for r in results)

    async def test_returns_list_of_invariant_results(self, db):
        results = await check_all_invariants(db)
        assert isinstance(results, list)
        assert all(isinstance(r, InvariantResult) for r in results)

    async def test_warn_mode_does_not_raise_on_violation(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(db, signal_id="sig1", realized_pnl=9999.0)
        # mode="warn" should not raise even with a violation
        results = await check_all_invariants(db, mode="warn")
        assert any(not r.passed for r in results)

    async def test_halt_mode_raises_on_violation(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(db, signal_id="sig1", realized_pnl=9999.0)
        with pytest.raises(InvariantViolation):
            await check_all_invariants(db, mode="halt")

    async def test_halt_mode_passes_on_clean_db(self, db):
        # Should not raise
        await check_all_invariants(db, mode="halt")

    async def test_violation_sends_discord_alert(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(db, signal_id="sig1", realized_pnl=9999.0)
        mock_mgr = AsyncMock()
        results = await check_all_invariants(db, alert_manager=mock_mgr, mode="warn")
        assert any(not r.passed for r in results)
        mock_mgr.send.assert_called()

    async def test_no_alert_when_all_pass(self, db):
        mock_mgr = AsyncMock()
        await check_all_invariants(db, alert_manager=mock_mgr, mode="warn")
        mock_mgr.send.assert_not_called()

    async def test_violation_persisted_to_db(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(db, signal_id="sig1", realized_pnl=9999.0)
        await check_all_invariants(db, mode="warn")
        cursor = await db.execute("SELECT COUNT(*) FROM invariant_violations")
        row = await cursor.fetchone()
        assert row[0] >= 1

    async def test_clean_run_writes_no_violations(self, db):
        await check_all_invariants(db, mode="warn")
        cursor = await db.execute("SELECT COUNT(*) FROM invariant_violations")
        row = await cursor.fetchone()
        assert row[0] == 0


# ── 7.5 Dashboard: /api/pnl-split ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestDashboardPnlSplit:
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


# ── 7.5 Dashboard: /api/invariants ────────────────────────────────────────────


@pytest.mark.asyncio
class TestDashboardInvariants:
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
