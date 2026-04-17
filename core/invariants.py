"""
Invariant assertion module for the prediction market trading system.

Each invariant is a named rule that should always hold in a healthy system.
Invariants are checked periodically (every status cycle) and at key write
sites. Failures are:
  - Logged at WARNING level
  - Persisted to the invariant_violations table
  - Sent to Discord (if an alert_manager is provided)
  - Raised as InvariantViolation when mode="halt" (requires human restart)

Usage::

    results = await check_all_invariants(db, arb_engine=engine, mode="warn")
    failed = [r for r in results if not r.passed]

Start with mode="warn" for the first week; promote to mode="halt" once the
thresholds are proven non-spurious.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger(__name__)


# ── Types ─────────────────────────────────────────────────────────────────────


@dataclass
class InvariantResult:
    """Outcome of a single invariant check."""

    name: str
    passed: bool
    message: str
    severity: str = "critical"


class InvariantViolation(RuntimeError):
    """Raised by check_all_invariants when mode='halt' and any check fails."""


# ── Individual invariant checks ───────────────────────────────────────────────


async def check_pnl_sanity(db: aiosqlite.Connection) -> InvariantResult:
    """Verify no closed position has realized_pnl > entry_size.

    The maximum theoretical profit on a binary outcome is the full stake
    (buy at price→0, market resolves YES→1). Any position exceeding its
    stake is a data error or calculation bug.
    """
    cursor = await db.execute(
        """SELECT id, realized_pnl, entry_size
           FROM positions
           WHERE status = 'closed'
             AND realized_pnl IS NOT NULL
             AND realized_pnl > entry_size * 1.001"""  # 0.1% tolerance for float rounding
    )
    offenders = list(await cursor.fetchall())

    if not offenders:
        return InvariantResult(
            name="pnl_sanity",
            passed=True,
            message="All closed positions have realized_pnl <= entry_size.",
        )

    ids = ", ".join(str(r[0]) for r in offenders[:3])
    return InvariantResult(
        name="pnl_sanity",
        passed=False,
        message=(
            f"{len(offenders)} position(s) have realized_pnl > entry_size "
            f"(first offenders: {ids}). "
            "This indicates a calculation bug or data corruption."
        ),
    )


async def check_position_duration(db: aiosqlite.Connection) -> InvariantResult:
    """Verify no closed position has a close timestamp earlier than its open.

    Atomic arbitrage trades legitimately open and close in the same tick
    (``ArbitrageEngine._execute_arb_trade`` writes ``opened_at == closed_at``
    when both legs fill), so zero-duration positions are allowed. What is
    never legitimate is a close timestamp *strictly before* the open — that
    would indicate a real timestamp bug in ``mark_and_close_positions`` or
    the settlement path.
    """
    cursor = await db.execute("""SELECT id, opened_at, closed_at
           FROM positions
           WHERE status = 'closed'
             AND closed_at IS NOT NULL
             AND opened_at IS NOT NULL
             AND closed_at < opened_at""")
    offenders = list(await cursor.fetchall())

    if not offenders:
        return InvariantResult(
            name="position_duration",
            passed=True,
            message="All closed positions have closed_at >= opened_at.",
        )

    ids = ", ".join(str(r[0]) for r in offenders[:3])
    return InvariantResult(
        name="position_duration",
        passed=False,
        message=(
            f"{len(offenders)} position(s) have closed_at < opened_at "
            f"(first offenders: {ids}). "
            "Indicates a timestamp bug in mark_and_close_positions or settlement."
        ),
    )


async def check_orphan_positions(db: aiosqlite.Connection) -> InvariantResult:
    """Verify every position has a corresponding signal row.

    The FK constraint enforces this at the DB level, but the constraint can
    be bypassed (PRAGMA foreign_keys = OFF) during migrations or bulk inserts.
    This soft check catches any leakage.
    """
    cursor = await db.execute("""SELECT p.id
           FROM positions p
           LEFT JOIN signals s ON s.id = p.signal_id
           WHERE s.id IS NULL""")
    offenders = list(await cursor.fetchall())

    if not offenders:
        return InvariantResult(
            name="orphan_positions",
            passed=True,
            message="All positions have a matching signal row.",
        )

    ids = ", ".join(str(r[0]) for r in offenders[:3])
    return InvariantResult(
        name="orphan_positions",
        passed=False,
        message=(
            f"{len(offenders)} position(s) have no matching signal row "
            f"(first orphans: {ids}). "
            "Risk checks were bypassed for these positions."
        ),
    )


async def check_fee_ratio(db: aiosqlite.Connection) -> InvariantResult:
    """Verify aggregate fee ratio is in a plausible range [0, 20%].

    Fees / notional above 20% indicates a fee calculation bug or an
    off-by-100x error (bps vs fraction confusion).
    """
    cursor = await db.execute("""SELECT SUM(fees_paid), SUM(entry_size * entry_price)
           FROM positions
           WHERE status = 'closed'
             AND fees_paid IS NOT NULL
             AND entry_size IS NOT NULL
             AND entry_price IS NOT NULL""")
    row = await cursor.fetchone()
    total_fees = (row[0] if row else 0.0) or 0.0
    total_notional = (row[1] if row else 0.0) or 0.0

    if total_notional == 0.0:
        return InvariantResult(
            name="fee_ratio",
            passed=True,
            message="No closed positions with notional — fee ratio check skipped.",
        )

    ratio = total_fees / total_notional
    if ratio > 0.20:
        return InvariantResult(
            name="fee_ratio",
            passed=False,
            message=(
                f"Aggregate fee ratio {ratio:.2%} exceeds 20% threshold "
                f"(fees={total_fees:.2f}, notional={total_notional:.2f}). "
                "Likely a fee calculation bug (bps vs fraction)."
            ),
        )

    return InvariantResult(
        name="fee_ratio",
        passed=True,
        message=f"Fee ratio {ratio:.2%} is within acceptable bounds.",
    )


def check_engine_state(arb_engine=None) -> InvariantResult:
    """Verify len(fired_state) <= len(_pairs) on the ArbitrageEngine.

    fired_state tracks pairs that have recently fired. It can only contain
    pairs known to the engine; any extra entries indicate a ghost state
    (pair removed from _pairs but lingering in fired_state).
    """
    if arb_engine is None:
        return InvariantResult(
            name="engine_state",
            passed=True,
            message="No ArbitrageEngine provided — engine state check skipped.",
        )

    fired_count = len(arb_engine.fired_state)
    pairs_count = len(arb_engine._pairs)

    if fired_count > pairs_count:
        return InvariantResult(
            name="engine_state",
            passed=False,
            message=(
                f"fired_state has {fired_count} entries but _pairs has only "
                f"{pairs_count}. Ghost entries in fired_state — possible memory leak."
            ),
        )

    return InvariantResult(
        name="engine_state",
        passed=True,
        message=f"Engine state consistent: {fired_count}/{pairs_count} pairs in fired_state.",
    )


# ── Orchestrator ──────────────────────────────────────────────────────────────


async def check_all_invariants(
    db: aiosqlite.Connection,
    arb_engine=None,
    mode: str = "warn",
    alert_manager=None,
) -> list[InvariantResult]:
    """Run all invariant checks and handle failures according to mode.

    Args:
        db: Active aiosqlite connection with the full schema applied.
        arb_engine: Optional ArbitrageEngine for in-memory state checks.
        mode: "warn" — log and return results without raising.
              "halt" — raise InvariantViolation on the first failure.
        alert_manager: Optional AlertManager to send Discord alerts on failure.

    Returns:
        List of InvariantResult, one per check run.

    Raises:
        InvariantViolation: If mode="halt" and any check fails.
    """
    async_checks = [
        check_pnl_sanity(db),
        check_position_duration(db),
        check_orphan_positions(db),
        check_fee_ratio(db),
    ]

    import asyncio

    results: list[InvariantResult] = list(await asyncio.gather(*async_checks))
    results.append(check_engine_state(arb_engine))

    now = datetime.now(timezone.utc).isoformat()
    first_failure: InvariantResult | None = None

    for result in results:
        if result.passed:
            continue

        logger.warning("INVARIANT VIOLATION [%s]: %s", result.name, result.message)

        # Persist to DB
        try:
            await db.execute(
                """INSERT OR IGNORE INTO invariant_violations
                   (id, name, message, severity, violated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    uuid.uuid4().hex,
                    result.name,
                    result.message,
                    result.severity,
                    now,
                ),
            )
            await db.commit()
        except Exception as exc:
            logger.error("Failed to persist invariant violation: %s", exc)

        # Send Discord alert
        if alert_manager is not None:
            try:
                from core.alerting import Severity

                await alert_manager.send(
                    title=f"Invariant violation: {result.name}",
                    message=result.message,
                    severity=Severity.CRITICAL,
                    context={"invariant": result.name, "mode": mode},
                )
            except Exception as exc:
                logger.error("Failed to send invariant alert: %s", exc)

        if first_failure is None:
            first_failure = result

    if mode == "halt" and first_failure is not None:
        raise InvariantViolation(
            f"Invariant '{first_failure.name}' violated: {first_failure.message}. "
            "System halted — human intervention required."
        )

    return results
