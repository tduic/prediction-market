"""Tests for operator scripts: clear_halt.py and verify_api_auth.py."""

import importlib.util
import sqlite3
from pathlib import Path

import aiosqlite
import pytest

ROOT = Path(__file__).parent.parent.parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


clear_halt_script = _load("clear_halt")


async def _migrated_db(path: Path) -> None:
    from tests.integration.conftest import _apply_migrations

    db = await aiosqlite.connect(str(path))
    await _apply_migrations(db)
    await db.close()


# ── clear_halt.py ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clear_halt_refuses_missing_db_and_does_not_create_it(tmp_path, capsys):
    missing = tmp_path / "nope.db"
    rc = await clear_halt_script.run(str(missing), reason=None, status_only=True)
    assert rc != 0
    assert not missing.exists()
    assert "does not exist" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_clear_halt_refuses_db_without_execution_control(tmp_path, capsys):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    rc = await clear_halt_script.run(str(path), reason=None, status_only=True)
    assert rc != 0
    assert "execution_control" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_clear_halt_status_not_halted(tmp_path, capsys):
    path = tmp_path / "ok.db"
    await _migrated_db(path)
    rc = await clear_halt_script.run(str(path), reason=None, status_only=True)
    assert rc == 0
    assert "Not halted" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_clear_halt_clears_a_halt(tmp_path, capsys):
    from core.engine.execution_control import get_halt, halt

    path = tmp_path / "halted.db"
    await _migrated_db(path)
    async with aiosqlite.connect(str(path)) as db:
        await halt(db, reason="one leg filled", component="test")

    rc = await clear_halt_script.run(str(path), reason="reconciled", status_only=False)
    assert rc == 0
    assert "Halt cleared" in capsys.readouterr().out
    async with aiosqlite.connect(str(path)) as db:
        assert await get_halt(db) is None


@pytest.mark.asyncio
async def test_clear_halt_unreadable_db_exits_nonzero(tmp_path, capsys):
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"this is not a sqlite database" * 100)
    rc = await clear_halt_script.run(str(path), reason=None, status_only=True)
    assert rc != 0
    assert "cannot read" in capsys.readouterr().err.lower()


# ── verify_api_auth.py ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kalshi_ok", "poly_ok", "expected"),
    [(True, True, 0), (False, True, 1), (True, False, 1)],
)
async def test_verify_api_auth_exit_code(monkeypatch, kalshi_ok, poly_ok, expected):
    verify = _load("verify_api_auth")

    async def fake_kalshi():
        return kalshi_ok

    async def fake_poly():
        return poly_ok

    monkeypatch.setattr(verify, "verify_kalshi", fake_kalshi)
    monkeypatch.setattr(verify, "verify_polymarket", fake_poly)
    assert await verify.main() == expected
