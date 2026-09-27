"""Persistent execution halt (core/engine/execution_control.py)."""

import sys
from unittest.mock import MagicMock

import aiosqlite
import pytest

from core.engine import execution_control
from core.engine.execution_control import clear_halt, get_halt, halt, is_halted


@pytest.fixture
def alerts(monkeypatch):
    mgr = MagicMock()
    monkeypatch.setattr(execution_control, "get_alert_manager", lambda: mgr)
    return mgr


async def _events(db) -> list[tuple]:
    cursor = await db.execute(
        "SELECT event_type, severity, component FROM system_events ORDER BY id"
    )
    return [tuple(r) for r in await cursor.fetchall()]


async def test_fresh_db_is_not_halted(db):
    assert await is_halted(db) is False
    assert await get_halt(db) is None


async def test_halt_persists_and_alerts(db, alerts):
    await halt(db, "unbalanced_arb pair=p1", component="arb_engine")
    assert await is_halted(db) is True
    state = await get_halt(db)
    assert state is not None
    assert state["reason"] == "unbalanced_arb pair=p1"
    assert state["halted_at"]
    assert await _events(db) == [("EXECUTION_HALTED", "critical", "arb_engine")]
    alerts.send_nowait.assert_called_once()
    assert alerts.send_nowait.call_args.kwargs["component"] == "arb_engine"


async def test_halt_is_idempotent_and_keeps_first_reason(db, alerts):
    await halt(db, "first", component="arb_engine")
    await halt(db, "second", component="single_platform")
    state = await get_halt(db)
    assert state is not None and state["reason"] == "first"
    assert len(await _events(db)) == 1


async def test_clear_halt(db, alerts):
    await halt(db, "unknown_fill", component="arb_engine")
    await clear_halt(db, reason="operator checked both venues")
    assert await is_halted(db) is False
    assert (await _events(db))[-1][0] == "EXECUTION_HALT_CLEARED"


async def test_survives_reconnect(tmp_path, alerts):
    from tests.integration.conftest import _apply_migrations

    path = tmp_path / "halt.db"
    async with aiosqlite.connect(path) as conn:
        await _apply_migrations(conn)
        await halt(conn, "unknown_fill", component="arb_engine")
    async with aiosqlite.connect(path) as conn:
        assert await is_halted(conn) is True


async def test_missing_table_reads_as_not_halted():
    async with aiosqlite.connect(":memory:") as conn:
        assert await is_halted(conn) is False


class TestClearHaltScript:
    async def _run(self, monkeypatch, tmp_path, argv):
        from scripts import clear_halt as script
        from tests.integration.conftest import _apply_migrations

        path = tmp_path / "halt.db"
        async with aiosqlite.connect(path) as conn:
            await _apply_migrations(conn)
            monkeypatch.setattr(execution_control, "get_alert_manager", MagicMock)
            await halt(conn, "unknown_fill", component="arb_engine")
        monkeypatch.setattr(sys, "argv", ["clear_halt.py", "--db", str(path), *argv])
        code = await script.main()
        async with aiosqlite.connect(path) as conn:
            return code, await is_halted(conn)

    async def test_requires_reason(self, monkeypatch, tmp_path):
        with pytest.raises(SystemExit):
            await self._run(monkeypatch, tmp_path, [])

    async def test_clears_with_reason(self, monkeypatch, tmp_path):
        code, halted = await self._run(
            monkeypatch, tmp_path, ["--reason", "reconciled manually"]
        )
        assert code == 0 and halted is False
