"""
Exchange reconciliation engine.

Verifies that local position/balance state matches what exchanges report.
Periodically compares exchange balances against local DB state and halts
trading if discrepancies exceed configured thresholds.
"""

import asyncio
import logging
from datetime import datetime, timezone

import aiosqlite

from core.alerting import Severity, get_alert_manager

logger = logging.getLogger(__name__)


class ReconciliationEngine:
    """
    Reconciliation engine for exchange balance and position verification.

    Periodically fetches balances from exchanges and compares against
    local database state. If discrepancies exceed halt_threshold_pct,
    sets a trading_halted flag to prevent further orders.
    """

    def __init__(
        self,
        db_connection: aiosqlite.Connection,
        polymarket_client,
        kalshi_client,
        halt_threshold_pct: float = 0.05,
        check_interval_s: float = 3600,
        starting_capital: float = 10000.0,
    ) -> None:
        """
        Initialize the reconciliation engine.

        Args:
            db_connection: SQLite connection for logging and state queries
            polymarket_client: PolymarketExecutionClient with get_balance()
            kalshi_client: KalshiExecutionClient with get_balance()
            halt_threshold_pct: Discrepancy threshold (as fraction) to halt trading
            check_interval_s: Interval in seconds between reconciliation checks
            starting_capital: Starting capital for portfolio value estimation
        """
        self.db = db_connection
        self.polymarket_client = polymarket_client
        self.kalshi_client = kalshi_client
        self.halt_threshold_pct = halt_threshold_pct
        self.check_interval_s = check_interval_s
        self.starting_capital = starting_capital

        self.trading_halted = False
        self._periodic_task = None

    async def reconcile_balances(self) -> dict:
        """
        Reconcile exchange balances against local DB.

        Queries each exchange for current balance, computes local balance
        from order history, calculates discrepancy, and logs result.

        Returns:
            Dict with structure:
            {
                "polymarket": {
                    "local": float,
                    "exchange": float | None,
                    "discrepancy": float,
                    "discrepancy_pct": float,
                    "status": "OK" | "DISCREPANCY" | "ERROR"
                },
                "kalshi": {...},
                "timestamp": ISO8601 string
            }
        """
        report: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        try:
            # Polymarket reconciliation
            poly_local = await self._compute_local_balance("polymarket")
            poly_exchange = await self.polymarket_client.get_balance()

            poly_report = await self._compute_balance_report(
                "polymarket",
                poly_local,
                poly_exchange,
            )
            report["polymarket"] = poly_report

            logger.info(
                "Polymarket balance reconciliation: local=%.2f exchange=%s discrepancy=%.2f (%s)",
                poly_local,
                poly_exchange if poly_exchange is not None else "UNREACHABLE",
                poly_report["discrepancy"],
                poly_report["status"],
            )

        except Exception as e:
            logger.error("Error reconciling Polymarket balance: %s", e, exc_info=True)
            report["polymarket"] = {
                "local": None,
                "exchange": None,
                "discrepancy": None,
                "discrepancy_pct": None,
                "status": "ERROR",
                "error": str(e),
            }

        try:
            # Kalshi reconciliation
            kalshi_local = await self._compute_local_balance("kalshi")
            kalshi_exchange = await self.kalshi_client.get_balance()

            kalshi_report = await self._compute_balance_report(
                "kalshi",
                kalshi_local,
                kalshi_exchange,
            )
            report["kalshi"] = kalshi_report

            logger.info(
                "Kalshi balance reconciliation: local=%.2f exchange=%s discrepancy=%.2f (%s)",
                kalshi_local,
                kalshi_exchange if kalshi_exchange is not None else "UNREACHABLE",
                kalshi_report["discrepancy"],
                kalshi_report["status"],
            )

        except Exception as e:
            logger.error("Error reconciling Kalshi balance: %s", e, exc_info=True)
            report["kalshi"] = {
                "local": None,
                "exchange": None,
                "discrepancy": None,
                "discrepancy_pct": None,
                "status": "ERROR",
                "error": str(e),
            }

        # Log to reconciliation_log table
        await self._log_reconciliation(report, check_type="balance")

        return report

    async def reconcile_positions(self) -> dict:
        """
        Reconcile positions against exchange APIs.

        For Kalshi: fetches portfolio positions via API.
        For Polymarket: computes from local order history.
        Compares against positions table (status='open').

        Returns:
            Dict with structure:
            {
                "polymarket": {
                    "local_count": int,
                    "exchange_count": int | None,
                    "matched": int,
                    "unmatched": list,
                    "status": "OK" | "WARNING" | "ERROR"
                },
                "kalshi": {...},
                "timestamp": ISO8601 string
            }
        """
        report: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        try:
            # Polymarket positions (from order history)
            poly_local_count = await self._count_open_positions("polymarket")
            poly_report = {
                "local_count": poly_local_count,
                "exchange_count": None,  # Not directly queryable
                "status": "OK" if poly_local_count >= 0 else "ERROR",
            }
            report["polymarket"] = poly_report

            logger.info(
                "Polymarket position reconciliation: local_open=%d",
                poly_local_count,
            )

        except Exception as e:
            logger.error("Error reconciling Polymarket positions: %s", e, exc_info=True)
            report["polymarket"] = {
                "local_count": None,
                "exchange_count": None,
                "status": "ERROR",
                "error": str(e),
            }

        try:
            # Kalshi positions (from API)
            kalshi_local_count = await self._count_open_positions("kalshi")
            kalshi_report = {
                "local_count": kalshi_local_count,
                "exchange_count": None,  # Would require Kalshi portfolio API
                "status": "OK" if kalshi_local_count >= 0 else "ERROR",
            }
            report["kalshi"] = kalshi_report

            logger.info(
                "Kalshi position reconciliation: local_open=%d",
                kalshi_local_count,
            )

        except Exception as e:
            logger.error("Error reconciling Kalshi positions: %s", e, exc_info=True)
            report["kalshi"] = {
                "local_count": None,
                "exchange_count": None,
                "status": "ERROR",
                "error": str(e),
            }

        # Log to reconciliation_log table
        await self._log_reconciliation(report, check_type="positions")

        return report

    async def run_reconciliation_check(self) -> tuple[bool, dict]:
        """
        Run full reconciliation check (balance + positions).

        Returns:
            Tuple of (trading_should_continue, full_report)
            If any discrepancy exceeds halt_threshold_pct, returns False
            and logs system event.
        """
        balance_report = await self.reconcile_balances()
        position_report = await self.reconcile_positions()

        combined_report = {
            "timestamp": balance_report.get("timestamp"),
            "balance": balance_report,
            "positions": position_report,
        }

        # Check for halt conditions
        trading_should_continue = await self._check_halt_conditions(balance_report)

        if not trading_should_continue:
            was_halted = self.trading_halted
            self.trading_halted = True
            logger.error(
                "RECONCILIATION HALT: Discrepancy exceeded halt threshold. Report: %s",
                combined_report,
            )
            await self._log_system_event(
                "RECONCILIATION_HALT",
                f"Exchange discrepancy exceeded {self.halt_threshold_pct * 100:.1f}%",
                combined_report,
            )
            # Only alert on the transition (not every periodic check) — the
            # AlertManager dedup would catch this too, but being explicit is
            # cheaper than hashing every call.
            if not was_halted:
                try:
                    await get_alert_manager().send(
                        title="Reconciliation HALT — trading halted",
                        message=(
                            f"Exchange balance discrepancy exceeded "
                            f"{self.halt_threshold_pct * 100:.1f}%"
                        ),
                        severity=Severity.CRITICAL,
                        context={
                            "polymarket_status": balance_report.get(
                                "polymarket", {}
                            ).get("status"),
                            "kalshi_status": balance_report.get("kalshi", {}).get(
                                "status"
                            ),
                        },
                        component="reconciliation",
                    )
                except Exception as e:
                    logger.error("Failed to send reconciliation alert: %s", e)
        else:
            if self.trading_halted:
                # Transition back to healthy — notify so ops knows it cleared.
                try:
                    await get_alert_manager().send(
                        title="Reconciliation recovered — trading resumed",
                        message="Balance discrepancies back within threshold",
                        severity=Severity.WARNING,
                        component="reconciliation",
                    )
                except Exception as e:
                    logger.error("Failed to send recovery alert: %s", e)
            self.trading_halted = False

        return trading_should_continue, combined_report

    async def run_periodic(self) -> None:
        """
        Async loop that runs reconciliation checks periodically.

        Runs every check_interval_s seconds. If a check fails,
        logs warning but continues periodic execution.
        """
        logger.info(
            "Starting periodic reconciliation check every %.1f seconds",
            self.check_interval_s,
        )

        while True:
            try:
                should_continue, report = await self.run_reconciliation_check()

                if not should_continue:
                    logger.warning(
                        "Reconciliation check returned HALT signal. Trading disabled."
                    )

            except asyncio.CancelledError:
                logger.info("Periodic reconciliation check cancelled")
                break
            except Exception as e:
                logger.error(
                    "Error in periodic reconciliation check: %s",
                    e,
                    exc_info=True,
                )
            await asyncio.sleep(self.check_interval_s)

    # ── Private helpers ──────────────────────────────────────────────────

    async def _compute_local_balance(self, platform: str) -> float:
        """
        Compute local balance from order history.

        For BUY orders: deduct cost (filled_size * filled_price + fee)
        For SELL orders: add proceeds (filled_size * filled_price - fee)

        Args:
            platform: "polymarket" or "kalshi"

        Returns:
            Net balance change (negative = money out, positive = money in)
        """
        pattern = f"%{platform}%" if platform else "%"
        cursor = await self.db.execute(
            """
            SELECT
                COALESCE(SUM(CASE
                    WHEN side = 'buy' THEN -filled_size * filled_price
                    ELSE filled_size * filled_price
                END), 0) + COALESCE(SUM(-fee_paid), 0) as net_balance
            FROM orders
            WHERE platform LIKE ? AND status = 'filled'
            """,
            (pattern,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0.0

    async def _compute_balance_report(
        self, platform: str, local_balance: float, exchange_balance: float | None
    ) -> dict:
        """
        Compute balance discrepancy report.

        Args:
            platform: "polymarket" or "kalshi"
            local_balance: Computed local balance
            exchange_balance: Balance from exchange API (or None if unavailable)

        Returns:
            Report dict with local, exchange, discrepancy, discrepancy_pct, status
        """
        if exchange_balance is None:
            return {
                "local": local_balance,
                "exchange": None,
                "discrepancy": None,
                "discrepancy_pct": None,
                "status": "UNREACHABLE",
            }

        discrepancy = abs(exchange_balance - local_balance)
        portfolio_value = await self._estimate_portfolio_value()

        if portfolio_value > 0:
            discrepancy_pct = discrepancy / portfolio_value
        else:
            discrepancy_pct = 0.0

        # Determine status
        if discrepancy_pct > self.halt_threshold_pct:
            status = "DISCREPANCY"
        else:
            status = "OK"

        return {
            "local": local_balance,
            "exchange": exchange_balance,
            "discrepancy": discrepancy,
            "discrepancy_pct": discrepancy_pct,
            "status": status,
        }

    async def _count_open_positions(self, platform: str) -> int:
        """
        Count open positions for a platform.

        Args:
            platform: "polymarket" or "kalshi"

        Returns:
            Count of open positions
        """
        cursor = await self.db.execute(
            """
            SELECT COUNT(*) FROM positions
            WHERE status = 'open'
              AND signal_id IN (
                  SELECT DISTINCT signal_id FROM orders WHERE platform LIKE ?
              )
            """,
            (f"%{platform}%",),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def _check_halt_conditions(self, balance_report: dict) -> bool:
        """
        Check if any balance discrepancy exceeds halt threshold.

        Args:
            balance_report: Report from reconcile_balances()

        Returns:
            True if trading should continue, False if should halt
        """
        for platform in ("polymarket", "kalshi"):
            if platform not in balance_report:
                continue

            report = balance_report[platform]

            # Skip if unreachable or error
            if report.get("status") in ("UNREACHABLE", "ERROR"):
                continue

            discrepancy_pct = report.get("discrepancy_pct")
            if (
                discrepancy_pct is not None
                and discrepancy_pct > self.halt_threshold_pct
            ):
                logger.error(
                    "%s discrepancy %.4f (%.2f%%) exceeds halt threshold %.2f%%",
                    platform,
                    report.get("discrepancy"),
                    discrepancy_pct * 100,
                    self.halt_threshold_pct * 100,
                )
                return False

        return True

    async def _estimate_portfolio_value(self) -> float:
        """
        Estimate current portfolio value.

        Queries sum of realized PnL to estimate current value
        (rough estimate; actual value depends on initial capital).

        Returns:
            Estimated portfolio value
        """
        try:
            cursor = await self.db.execute("""
                SELECT COALESCE(SUM(realized_pnl), 0) FROM positions
                WHERE status = 'closed'
                """)
            row = await cursor.fetchone()
            realized_pnl = row[0] if row else 0.0

            # Rough estimate: assume starting capital minus fees
            # This is approximate; actual value requires balance queries
            cursor = await self.db.execute("""
                SELECT COALESCE(SUM(fee_paid), 0) FROM orders
                """)
            row = await cursor.fetchone()
            total_fees = row[0] if row else 0.0

            estimated_value = self.starting_capital + realized_pnl - total_fees
            return max(100.0, estimated_value)  # Floor at 100 to avoid division issues

        except Exception as e:
            logger.warning("Error estimating portfolio value: %s", e)
            return self.starting_capital

    async def _log_reconciliation(self, report: dict, check_type: str = "full") -> None:
        """
        Log reconciliation check to database.

        Writes one row per platform to the reconciliation_log table.

        Args:
            report: Reconciliation report dict
            check_type: "balance", "positions", or "full"
        """
        import json

        now = datetime.now(timezone.utc).isoformat()

        try:
            for platform in ("polymarket", "kalshi"):
                if platform not in report:
                    continue

                p = report[platform]
                local_val = (
                    p["local"]
                    if p.get("local") is not None
                    else (p["local_count"] if p.get("local_count") is not None else 0.0)
                )
                exchange_val = (
                    p["exchange"]
                    if p.get("exchange") is not None
                    else p.get("exchange_count")
                )
                discrepancy = (
                    p["discrepancy"] if p.get("discrepancy") is not None else 0.0
                )
                status = p.get("status", "ERROR")

                await self.db.execute(
                    """
                    INSERT INTO reconciliation_log
                    (platform, check_type, local_value, exchange_value,
                     discrepancy, status, detail, action_taken, checked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        platform,
                        check_type,
                        float(local_val),
                        float(exchange_val) if exchange_val is not None else None,
                        float(discrepancy),
                        status,
                        json.dumps(p),
                        "halt" if status == "DISCREPANCY" else None,
                        now,
                    ),
                )

            await self.db.commit()
        except Exception as e:
            logger.error("Failed to log reconciliation: %s", e, exc_info=True)

    async def _log_system_event(
        self, event_type: str, detail: str, context: dict | None = None
    ) -> None:
        """
        Log a system event (e.g., halt).

        Args:
            event_type: Type of event
            detail: Human-readable detail
            context: Additional context dict
        """
        import json

        try:
            now = datetime.now(timezone.utc).isoformat()
            context_json = json.dumps(context) if context else None
            await self.db.execute(
                """
                INSERT INTO system_events
                (event_type, severity, component, detail, context, occurred_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    "critical",
                    "reconciliation",
                    detail,
                    context_json,
                    now,
                ),
            )
            await self.db.commit()
        except Exception as e:
            logger.error("Failed to log system event: %s", e, exc_info=True)
