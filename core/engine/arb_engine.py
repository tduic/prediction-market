"""
Event-driven arbitrage engine.

Provides ArbitrageEngine which monitors matched pairs and executes
cross-platform arb trades when websocket price updates reveal spread violations.
"""

import asyncio
import logging
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import aiosqlite

from core.config import RiskControlConfig
from core.engine.fire_state import PairFireState, _RiskLeg, _RiskSignal

# Circuit-breaker: if actual_pnl > size * this ratio the DB write is skipped.
# P1 false-positive trades book ~40% of size; 10% catches fakes without blocking
# any legitimate arb (typical real spread is 2–5%).
_PNL_SANITY_CAP_RATIO = 0.10

logger = logging.getLogger(__name__)


class ArbitrageEngine:
    """Event-driven cross-platform arbitrage.

    Holds an in-memory index of matched pairs and their latest prices.
    When a websocket price update arrives, `on_price_update()` is called
    synchronously to check if any spread now exceeds the threshold.
    If so, the trade is executed immediately — no polling loop.

    This keeps arb latency bounded by websocket delivery + trade execution,
    not by a sleep-based cycle interval.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        matches: list[dict],
        min_spread: float = 0.03,
        risk_config: "RiskControlConfig | None" = None,
        circuit_breaker=None,
        execution_mode: str | None = None,
    ):
        self.db = db
        self.min_spread = min_spread
        self.trades: list[dict] = []
        self._trade_lock = asyncio.Lock()
        self._pending_commit = 0

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

        # Live price cache: market_id -> price
        self.prices: dict[str, float] = {}

        # fired_state: per-pair cooldown and re-arm state (Phase 3).
        # Replaced the bare recently_fired set to support cooldown + hysteresis re-arm.
        self.fired_state: dict[str, PairFireState] = {}

        # Telemetry fields surfaced in stats() and the STATUS log line.
        self.last_arb_fired_at: float | None = None
        self._ticks_since_last_fire: int = 0
        self._last_tick_at: dict[str, float] = {}  # market_id -> time.time()
        self._market_platform: dict[str, str] = {}  # market_id -> "polymarket"/"kalshi"

        # Build pair indexes for O(1) lookup on price update
        # poly_id -> list of (kalshi_id, match_dict)
        # kalshi_id -> list of (poly_id, match_dict)
        self._poly_to_pairs: dict[str, list[dict]] = defaultdict(list)
        self._kalshi_to_pairs: dict[str, list[dict]] = defaultdict(list)
        self._pairs: dict[str, dict] = {}  # pair_id -> match

        for m in matches:
            pair_id = f"{m['poly_id']}_{m['kalshi_id']}"
            self._pairs[pair_id] = m
            self._poly_to_pairs[m["poly_id"]].append(m)
            self._kalshi_to_pairs[m["kalshi_id"]].append(m)
            self._market_platform[m["poly_id"]] = "polymarket"
            self._market_platform[m["kalshi_id"]] = "kalshi"
            # Seed prices from match data
            if m.get("poly_price"):
                self.prices[m["poly_id"]] = m["poly_price"]
            if m.get("kalshi_price"):
                self.prices[m["kalshi_id"]] = m["kalshi_price"]

        # Execution clients (live or paper depending on EXECUTION_MODE)
        execution_mode = os.getenv("EXECUTION_MODE", "paper")
        from execution.factory import _make_execution_clients

        self._poly_client, self._kalshi_client = _make_execution_clients(
            db, execution_mode
        )

        # Track pairs that need an initial sweep (prices seeded from match data)
        self._needs_initial_sweep = True

        logger.info(
            "ArbitrageEngine initialized: %d pairs, min_spread=%.4f",
            len(self._pairs),
            min_spread,
        )

    @property
    def recently_fired(self) -> set[str]:
        """Backward-compatible view of all pairs that have ever fired."""
        return set(self.fired_state.keys())

    def _is_eligible(self, pair_id: str) -> bool:
        """Return True if pair_id may fire (never fired, armed, and cooldown elapsed)."""
        state = self.fired_state.get(pair_id)
        if state is None:
            return True  # Never fired → always eligible
        if not state.armed:
            return False  # Waiting for spread reversion
        return (time.time() - state.last_fired_at) >= self._risk_config.arb_cooldown_s

    def _check_rearm(self, pair_id: str, spread: float) -> None:
        """Re-arm pair if spread has reverted below the hysteresis threshold."""
        state = self.fired_state.get(pair_id)
        if state is None or state.armed:
            return
        threshold = self.min_spread - self._risk_config.arb_rearm_hysteresis
        if spread < threshold:
            state.armed = True

    async def initial_sweep(self) -> None:
        """Check all seeded pairs for opportunities at startup.

        Prices are seeded from match data before any websocket tick arrives.
        Without this sweep, pairs already above the spread threshold at launch
        are invisible until a price delta triggers on_price_update.
        """
        if not self._needs_initial_sweep:
            return
        self._needs_initial_sweep = False

        swept = 0
        for match in self._pairs.values():
            pair_id = f"{match['poly_id']}_{match['kalshi_id']}"
            p_price = self.prices.get(match["poly_id"])
            k_price = self.prices.get(match["kalshi_id"])
            if p_price is None or k_price is None:
                continue
            spread = abs(p_price - k_price)
            self._check_rearm(pair_id, spread)
            if spread < self.min_spread:
                continue
            if not self._is_eligible(pair_id):
                continue
            async with self._trade_lock:
                self._check_rearm(pair_id, spread)
                if not self._is_eligible(pair_id):
                    continue
                try:
                    trade = await self._execute_arb_trade(
                        match, p_price, k_price, spread, pair_id
                    )
                except Exception:
                    logger.exception(
                        "Unhandled exception in initial_sweep for pair %s", pair_id
                    )
                    continue
                if trade:
                    self.fired_state[pair_id] = PairFireState(
                        last_fired_at=time.time(), armed=False
                    )
                    self.trades.append(trade)
                    self.last_arb_fired_at = time.time()
                    self._ticks_since_last_fire = 0
                    swept += 1

        if swept:
            logger.info("Initial sweep found %d arb opportunities", swept)

    async def on_price_update(self, market_id: str, new_price: float):
        """Called by websocket handlers on every price tick.

        Checks all pairs involving this market_id. If any spread
        exceeds threshold and we don't have an open position, trade.
        """
        old_price = self.prices.get(market_id)
        self.prices[market_id] = new_price
        self._last_tick_at[market_id] = time.time()

        # Skip if price didn't change meaningfully
        if old_price is not None and abs(new_price - old_price) < 0.001:
            return

        self._ticks_since_last_fire += 1

        # Find all pairs involving this market
        affected = []
        if market_id in self._poly_to_pairs:
            affected.extend(self._poly_to_pairs[market_id])
        if market_id in self._kalshi_to_pairs:
            affected.extend(self._kalshi_to_pairs[market_id])

        for match in affected:
            pair_id = f"{match['poly_id']}_{match['kalshi_id']}"

            p_price = self.prices.get(match["poly_id"])
            k_price = self.prices.get(match["kalshi_id"])
            if p_price is None or k_price is None:
                continue

            spread = abs(p_price - k_price)
            # Always run re-arm check (spread may have reverted below threshold)
            self._check_rearm(pair_id, spread)

            if spread < self.min_spread:
                continue

            if not self._is_eligible(pair_id):
                continue

            # Execute immediately under lock (prevent concurrent trades on same pair)
            async with self._trade_lock:
                # Re-check under lock (state may have changed)
                self._check_rearm(pair_id, spread)
                if not self._is_eligible(pair_id):
                    continue
                # fired_state updated only on success; risk/CB rejections leave pair
                # retriable (Phase 2 contract). Exceptions also leave pair unlocked.
                try:
                    trade = await self._execute_arb_trade(
                        match, p_price, k_price, spread, pair_id
                    )
                except Exception:
                    logger.exception(
                        "Unhandled exception in on_price_update for pair %s", pair_id
                    )
                    continue
                if trade:
                    self.fired_state[pair_id] = PairFireState(
                        last_fired_at=time.time(), armed=False
                    )
                    self.trades.append(trade)
                    self.last_arb_fired_at = time.time()
                    self._ticks_since_last_fire = 0

    async def periodic_scan(self) -> None:
        """Scan all tracked pairs for arbitrage opportunities.

        Called on a timer, independent of price-tick events. Catches pairs
        whose spread opened while both prices drifted simultaneously (no single
        tick would have triggered on_price_update for the pair).
        """
        for match in self._pairs.values():
            pair_id = f"{match['poly_id']}_{match['kalshi_id']}"
            p_price = self.prices.get(match["poly_id"])
            k_price = self.prices.get(match["kalshi_id"])
            if p_price is None or k_price is None:
                continue
            spread = abs(p_price - k_price)
            self._check_rearm(pair_id, spread)
            if spread < self.min_spread:
                continue
            if not self._is_eligible(pair_id):
                continue
            async with self._trade_lock:
                self._check_rearm(pair_id, spread)
                if not self._is_eligible(pair_id):
                    continue
                try:
                    trade = await self._execute_arb_trade(
                        match, p_price, k_price, spread, pair_id
                    )
                except Exception:
                    logger.exception(
                        "Unhandled exception in periodic_scan for pair %s", pair_id
                    )
                    continue
                if trade:
                    self.fired_state[pair_id] = PairFireState(
                        last_fired_at=time.time(), armed=False
                    )
                    self.trades.append(trade)
                    self.last_arb_fired_at = time.time()
                    self._ticks_since_last_fire = 0

    async def _execute_arb_trade(
        self,
        match: dict,
        p_price: float,
        k_price: float,
        spread: float,
        pair_id: str,
    ) -> dict | None:
        """Execute a single arbitrage trade on a matched pair."""
        from core.signals.risk import run_all_checks
        from core.signals.sizing import compute_kelly_fraction, compute_position_size
        from execution.models import OrderLeg

        now = datetime.now(timezone.utc).isoformat()
        strategy = "P1_cross_market_arb"
        signal_id = f"sig_{uuid.uuid4().hex[:12]}"
        violation_id = f"viol_{uuid.uuid4().hex[:12]}"

        if p_price < k_price:
            buy_platform, sell_platform = "polymarket", "kalshi"
            buy_id, sell_id = match["poly_id"], match["kalshi_id"]
            buy_price, sell_price = p_price, k_price
            buy_client, sell_client = self._poly_client, self._kalshi_client
        else:
            buy_platform, sell_platform = "kalshi", "polymarket"
            buy_id, sell_id = match["kalshi_id"], match["poly_id"]
            buy_price, sell_price = k_price, p_price
            buy_client, sell_client = self._kalshi_client, self._poly_client

        # Phase 2.4: circuit breaker halt check.
        if self._circuit_breaker is not None:
            if await self._circuit_breaker.should_halt():
                logger.warning(
                    "CIRCUIT_BREAKER halted — skipping arb trade on pair=%s", pair_id
                )
                return None

        edge = spread

        # Phase 2.3: Kelly-based position sizing (replaces hardcoded min(10, 100*edge)).
        kelly_f = compute_kelly_fraction(edge, 1.0, self._risk_config.kelly_fraction)
        bankroll = self._risk_config.starting_capital
        max_size = bankroll * self._risk_config.max_position_pct
        size = round(compute_position_size(kelly_f, bankroll, max_size=max_size), 1)
        if size <= 0:
            logger.debug(
                "Kelly sizing produced zero size for edge=%.4f — skipping", edge
            )
            return None

        # Phase 2.2: run all risk checks before executing orders.
        risk_signal = _RiskSignal(
            legs=[
                _RiskLeg(
                    market_id=buy_id, limit_price=buy_price, size=size, side="BUY"
                ),
                _RiskLeg(
                    market_id=sell_id, limit_price=sell_price, size=size, side="SELL"
                ),
            ],
            edge=edge,
            strategy=strategy,
            signal_id=signal_id,
        )
        all_passed, check_results = await run_all_checks(
            risk_signal, self._risk_config, self.db
        )
        if not all_passed:
            failed = [r.check_type for r in check_results if not r.passed]
            logger.info(
                "RISK_REJECTED pair=%s failed_checks=%s — not adding to recently_fired",
                pair_id,
                failed,
            )
            return None

        logger.info(
            "ARB TRADE: spread=%.4f | %s@%.3f vs %s@%.3f",
            spread,
            match["poly_title"][:40],
            p_price,
            match["kalshi_title"][:40],
            k_price,
        )

        # Write market_pair, violation, signal
        try:
            await self.db.execute(
                """INSERT OR IGNORE INTO market_pairs
                   (id, market_id_a, market_id_b, pair_type, similarity_score,
                    match_method, active, created_at, updated_at)
                   VALUES (?, ?, ?, 'cross_platform', ?, 'inverted_index', 1, ?, ?)""",
                (pair_id, buy_id, sell_id, match.get("similarity", 0.0), now, now),
            )
        except Exception as e:
            logger.debug("Market pair insert error: %s", e)

        try:
            await self.db.execute(
                """INSERT OR IGNORE INTO violations
                   (id, pair_id, violation_type, price_a_at_detect, price_b_at_detect,
                    raw_spread, net_spread, fee_estimate_a, fee_estimate_b,
                    status, detected_at, updated_at)
                   VALUES (?, ?, 'cross_platform', ?, ?, ?, ?, ?, ?, 'detected', ?, ?)""",
                (
                    violation_id,
                    pair_id,
                    buy_price,
                    sell_price,
                    spread,
                    spread - 0.02,
                    buy_price * 0.02,
                    sell_price * 0.02,
                    now,
                    now,
                ),
            )
        except Exception as e:
            logger.debug("Violation insert error: %s", e)

        try:
            kelly = min(edge / 0.50, 0.25)
            await self.db.execute(
                """INSERT OR IGNORE INTO signals
                   (id, violation_id, strategy, signal_type, market_id_a, market_id_b,
                    model_edge, kelly_fraction, position_size_a, position_size_b,
                    total_capital_at_risk, status, fired_at, updated_at)
                   VALUES (?, ?, ?, 'arb_pair', ?, ?, ?, ?, ?, ?, ?, 'fired', ?, ?)""",
                (
                    signal_id,
                    violation_id,
                    strategy,
                    buy_id,
                    sell_id,
                    edge,
                    kelly,
                    size,
                    size,
                    size * 2,
                    now,
                    now,
                ),
            )
        except Exception as e:
            logger.debug("Signal insert error: %s", e)

        # Execute both legs
        buy_leg = OrderLeg(
            market_id=buy_id,
            platform=buy_platform,
            side="BUY",
            size=size,
            limit_price=buy_price,
            order_type="LIMIT",
        )
        sell_leg = OrderLeg(
            market_id=sell_id,
            platform=sell_platform,
            side="SELL",
            size=size,
            limit_price=sell_price,
            order_type="LIMIT",
        )
        buy_result = await buy_client.submit_order(
            buy_leg, signal_id=signal_id, strategy=strategy
        )
        sell_result = await sell_client.submit_order(
            sell_leg, signal_id=signal_id, strategy=strategy
        )

        if buy_result.filled_price and sell_result.filled_price:
            actual_spread = sell_result.filled_price - buy_result.filled_price
            total_fees = (buy_result.fee_paid or 0) + (sell_result.fee_paid or 0)
            actual_pnl = round(actual_spread * size - total_fees, 4)

            _pnl_cap = size * _PNL_SANITY_CAP_RATIO
            if actual_pnl > _pnl_cap:
                logger.warning(
                    "PNL_SANITY_CAP blocked pair=%s actual_pnl=%.4f > cap=%.4f "
                    "(size=%.1f spread=%.4f). Likely false-positive pair — skipping DB write.",
                    pair_id,
                    actual_pnl,
                    _pnl_cap,
                    size,
                    spread,
                )
                return None

            pos_id = f"pos_{uuid.uuid4().hex[:12]}"
            try:
                await self.db.execute(
                    """INSERT INTO positions
                       (id, signal_id, market_id, strategy, side, entry_price,
                        entry_size, exit_price, exit_size, realized_pnl, fees_paid,
                        status, opened_at, closed_at, updated_at)
                       VALUES (?, ?, ?, ?, 'BUY', ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?)""",
                    (
                        pos_id,
                        signal_id,
                        buy_id,
                        strategy,
                        buy_result.filled_price,
                        size,
                        sell_result.filled_price,
                        size,
                        actual_pnl,
                        total_fees,
                        now,
                        now,
                        now,
                    ),
                )
            except Exception:
                pass

            try:
                await self.db.execute(
                    """INSERT INTO trade_outcomes
                       (id, signal_id, strategy, violation_id, market_id_a, market_id_b,
                        predicted_edge, predicted_pnl, actual_pnl, fees_total,
                        edge_captured_pct, signal_to_fill_ms, holding_period_ms,
                        resolved_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f"trade_{uuid.uuid4().hex[:12]}",
                        signal_id,
                        strategy,
                        violation_id,
                        buy_id,
                        sell_id,
                        edge,
                        round(edge * size, 4),
                        actual_pnl,
                        total_fees,
                        (
                            round((actual_pnl / (edge * size)) * 100, 1)
                            if edge * size > 0
                            else 0
                        ),
                        buy_result.submission_latency_ms
                        + (buy_result.fill_latency_ms or 0),
                        5000,
                        now,
                        now,
                    ),
                )
            except Exception:
                pass

            # Batch commit
            self._pending_commit += 1
            if self._pending_commit >= 10:
                await self.db.commit()
                self._pending_commit = 0

            logger.info(
                "  ARB FILLED: pnl=$%.4f fees=$%.4f | buy@%.4f sell@%.4f",
                actual_pnl,
                total_fees,
                buy_result.filled_price,
                sell_result.filled_price,
            )

            return {
                "strategy": strategy,
                "pair_id": pair_id,
                "spread": spread,
                "actual_pnl": actual_pnl,
                "fees": total_fees,
            }
        return None

    async def flush(self):
        """Commit any pending DB writes."""
        if self._pending_commit > 0:
            await self.db.commit()
            self._pending_commit = 0

    def stats(self) -> dict:
        total_pnl = sum(t.get("actual_pnl", 0) for t in self.trades)
        eligible = sum(
            1
            for m in self._pairs.values()
            if (p := self.prices.get(m["poly_id"])) is not None
            and (k := self.prices.get(m["kalshi_id"])) is not None
            and abs(p - k) >= self.min_spread
        )
        now = time.time()
        tick_age: dict[str, int] = {}
        for market_id, ts in self._last_tick_at.items():
            platform = self._market_platform.get(market_id, "unknown")
            age_ms = int((now - ts) * 1000)
            # Keep the freshest (smallest age) tick per platform
            if platform not in tick_age or tick_age[platform] > age_ms:
                tick_age[platform] = age_ms
        return {
            "pairs_monitored": len(self._pairs),
            "pairs_eligible_now": eligible,
            "recently_fired": len(self.recently_fired),
            "last_arb_fired_at": self.last_arb_fired_at,
            "ticks_since_last_fire": self._ticks_since_last_fire,
            "total_pnl": total_pnl,
            "prices_tracked": len(self.prices),
            "ws_last_tick_age_ms_by_platform": tick_age,
        }
