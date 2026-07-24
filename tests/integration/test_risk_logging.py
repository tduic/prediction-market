"""
Tests for the production _log_risk_checks and run_all_checks in core/signals/risk.py.

Exercises the DB-write path using the real schema so that the executemany
batch insert is verified against the actual risk_check_log table structure.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.signals.risk import RiskCheckResult, _log_risk_checks


class _MinimalSignal:
    """Minimal signal stub: only needs .violation_id for _log_risk_checks."""

    def __init__(self, violation_id=None):
        self.violation_id = violation_id


@pytest.mark.asyncio
async def test_log_risk_checks_inserts_all_rows(db):
    """_log_risk_checks writes one row per RiskCheckResult via executemany."""
    results = [
        RiskCheckResult(
            passed=True,
            check_type="position_limit",
            check_value=100.0,
            threshold=500.0,
            detail="Signal size $100.00 vs limit $500.00",
        ),
        RiskCheckResult(
            passed=True,
            check_type="daily_loss_limit",
            check_value=0.0,
            threshold=200.0,
            detail="Today's net loss $0.00 vs limit $200.00",
        ),
        RiskCheckResult(
            passed=False,
            check_type="portfolio_exposure",
            check_value=2100.0,
            threshold=2000.0,
            detail="Total exposure $2100.00 exceeds limit $2000.00",
        ),
        RiskCheckResult(
            passed=True,
            check_type="duplicate",
            check_value=0.0,
            threshold=0.0,
            detail="No recent orders on same markets",
        ),
        RiskCheckResult(
            passed=True,
            check_type="min_edge",
            check_value=0.05,
            threshold=0.02,
            detail="Edge 0.0500 vs minimum 0.0200",
        ),
    ]
    signal = _MinimalSignal(violation_id=None)

    await _log_risk_checks(db, signal, results)

    cursor = await db.execute(
        "SELECT check_type, passed, check_value, threshold FROM risk_check_log ORDER BY id"
    )
    rows = await cursor.fetchall()

    assert len(rows) == 5
    assert rows[0]["check_type"] == "position_limit"
    assert rows[0]["passed"] == 1
    assert rows[2]["check_type"] == "portfolio_exposure"
    assert rows[2]["passed"] == 0
    assert rows[4]["check_type"] == "min_edge"
    assert rows[4]["check_value"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_log_risk_checks_empty_results_no_error(db):
    """_log_risk_checks with an empty list commits cleanly with no rows."""
    signal = _MinimalSignal(violation_id=None)

    await _log_risk_checks(db, signal, [])

    cursor = await db.execute("SELECT COUNT(*) FROM risk_check_log")
    row = await cursor.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_log_risk_checks_null_signal_id(db):
    """signal_id is always NULL in risk_check_log (pre-signal write path)."""
    results = [
        RiskCheckResult(
            passed=True,
            check_type="min_edge",
            check_value=0.03,
            threshold=0.02,
            detail="Edge ok",
        )
    ]
    signal = _MinimalSignal(violation_id=None)

    await _log_risk_checks(db, signal, results)

    cursor = await db.execute("SELECT signal_id FROM risk_check_log LIMIT 1")
    row = await cursor.fetchone()
    assert row["signal_id"] is None
