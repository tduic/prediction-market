"""
Event-driven arbitrage engine.

Provides ArbitrageEngine which monitors matched pairs and executes
cross-platform arb trades when websocket price updates reveal spread violations.
"""

import asyncio
import logging
import os
import random
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import aiosqlite

from core.config import RiskControlConfig, get_config
from core.engine.fire_state import PairFireState, _RiskLeg, _RiskSignal
from execution.clients.base import BaseExecutionClient, OrderResult
from execution.enums import Side
from execution.models import OrderLeg


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
        # Count of fires suppressed because one side's cached price was
        # older than risk_config.max_price_age_s. Surfaced in stats().
        self._skipped_stale: int = 0

        # Build pair indexes for O(1) lookup on price update
        # poly_id -> list of (kalshi_id, match_dict)
        # kalshi_id -> list of (poly_id, match_dict)
        self._poly_to_pairs: dict[str, list[dict]] = defaultdict(list)
        self._kalshi_to_pairs: dict[str, list[dict]] = defaultdict(list)
        self._pairs: dict[str, dict] = {}  # pair_id -> match

        # Treat seeded prices as fresh at startup so initial_sweep has a
        # window to fire. Once max_price_age_s elapses without a real WS
        # tick, the staleness guard kicks in. Use a single timestamp across
        # all seeds so the window is uniform.
        seed_ts = time.time()
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
                self._last_tick_at[m["poly_id"]] = seed_ts
            if m.get("kalshi_price"):
                self.prices[m["kalshi_id"]] = m["kalshi_price"]
                self._last_tick_at[m["kalshi_id"]] = seed_ts

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

    def update_pairs(self, matches: list[dict]) -> dict:
        """Replace the pair index with ``matches``. Safe to call while running.

        Retains ``fired_state`` and live ``prices`` for markets still present.
        Adds new pairs, drops pairs no longer in the match set, and seeds
        prices for newly-added markets from the match data only when we
        don't already have a live price from the websocket feed.

        Flags the next ``initial_sweep()`` call to fire so the caller can
        pick up any new pairs that are already above the spread threshold
        at refresh time.

        Returns counts: ``{"added": N, "removed": N, "retained": N}``.
        """
        new_pair_ids = {f"{m['poly_id']}_{m['kalshi_id']}" for m in matches}
        old_pair_ids = set(self._pairs)
        added = new_pair_ids - old_pair_ids
        removed = old_pair_ids - new_pair_ids
        retained = new_pair_ids & old_pair_ids

        # Rebuild indexes from scratch. At weekly refresh scale this is
        # cheap and avoids subtle drift between the forward and reverse maps.
        self._pairs = {}
        self._poly_to_pairs = defaultdict(list)
        self._kalshi_to_pairs = defaultdict(list)

        # Single seed timestamp so newly-added markets share a uniform
        # freshness window after a refresh.
        seed_ts = time.time()
        for m in matches:
            pair_id = f"{m['poly_id']}_{m['kalshi_id']}"
            self._pairs[pair_id] = m
            self._poly_to_pairs[m["poly_id"]].append(m)
            self._kalshi_to_pairs[m["kalshi_id"]].append(m)
            self._market_platform.setdefault(m["poly_id"], "polymarket")
            self._market_platform.setdefault(m["kalshi_id"], "kalshi")
            # Seed prices for markets we've never seen a tick for. Don't
            # clobber live prices — match data is stale compared to the WS feed.
            if m.get("poly_price") and m["poly_id"] not in self.prices:
                self.prices[m["poly_id"]] = m["poly_price"]
                self._last_tick_at.setdefault(m["poly_id"], seed_ts)
            if m.get("kalshi_price") and m["kalshi_id"] not in self.prices:
                self.prices[m["kalshi_id"]] = m["kalshi_price"]
                self._last_tick_at.setdefault(m["kalshi_id"], seed_ts)

        for pair_id in removed:
            self.fired_state.pop(pair_id, None)

        # Prune per-market state for markets no longer referenced by any pair.
        # Without this, _market_platform / _last_tick_at / prices grew by ~2
        # entries per removed pair on every weekly refresh — a slow leak that
        # accumulates over the lifetime of a long-running process.
        live_market_ids: set[str] = set()
        for m in self._pairs.values():
            live_market_ids.add(m["poly_id"])
            live_market_ids.add(m["kalshi_id"])

        stale_market_ids = (
            set(self._market_platform) | set(self._last_tick_at) | set(self.prices)
        ) - live_market_ids
        for mid in stale_market_ids:
            self._market_platform.pop(mid, None)
            self._last_tick_at.pop(mid, None)
            self.prices.pop(mid, None)

        if added:
            self._needs_initial_sweep = True

        logger.info(
            "ArbitrageEngine.update_pairs: added=%d removed=%d retained=%d "
            "total=%d pruned_markets=%d",
            len(added),
            len(removed),
            len(retained),
            len(self._pairs),
            len(stale_market_ids),
        )
        return {
            "added": len(added),
            "removed": len(removed),
            "retained": len(retained),
        }

    def _is_fresh(self, market_id: str, now: float | None = None) -> bool:
        """Return True if ``market_id``'s cached price was updated within
        ``risk_config.max_price_age_s`` seconds.

        This is the staleness guard that prevents firing on cached prices
        the live market has already drifted past. Missing tick times are
        treated as stale (the guard's job is to be conservative).
        """
        t = self._last_tick_at.get(market_id)
        if t is None:
            return False
        if now is None:
            now = time.time()
        return (now - t) <= self._risk_config.max_price_age_s

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

    async def _try_fire_pair(self, match: dict, pair_id: str, now: float) -> bool:
        """Attempt to fire an arb trade for one pair at timestamp ``now``.

        Performs the full eligibility + staleness + lock + execute cycle.
        Returns True if a trade was recorded, False otherwise (below threshold,
        ineligible, stale, risk-rejected, or execution error).

        fired_state is updated only on a successful trade so that risk/CB
        rejections and exceptions leave the pair retriable (Phase 2 contract).
        """
        p_price = self.prices.get(match["poly_id"])
        k_price = self.prices.get(match["kalshi_id"])
        if p_price is None or k_price is None:
            return False

        spread = abs(p_price - k_price)
        # Re-arm check runs unconditionally so spread reversions are captured
        # even when we ultimately don't fire (spread below threshold).
        self._check_rearm(pair_id, spread)

        if spread < self.min_spread:
            return False

        if not self._is_eligible(pair_id):
            return False

        # Staleness guard: firing on a cached price the market has drifted
        # past produces guaranteed-reject limit orders on the exchange.
        if not (
            self._is_fresh(match["poly_id"], now)
            and self._is_fresh(match["kalshi_id"], now)
        ):
            self._skipped_stale += 1
            return False

        # Execute under lock to prevent concurrent trades on the same pair.
        async with self._trade_lock:
            # Re-check under lock — state may have changed while we waited.
            self._check_rearm(pair_id, spread)
            if not self._is_eligible(pair_id):
                return False
            try:
                trade = await self._execute_arb_trade(
                    match, p_price, k_price, spread, pair_id
                )
            except Exception:
                logger.exception(
                    "Unhandled exception in _try_fire_pair for pair %s", pair_id
                )
                return False
            if trade:
                self.fired_state[pair_id] = PairFireState(
                    last_fired_at=time.time(), armed=False
                )
                self.trades.append(trade)
                self.last_arb_fired_at = time.time()
                self._ticks_since_last_fire = 0
                return True
        return False

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
        now = time.time()
        for match in self._pairs.values():
            pair_id = f"{match['poly_id']}_{match['kalshi_id']}"
            if await self._try_fire_pair(match, pair_id, now):
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

        now = time.time()
        for match in affected:
            pair_id = f"{match['poly_id']}_{match['kalshi_id']}"
            await self._try_fire_pair(match, pair_id, now)

    async def periodic_scan(self) -> None:
        """Scan all tracked pairs for arbitrage opportunities.

        Called on a timer, independent of price-tick events. Catches pairs
        whose spread opened while both prices drifted simultaneously (no single
        tick would have triggered on_price_update for the pair).
        """
        now = time.time()
        for match in self._pairs.values():
            pair_id = f"{match['poly_id']}_{match['kalshi_id']}"
            await self._try_fire_pair(match, pair_id, now)

    async def _submit_with_retry(
        self,
        client: BaseExecutionClient,
        leg: OrderLeg,
        signal_id: str | None,
        strategy: str | None,
    ) -> OrderResult:
        """Submit an order with exponential backoff on transient failures.

        Retries up to `execution.max_order_retries` attempts when the venue
        returns a non-filled status, with delay `retry_backoff_base_s * 2**n`
        plus jitter between attempts. Each retry opens a new venue order
        (submit_order is not idempotent), which is safe because the prior
        attempt reported no fill. Every attempt's orders row is written by
        the client; an `order_events` row is appended here to record the
        retry reason so post-mortems can trace the sequence.

        Returns the last OrderResult (filled if any attempt fills, otherwise
        the final failure).
        """
        cfg = get_config().execution
        max_attempts = max(1, cfg.max_order_retries)
        base_s = cfg.retry_backoff_base_s

        last_result: OrderResult | None = None
        for attempt in range(1, max_attempts + 1):
            result = await client.submit_order(
                leg, signal_id=signal_id, strategy=strategy
            )
            last_result = result
            # Accept any partial or full fill; only retry on clean failure.
            if result.status in ("filled", "partially_filled"):
                if attempt > 1:
                    logger.info(
                        "ORDER_RETRY_SUCCESS order=%s market=%s attempt=%d/%d",
                        result.order_id,
                        leg.market_id,
                        attempt,
                        max_attempts,
                    )
                return result

            if attempt < max_attempts:
                delay = base_s * (2 ** (attempt - 1)) * (1 + random.random() * 0.25)
                logger.warning(
                    "ORDER_RETRY order=%s market=%s attempt=%d/%d status=%s err=%r "
                    "next_delay=%.2fs",
                    result.order_id,
                    leg.market_id,
                    attempt,
                    max_attempts,
                    result.status,
                    result.error_message,
                    delay,
                )
                try:
                    await self.db.execute(
                        """INSERT INTO order_events
                           (order_id, event_type, detail, occurred_at)
                           VALUES (?, 'retry', ?, ?)""",
                        (
                            result.order_id,
                            f"attempt={attempt} status={result.status} "
                            f"err={result.error_message or ''}",
                            int(time.time()),
                        ),
                    )
                except Exception:
                    logger.debug("order_events retry log failed", exc_info=True)
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "ORDER_RETRY_EXHAUSTED order=%s market=%s attempts=%d status=%s err=%r",
                    result.order_id,
                    leg.market_id,
                    max_attempts,
                    result.status,
                    result.error_message,
                )

        assert last_result is not None  # max_attempts >= 1
        return last_result

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

        # Insert market_pair + violation BEFORE risk checks so:
        #   (a) risk_check_log.violation_id FK is satisfied when we log
        #       (PRAGMA foreign_keys=ON enforces this at INSERT time), and
        #   (b) rejected trades still leave a violations row for analytics.
        # An exception here (not a duplicate — INSERT OR IGNORE swallows those
        # silently with rowcount=0) indicates a schema/FK/lock error, and we
        # must abort before sending orders.
        try:
            await self.db.execute(
                """INSERT OR IGNORE INTO market_pairs
                   (id, market_id_a, market_id_b, pair_type, similarity_score,
                    match_method, active, created_at, updated_at)
                   VALUES (?, ?, ?, 'cross_platform', ?, 'inverted_index', 1, ?, ?)""",
                (pair_id, buy_id, sell_id, match.get("similarity", 0.0), now, now),
            )
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
            await self.db.commit()
        except Exception:
            logger.exception(
                "Aborting arb trade for pair=%s: market_pair/violation insert failed",
                pair_id,
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
            violation_id=violation_id,
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

        try:
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
                    kelly_f,
                    size,
                    size,
                    size * 2,
                    now,
                    now,
                ),
            )
            await self.db.commit()
        except Exception:
            logger.exception(
                "Aborting arb trade for pair=%s: signal insert failed", pair_id
            )
            return None

        # Execute both legs
        buy_leg = OrderLeg(
            market_id=buy_id,
            platform=buy_platform,
            side=Side.BUY,
            size=size,
            limit_price=buy_price,
            order_type="LIMIT",
        )
        sell_leg = OrderLeg(
            market_id=sell_id,
            platform=sell_platform,
            side=Side.SELL,
            size=size,
            limit_price=sell_price,
            order_type="LIMIT",
        )
        _trade_start_ms = int(time.time() * 1000)
        buy_result = await self._submit_with_retry(
            buy_client, buy_leg, signal_id=signal_id, strategy=strategy
        )
        sell_result = await self._submit_with_retry(
            sell_client, sell_leg, signal_id=signal_id, strategy=strategy
        )

        # Flag unbalanced fills so reconciliation/close-out can pick them up.
        buy_filled = buy_result.filled_price is not None
        sell_filled = sell_result.filled_price is not None
        if buy_filled != sell_filled:
            logger.error(
                "UNBALANCED_ARB pair=%s buy_filled=%s sell_filled=%s — "
                "one leg open without hedge. Reconciliation will flag this.",
                pair_id,
                buy_filled,
                sell_filled,
            )

        if self._circuit_breaker is not None:
            await self._circuit_breaker.record_order_result(
                success=buy_filled and sell_filled
            )

        if buy_result.filled_price and sell_result.filled_price:
            actual_spread = sell_result.filled_price - buy_result.filled_price
            total_fees = (buy_result.fee_paid or 0) + (sell_result.fee_paid or 0)
            actual_pnl = round(actual_spread * size - total_fees, 4)

            _pnl_cap = size * self._risk_config.pnl_sanity_cap_ratio
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
                       (id, signal_id, market_id, strategy, side, book, entry_price,
                        entry_size, exit_price, exit_size, realized_pnl, fees_paid,
                        status, opened_at, closed_at, updated_at)
                       VALUES (?, ?, ?, ?, 'BUY', 'YES', ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?)""",
                    # TODO[no-naked-shorts]: when the translated-NO path becomes live
                    # for arbs, propagate the resolved book here instead of 'YES'.
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
                logger.exception(
                    "Failed to insert positions row for pair=%s signal_id=%s pos_id=%s",
                    pair_id,
                    signal_id,
                    pos_id,
                )

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
                        max(1, int(time.time() * 1000) - _trade_start_ms),
                        now,
                        now,
                    ),
                )
            except Exception:
                logger.exception(
                    "Failed to insert trade_outcomes row for pair=%s signal_id=%s",
                    pair_id,
                    signal_id,
                )

            # Commit per trade: batching delayed persistence by up to 9 trades,
            # so a process crash between flushes could drop filled positions
            # that already moved real capital on the exchange. Reconciliation
            # can't repair what it can't see. SQLite in WAL mode handles
            # single-row commits cheaply, so the throughput cost is negligible.
            try:
                await self.db.commit()
            except Exception:
                logger.exception(
                    "Failed to commit positions/trade_outcomes for pair=%s signal_id=%s",
                    pair_id,
                    signal_id,
                )

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
        """Commit any pending DB writes.

        No-op under the current per-trade-commit model; retained so callers
        (trading_session shutdown, tests) can keep their "drain before exit"
        semantics without caring how persistence is scheduled internally.
        """
        await self.db.commit()

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
            "skipped_stale": self._skipped_stale,
        }
