"""
Scheduled strategy runner.

Provides ScheduledStrategyRunner which runs non-latency-sensitive strategies
(calibration bias, liquidity patterns, etc.) on a fixed timer interval.
"""

import asyncio
import logging
import time

import aiosqlite

from core.config import RiskControlConfig

logger = logging.getLogger(__name__)


class ScheduledStrategyRunner:
    """Runs non-latency-sensitive strategies on a fixed interval.

    These strategies don't depend on capturing a fleeting spread —
    they analyze calibration bias, liquidity patterns, mean reversion, etc.
    Running every 60-300s is fine.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        interval: int = 120,
        max_trades_per_cycle: int = 20,
        risk_config: "RiskControlConfig | None" = None,
        circuit_breaker=None,
        execution_mode: str | None = None,
        alert_manager=None,
        price_cache: "dict | None" = None,
    ):
        self.db = db
        self.interval = interval
        self.max_trades = max_trades_per_cycle
        self.total_trades = 0
        # Live price cache shared with the websocket feed — gives P2-P5
        # strategies real-time prices without reading stale DB rows.
        self._price_cache = price_cache
        # Phase 2: risk config and circuit breaker.
        # Phase 6: when execution_mode is provided and risk_config is not, use
        # get_effective_risk_config so live mode automatically enforces tighter limits.
        if risk_config is not None:
            self._risk_config: RiskControlConfig = risk_config
        elif execution_mode is not None:
            from core.live_gate import get_effective_risk_config

            self._risk_config = get_effective_risk_config(execution_mode)
        else:
            self._risk_config = RiskControlConfig()
        self._circuit_breaker = circuit_breaker
        # Phase 7: alert_manager forwards invariant violations to Discord.
        self._alert_manager = alert_manager

    async def run_one_cycle(self) -> list:
        """Execute a single strategy cycle. Returns list of opened positions.

        Circuit breaker check happens first — if halted, returns [] immediately.
        After opening new positions, closes any that have exceeded holding_period_s
        (Phase 4 realistic fill model).
        Extracted from run() so it can be called and tested independently.
        """
        from core.strategies.single_platform import (
            detect_single_platform_opportunities,
            mark_and_close_positions,
        )

        if self._circuit_breaker is not None:
            if await self._circuit_breaker.should_halt():
                logger.warning(
                    "CIRCUIT_BREAKER halted — skipping scheduled strategy cycle"
                )
                return []
        # Mark-to-market pass: close expired open positions at current prices
        await mark_and_close_positions(
            self.db,
            holding_period_s=self._risk_config.strategy_holding_period_s,
            price_cache=self._price_cache,
        )
        # Phase 7: run invariant checks before opening new positions.
        # alert_manager forwards violations to Discord when configured.
        from core.invariants import check_all_invariants

        await check_all_invariants(
            self.db, mode="warn", alert_manager=self._alert_manager
        )
        return await detect_single_platform_opportunities(
            self.db,
            max_trades=self.max_trades,
            risk_config=self._risk_config,
            price_cache=self._price_cache,
        )

    async def run(self, stop_event: asyncio.Event):
        """Run strategy cycles until stop_event is set."""
        logger.info(
            "ScheduledStrategyRunner started: interval=%ds, max_trades=%d",
            self.interval,
            self.max_trades,
        )
        while not stop_event.is_set():
            try:
                t0 = time.time()
                trades = await self.run_one_cycle()
                self.total_trades += len(trades)
                elapsed = time.time() - t0
                logger.info(
                    "Scheduled strategies: %d trades in %.1fs (total: %d)",
                    len(trades),
                    elapsed,
                    self.total_trades,
                )
            except Exception as e:
                logger.error("Scheduled strategy error: %s", e)

            # Wait for interval or stop
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval)
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # interval elapsed, run again
