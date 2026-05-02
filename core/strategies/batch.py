"""
Batch/legacy cross-platform trade detection.

Provides detect_violations_and_trade for the --once / --refresh batch mode,
which scans all matched pairs for spread violations and executes trades.
"""

import logging
import os
import time
import uuid
from datetime import datetime, timezone

import aiosqlite

from core.config import RiskControlConfig

# Circuit-breaker: if actual_pnl > size * this ratio the DB write is skipped.
# P1 false-positive trades book ~40% of size; 10% catches fakes without blocking
# any legitimate arb (typical real spread is 2–5%).
_PNL_SANITY_CAP_RATIO = 0.10

logger = logging.getLogger(__name__)


async def detect_violations_and_trade(
    db: aiosqlite.Connection,
    matches: list[dict],
    min_spread: float = 0.03,
) -> list[dict]:
    """
    For each matched pair, check for price discrepancies.
    If spread exceeds threshold, generate a signal and execute paper trade.
    """
    from core.signals.sizing import compute_kelly_fraction, compute_position_size
    from core.strategies.assignment import assign_strategy
    from execution.enums import Side
    from execution.factory import _make_execution_clients
    from execution.models import OrderLeg

    execution_mode = os.getenv("EXECUTION_MODE", "paper")
    paper_poly, paper_kalshi = _make_execution_clients(db, execution_mode)

    trades = []

    for match in matches:
        now = datetime.now(timezone.utc).isoformat()
        p_price = match["poly_price"]
        k_price = match["kalshi_price"]
        spread = abs(p_price - k_price)

        if spread < min_spread:
            continue

        strategy = assign_strategy(spread, "cross_platform")
        signal_id = f"sig_{uuid.uuid4().hex[:12]}"
        violation_id = f"viol_{uuid.uuid4().hex[:12]}"

        # Determine direction: buy cheap, sell expensive
        if p_price < k_price:
            buy_platform, sell_platform = "polymarket", "kalshi"
            buy_id, sell_id = match["poly_id"], match["kalshi_id"]
            buy_price, sell_price = p_price, k_price
            buy_client, sell_client = paper_poly, paper_kalshi
        else:
            buy_platform, sell_platform = "kalshi", "polymarket"
            buy_id, sell_id = match["kalshi_id"], match["poly_id"]
            buy_price, sell_price = k_price, p_price
            buy_client, sell_client = paper_kalshi, paper_poly

        edge = spread
        _rc = RiskControlConfig()
        _kelly_f = compute_kelly_fraction(edge, 1.0, _rc.kelly_fraction)
        size = round(
            compute_position_size(
                _kelly_f,
                _rc.starting_capital,
                max_size=_rc.starting_capital * _rc.max_position_pct,
            ),
            1,
        )
        if size <= 0:
            continue

        logger.info(
            "VIOLATION: spread=%.4f | %s@%.3f vs %s@%.3f | %s",
            spread,
            match["poly_title"][:40],
            p_price,
            match["kalshi_title"][:40],
            k_price,
            strategy,
        )

        # Create market pair record
        pair_id = f"{buy_id}_{sell_id}"
        try:
            await db.execute(
                """INSERT OR IGNORE INTO market_pairs
                   (id, market_id_a, market_id_b, pair_type, similarity_score,
                    match_method, active, created_at, updated_at)
                   VALUES (?, ?, ?, 'cross_platform', ?, 'inverted_index', 1, ?, ?)""",
                (pair_id, buy_id, sell_id, match.get("similarity", 0.0), now, now),
            )
        except Exception as e:
            logger.debug("Market pair insert error: %s", e)

        # Write violation (schema: id, pair_id, violation_type,
        #   price_a_at_detect, price_b_at_detect, raw_spread, net_spread,
        #   fee_estimate_a, fee_estimate_b, status, detected_at, updated_at)
        try:
            await db.execute(
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

        # Write signal (schema: id, violation_id, strategy, signal_type,
        #   market_id_a, market_id_b, model_edge, kelly_fraction,
        #   position_size_a, position_size_b, total_capital_at_risk,
        #   status, fired_at, updated_at)
        try:
            kelly = _kelly_f
            await db.execute(
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

        # Execute paper trades
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
        buy_result = await buy_client.submit_order(
            buy_leg, signal_id=signal_id, strategy=strategy
        )
        sell_result = await sell_client.submit_order(
            sell_leg, signal_id=signal_id, strategy=strategy
        )

        # Record position and trade outcome
        if buy_result.filled_price and sell_result.filled_price:
            actual_spread = sell_result.filled_price - buy_result.filled_price
            total_fees = (buy_result.fee_paid or 0) + (sell_result.fee_paid or 0)
            actual_pnl = round(actual_spread * size - total_fees, 4)

            _pnl_cap = size * _PNL_SANITY_CAP_RATIO
            if actual_pnl > _pnl_cap:
                logger.warning(
                    "PNL_SANITY_CAP blocked dual-platform arb actual_pnl=%.4f > cap=%.4f "
                    "(size=%.1f). Likely false-positive pair — skipping DB write.",
                    actual_pnl,
                    _pnl_cap,
                    size,
                )
                continue

            pos_id = f"pos_{uuid.uuid4().hex[:12]}"
            try:
                await db.execute(
                    """INSERT INTO positions
                       (id, signal_id, market_id, strategy, side, book, entry_price,
                        entry_size, exit_price, exit_size, realized_pnl, fees_paid,
                        status, opened_at, closed_at, updated_at)
                       VALUES (?, ?, ?, ?, 'BUY', 'YES', ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?)""",
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
                await db.execute(
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
                pass

            trades.append(
                {
                    "strategy": strategy,
                    "buy": f"{buy_platform}:{buy_id[:20]}",
                    "sell": f"{sell_platform}:{sell_id[:20]}",
                    "spread": spread,
                    "actual_pnl": actual_pnl,
                    "fees": total_fees,
                }
            )

            logger.info(
                "  TRADE: pnl=$%.4f fees=$%.4f | buy@%.4f sell@%.4f",
                actual_pnl,
                total_fees,
                buy_result.filled_price,
                sell_result.filled_price,
            )

            # Batch commit every 50 trades
            if len(trades) % 50 == 0:
                await db.commit()

    # Final commit for remaining trades
    await db.commit()
    return trades
