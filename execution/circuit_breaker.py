"""
Daily loss circuit breaker.

Unlike the per-signal ``check_daily_loss_limit`` risk check, the circuit
breaker is a STICKY halt. Once today's realized losses exceed the configured
threshold, the breaker trips and rejects all subsequent signals until either:

  1. The UTC day rolls over (automatic daily reset), or
  2. An operator calls ``reset()`` manually (after investigating).

The breaker also tracks consecutive order execution failures — if the router
reports N failures in a row, the breaker trips to avoid hammering a degraded
exchange. This provides a second, orthogonal halt signal.

State is persisted in the ``system_events`` table so trips survive process
restarts within the same UTC day (on startup, the breaker checks whether it
was already tripped today and restores that state).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from core.alerting import Severity, get_alert_manager

logger = logging.getLogger(__name__)


@dataclass
class BreakerState:
    """Snapshot of the current circuit-breaker state."""

    tripped: bool
    reason: str | None
    tripped_at: str | None
    daily_loss: float
    daily_loss_limit: float
    consecutive_failures: int
    consecutive_failure_limit: int
    utc_day: str
    daily_loss_available: bool = True


class DailyLossCircuitBreaker:
    """
    Sticky daily-loss + consecutive-failure circuit breaker.

    Usage::

        breaker = DailyLossCircuitBreaker(
            db=conn,
            starting_capital=10_000,
            max_daily_loss_pct=0.02,
            consecutive_failure_limit=5,
        )
        await breaker.load_state()          # restore from DB on startup

        # Before routing every signal:
        if await breaker.should_halt():
            # reject — log and return

        # After each order lifecycle:
        await breaker.record_order_result(success=True_or_False)
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        starting_capital: float,
        max_daily_loss_pct: float,
        consecutive_failure_limit: int = 5,
    ) -> None:
        self.db = db
        self.starting_capital = starting_capital
        self.max_daily_loss_pct = max_daily_loss_pct
        self.consecutive_failure_limit = consecutive_failure_limit

        self._tripped: bool = False
        self._reason: str | None = None
        self._tripped_at: str | None = None
        self._consecutive_failures: int = 0
        self._utc_day: str = self._today()

    # ── Public API ────────────────────────────────────────────────────

    async def load_state(self) -> None:
        """
        Restore trip state from ``system_events``.

        If a ``CIRCUIT_BREAKER_TRIPPED`` event was written earlier today (UTC),
        the breaker remains tripped on startup. Trips from previous days are
        ignored — the breaker auto-resets at day boundary.
        """
        today = self._today()
        self._utc_day = today
        try:
            cursor = await self.db.execute(
                """
                SELECT detail, context, occurred_at
                FROM system_events
                WHERE event_type = 'CIRCUIT_BREAKER_TRIPPED'
                  AND DATE(occurred_at) = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (today,),
            )
            row = await cursor.fetchone()
            if row:
                self._tripped = True
                self._reason = row[0]
                self._tripped_at = row[2]
                logger.warning(
                    "Circuit breaker restored to TRIPPED state from %s: %s",
                    self._tripped_at,
                    self._reason,
                )
            else:
                logger.info("Circuit breaker clean for %s", today)
        except Exception as e:
            logger.error("Failed to load circuit breaker state: %s", e)

    async def should_halt(self) -> bool:
        """
        Returns True if trading should be halted.

        Performs a day-rollover check (auto-reset at UTC midnight) and then
        re-evaluates daily loss against the limit. If either the sticky
        ``_tripped`` flag is set OR the fresh daily-loss query exceeds the
        limit, returns True and trips the breaker if not already tripped.
        """
        await self._maybe_rollover()

        if self._tripped:
            return True

        try:
            daily_loss = await self._compute_daily_loss()
        except Exception as e:
            logger.error(
                "Failed to compute daily loss — halting as safe default: %s", e
            )
            await self._trip(
                reason=f"DB error during daily-loss query: {e}",
                context={"trip_type": "db_error", "error": str(e)},
            )
            return True

        limit = self._daily_loss_limit()

        if daily_loss >= limit:
            await self._trip(
                reason=(
                    f"Daily loss ${daily_loss:.2f} >= limit ${limit:.2f} "
                    f"({self.max_daily_loss_pct:.1%} of ${self.starting_capital:.2f})"
                ),
                context={
                    "trip_type": "daily_loss",
                    "daily_loss": daily_loss,
                    "limit": limit,
                },
            )
            return True

        return False

    async def record_order_result(self, success: bool) -> None:
        """
        Track order outcomes for the consecutive-failure halt path.

        Call this after each order submission regardless of whether the order
        filled, errored, or timed out. A single success resets the counter;
        N consecutive failures trip the breaker.
        """
        if success:
            if self._consecutive_failures > 0:
                logger.info(
                    "Circuit breaker consecutive failures reset after success (was %d)",
                    self._consecutive_failures,
                )
            self._consecutive_failures = 0
            return

        self._consecutive_failures += 1
        logger.warning(
            "Circuit breaker consecutive failure %d/%d",
            self._consecutive_failures,
            self.consecutive_failure_limit,
        )

        if self._consecutive_failures >= self.consecutive_failure_limit:
            await self._trip(
                reason=(
                    f"{self._consecutive_failures} consecutive order failures "
                    f"(limit {self.consecutive_failure_limit})"
                ),
                context={
                    "trip_type": "consecutive_failures",
                    "failures": self._consecutive_failures,
                    "limit": self.consecutive_failure_limit,
                },
            )

    async def reset(self, reason: str = "manual reset") -> None:
        """Manually clear the tripped state (for operator kill-switch off)."""
        was_tripped = self._tripped
        previous_reason = self._reason
        self._tripped = False
        self._reason = None
        self._tripped_at = None
        self._consecutive_failures = 0
        if was_tripped:
            await self._log_event(
                "CIRCUIT_BREAKER_RESET",
                severity="warning",
                detail=reason,
                context={"previous_reason": previous_reason},
            )
            logger.warning("Circuit breaker RESET: %s", reason)
            try:
                await get_alert_manager().send(
                    title="Circuit breaker RESET — trading resumed",
                    message=reason,
                    severity=Severity.WARNING,
                    component="circuit_breaker",
                )
            except Exception as e:
                logger.error("Failed to send reset alert: %s", e)

    async def get_state(self) -> BreakerState:
        """Return a snapshot of current state (for dashboards / status API)."""
        daily_loss_available = True
        try:
            daily_loss = await self._compute_daily_loss()
        except Exception as e:
            logger.warning(
                "get_state: failed to compute daily loss — reporting unavailable: %s", e
            )
            daily_loss = 0.0
            daily_loss_available = False
        return BreakerState(
            tripped=self._tripped,
            reason=self._reason,
            tripped_at=self._tripped_at,
            daily_loss=daily_loss,
            daily_loss_limit=self._daily_loss_limit(),
            consecutive_failures=self._consecutive_failures,
            consecutive_failure_limit=self.consecutive_failure_limit,
            utc_day=self._utc_day,
            daily_loss_available=daily_loss_available,
        )

    # ── Internal helpers ──────────────────────────────────────────────

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _daily_loss_limit(self) -> float:
        return self.starting_capital * self.max_daily_loss_pct

    async def _compute_daily_loss(self) -> float:
        """
        Return today's realized net losses as a positive float.

        Sums ``actual_pnl - fees_total`` from ``trade_outcomes`` dated
        today (UTC). Returns 0.0 if net P&L is positive (no loss).

        Raises:
            Exception: Re-raised if the DB query fails so that ``should_halt``
                can apply the safe-by-default halt policy on DB errors.
        """
        cursor = await self.db.execute(
            """
            SELECT COALESCE(SUM(actual_pnl - COALESCE(fees_total, 0)), 0)
            FROM trade_outcomes
            WHERE DATE(created_at) = ?
            """,
            (self._today(),),
        )
        row = await cursor.fetchone()
        net_pnl = float(row[0]) if row and row[0] is not None else 0.0
        return max(0.0, -net_pnl)

    async def _maybe_rollover(self) -> None:
        """Auto-reset at UTC day boundary."""
        today = self._today()
        if today != self._utc_day:
            was_tripped = self._tripped
            previous_reason = self._reason
            previous_day = self._utc_day
            logger.info(
                "Circuit breaker day rollover %s → %s, auto-reset",
                previous_day,
                today,
            )
            self._utc_day = today
            self._tripped = False
            self._reason = None
            self._tripped_at = None
            self._consecutive_failures = 0
            if was_tripped:
                detail = (
                    f"UTC day rollover {previous_day} → {today}; "
                    f"previous trip reason: {previous_reason}"
                )
                await self._log_event(
                    "CIRCUIT_BREAKER_RESET",
                    severity="warning",
                    detail=detail,
                    context={
                        "reset_type": "utc_day_rollover",
                        "previous_reason": previous_reason,
                        "previous_day": previous_day,
                        "new_day": today,
                    },
                )
                logger.warning(
                    "Circuit breaker auto-reset on UTC day rollover (was tripped: %s)",
                    previous_reason,
                )
                try:
                    await get_alert_manager().send(
                        title="Circuit breaker auto-reset — UTC day rollover",
                        message=detail,
                        severity=Severity.WARNING,
                        component="circuit_breaker",
                    )
                except Exception as e:
                    logger.error("Failed to send auto-reset alert: %s", e)

    async def _trip(self, reason: str, context: dict[str, Any]) -> None:
        """Mark the breaker tripped and persist a system event."""
        if self._tripped:
            return
        self._tripped = True
        self._reason = reason
        self._tripped_at = datetime.now(timezone.utc).isoformat()
        logger.error("CIRCUIT BREAKER TRIPPED: %s", reason)
        await self._log_event(
            "CIRCUIT_BREAKER_TRIPPED",
            severity="critical",
            detail=reason,
            context=context,
        )
        try:
            await get_alert_manager().send(
                title="Circuit breaker TRIPPED — trading halted",
                message=reason,
                severity=Severity.CRITICAL,
                context=context,
                component="circuit_breaker",
            )
        except Exception as e:
            logger.error("Failed to send circuit-breaker alert: %s", e)

    async def _log_event(
        self,
        event_type: str,
        severity: str,
        detail: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self.db.execute(
                """
                INSERT INTO system_events
                (event_type, severity, component, detail, context, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    severity,
                    "circuit_breaker",
                    detail,
                    json.dumps(context) if context else None,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            await self.db.commit()
        except Exception as e:
            logger.error("Failed to log circuit breaker event: %s", e)
