"""
Single-platform trading strategies.

Provides detect_single_platform_opportunities, mark_and_close_positions,
and related helper functions for P2-P5 strategies that operate on individual
markets without needing a cross-platform match.
"""

import logging
import os
import re
import statistics
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import aiosqlite

logger = logging.getLogger(__name__)

# Month/date suffix pattern for _p2_title_root
_MONTH_PAT = (
    r"(january|february|march|april|may|june|july|august|september|"
    r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)"
)
_STRIP_SUFFIX = re.compile(
    rf"\s+(?:{_MONTH_PAT}|20\d{{2}}|q[1-4]|h[1-2]|"
    r"\$?[\d,]+\.?\d*[km%]?(?:\s*[-–to]+\s*\$?[\d,]+\.?\d*[km%]?)?)\s*$",
    re.IGNORECASE,
)


def _p2_title_root(title: str) -> str:
    """Strip trailing date/value suffixes to find the shared event root.

    Used to group series markets (e.g. "Will GDP grow in Q1?" / "...Q2?")
    so we can detect over-sum inconsistency across the series.
    """
    t = title.lower().strip().rstrip("?.,!")
    # Iteratively strip up to 3 trailing tokens (e.g. "March 2025 $50k")
    for _ in range(3):
        new_t = _STRIP_SUFFIX.sub("", t)
        if new_t == t:
            break
        t = new_t.rstrip("?.,! ")
    return t


def _cross_strategy_dedup(opportunities: list[dict]) -> list[dict]:
    """Keep only the highest-signal_strength entry per market_id (5.1).

    A market that qualifies for both P3 and P4 in the same cycle contributes
    one position — for the strategy most confident about it.
    """
    best: dict[str, dict] = {}
    for opp in opportunities:
        mid = opp["market"]["id"]
        if mid not in best or opp["signal_strength"] > best[mid]["signal_strength"]:
            best[mid] = opp
    return list(best.values())


def _normalize_signal_strengths(opportunities: list[dict]) -> list[dict]:
    """Z-score normalize signal_strength within each strategy bucket (5.3).

    Adds a signal_strength_normalized key to each opportunity. The original
    signal_strength is preserved. Single-item buckets receive 0.0.
    """
    by_strategy: dict[str, list[dict]] = {}
    for opp in opportunities:
        by_strategy.setdefault(opp["strategy"], []).append(opp)

    result = []
    for opps in by_strategy.values():
        strengths = [o["signal_strength"] for o in opps]
        if len(strengths) == 1:
            result.append({**opps[0], "signal_strength_normalized": 0.0})
            continue
        mean = statistics.mean(strengths)
        stdev = statistics.stdev(strengths)
        for opp in opps:
            z = (opp["signal_strength"] - mean) / stdev if stdev > 0 else 0.0
            result.append({**opp, "signal_strength_normalized": z})
    return result


async def _get_strategy_rolling_pnl(
    db: aiosqlite.Connection,
    strategy: str,
    window_s: int,
) -> tuple[int, float]:
    """Return (trade_count, total_pnl) for strategy over the last window_s seconds.

    Only counts realistic-model closed positions (pnl_model='realistic').
    Used by the per-strategy kill-switch (5.6).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_s)).isoformat()
    cursor = await db.execute(
        "SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0.0) "
        "FROM positions "
        "WHERE strategy=? AND pnl_model='realistic' AND status='closed' AND closed_at >= ?",
        (strategy, cutoff),
    )
    row = await cursor.fetchone()
    return row[0], row[1]


async def mark_and_close_positions(
    db: aiosqlite.Connection,
    holding_period_s: int = 300,
) -> int:
    """Close open positions that have exceeded their holding period.

    Walks all positions with status='open' opened more than holding_period_s
    ago, queries the latest market price, computes realized_pnl, and closes
    them. Positions with no current price data are left open (stale handling).

    Returns the number of positions closed.
    """
    now_dt = datetime.now(timezone.utc)
    cutoff = (now_dt - timedelta(seconds=holding_period_s)).isoformat()
    now = now_dt.isoformat()

    cursor = await db.execute(
        "SELECT id, market_id, side, entry_price, entry_size, fees_paid "
        "FROM positions WHERE status='open' AND opened_at <= ?",
        (cutoff,),
    )
    rows = await cursor.fetchall()

    closed = 0
    for row in rows:
        pos_id, market_id, side, entry_price, entry_size, fees_paid = row

        price_cursor = await db.execute(
            "SELECT yes_price FROM market_prices WHERE market_id=? "
            "ORDER BY polled_at DESC LIMIT 1",
            (market_id,),
        )
        price_row = await price_cursor.fetchone()
        if price_row is None:
            logger.debug(
                "mark_and_close: no price for market %s — leaving open", market_id
            )
            continue

        current_price = price_row[0]
        if side == "BUY":
            realized_pnl = round(
                (current_price - entry_price) * entry_size - (fees_paid or 0), 4
            )
        else:
            realized_pnl = round(
                (entry_price - current_price) * entry_size - (fees_paid or 0), 4
            )

        await db.execute(
            """UPDATE positions
               SET status='closed', exit_price=?, exit_size=?,
                   realized_pnl=?, current_price=?, closed_at=?, updated_at=?
             WHERE id=?""",
            (current_price, entry_size, realized_pnl, current_price, now, now, pos_id),
        )
        closed += 1
        logger.debug(
            "mark_and_close: closed pos=%s market=%s pnl=%.4f",
            pos_id,
            market_id,
            realized_pnl,
        )

    if closed:
        await db.commit()
        logger.info("mark_and_close_positions: closed %d expired positions", closed)

    return closed


async def detect_single_platform_opportunities(
    db: aiosqlite.Connection,
    max_trades: int = 20,
    risk_config=None,
) -> list[dict]:
    """
    Find trading opportunities on individual markets (no cross-platform match needed).

    Strategies:
    - P3_calibration_bias: Market is mispriced vs estimated fair value
      (e.g., price far from 0.50 on a coin-flip event, or spread is wide)
    - P4_liquidity_timing: Wide bid/ask spread indicates market maker opportunity
    - P5_mean_reversion: Price moved sharply, bet on reversion
    """
    from core.config import RiskControlConfig
    from core.signals.sizing import compute_kelly_fraction, compute_position_size
    from execution.factory import _make_single_execution_client
    from execution.models import OrderLeg

    _risk_cfg: RiskControlConfig = risk_config or RiskControlConfig()

    execution_mode = os.getenv("EXECUTION_MODE", "paper")
    # Cache clients by platform to avoid re-initializing per trade
    _clients: dict = {}

    # Get all markets with their latest prices
    cursor = await db.execute("""SELECT m.id, m.platform, m.title,
                  mp.yes_price, mp.no_price, mp.spread,
                  mp.volume_24h, mp.liquidity
           FROM markets m
           JOIN market_prices mp ON mp.market_id = m.id
           WHERE m.status = 'open'
             AND mp.yes_price > 0.05
             AND mp.yes_price < 0.95
           ORDER BY mp.polled_at DESC""")
    rows = await cursor.fetchall()

    # Deduplicate (keep latest per market)
    seen = set()
    markets = []
    for row in rows:
        if row[0] not in seen:
            seen.add(row[0])
            markets.append(
                {
                    "id": row[0],
                    "platform": row[1],
                    "title": row[2],
                    "yes_price": row[3],
                    "no_price": row[4],
                    "spread": row[5] or 0.02,
                    "volume_24h": row[6] or 0,
                    "liquidity": row[7] or 0,
                }
            )

    trades = []
    opportunities = []

    for m in markets:
        price = m["yes_price"]

        # Strategy: Calibration bias — markets with extreme prices
        # (far from 0.50) have high implied conviction. We bet toward
        # the center, assuming mean reversion over time.
        # Edge = 2% of the distance from center (conservative).
        distance_from_center = abs(price - 0.50)
        if distance_from_center > 0.20:
            side = "BUY" if price < 0.50 else "SELL"
            edge = round(distance_from_center * 0.04, 4)  # 4% of distance
            opportunities.append(
                {
                    "market": m,
                    "strategy": "P3_calibration_bias",
                    "side": side,
                    "edge": max(edge, 0.005),
                    "signal_strength": distance_from_center,
                }
            )

        # Strategy: Liquidity timing — markets in the "uncertain zone"
        # (0.35 - 0.65) are most liquid and have the tightest spreads.
        # For markets slightly outside this zone (0.20-0.35 or 0.65-0.80),
        # there's often a liquidity premium we can capture.
        if 0.15 < price < 0.35 or 0.65 < price < 0.85:
            side = "BUY" if price < 0.50 else "SELL"
            edge = round(abs(price - 0.50) * 0.03, 4)  # 3% of distance
            opportunities.append(
                {
                    "market": m,
                    "strategy": "P4_liquidity_timing",
                    "side": side,
                    "edge": max(edge, 0.005),
                    "signal_strength": abs(price - 0.50),
                }
            )

        # Strategy: Information latency — wide bid-ask spread combined with an
        # extreme price indicates that market makers haven't caught up to recent
        # information. The directional signal is already in the price; the edge
        # is the spread compression that occurs as information propagates.
        if m["spread"] >= 0.08 and (price < 0.25 or price > 0.75):
            side = "BUY" if price < 0.50 else "SELL"
            edge = round(m["spread"] * 0.30, 4)  # capture ~30% of the spread
            opportunities.append(
                {
                    "market": m,
                    "strategy": "P5_information_latency",
                    "side": side,
                    "edge": max(edge, 0.005),
                    "signal_strength": m["spread"],
                }
            )

    # Strategy: Structured event inconsistency — group same-platform markets by
    # title root (stripping date/value suffixes). When the YES prices of a
    # mutually-exclusive series sum > 1.05, sell the most overpriced member.
    _p2_groups: dict[tuple, list[dict]] = defaultdict(list)
    for m in markets:
        root = _p2_title_root(m["title"])
        if len(root) >= 25:
            _p2_groups[(m["platform"], root)].append(m)

    for (_plat, _root), group in _p2_groups.items():
        if len(group) < 2 or len(group) > 10:
            continue
        total_yes = sum(m["yes_price"] for m in group)
        if total_yes <= 1.05:
            continue
        expected = 1.0 / len(group)
        best = max(group, key=lambda m: m["yes_price"] - expected)
        edge = round((total_yes - 1.0) / len(group), 4)
        opportunities.append(
            {
                "market": best,
                "strategy": "P2_structured_event",
                "side": "SELL",
                "edge": max(edge, 0.005),
                "signal_strength": total_yes - 1.0,
            }
        )

    # ── Phase 5: Strategy hygiene ─────────────────────────────────────────────

    # 5.4 — Filter strategies disabled via config flags
    _strategy_enabled = {
        "P2_structured_event": _risk_cfg.strategy_p2_enabled,
        "P3_calibration_bias": _risk_cfg.strategy_p3_enabled,
        "P4_liquidity_timing": _risk_cfg.strategy_p4_enabled,
        "P5_information_latency": _risk_cfg.strategy_p5_enabled,
    }
    opportunities = [
        o for o in opportunities if _strategy_enabled.get(o["strategy"], True)
    ]

    # 5.1 — Cross-strategy dedup: same market → keep highest signal_strength only
    opportunities = _cross_strategy_dedup(opportunities)

    # 5.2 — Consecutive-cycle dedup: skip recently-traded markets unless price moved
    _cooldown = _risk_cfg.strategy_replay_cooldown_s
    _min_move = _risk_cfg.strategy_replay_min_move

    _cutoff = (datetime.now(timezone.utc) - timedelta(seconds=_cooldown)).isoformat()
    _recent_cursor = await db.execute(
        "SELECT market_id, entry_price FROM positions "
        "WHERE (status='open' OR (status='closed' AND closed_at >= ?)) "
        "ORDER BY opened_at DESC",
        (_cutoff,),
    )
    _recently_traded: dict[str, float] = {}
    for _row in await _recent_cursor.fetchall():
        _mid, _ep = _row[0], _row[1]
        if _mid not in _recently_traded:
            _recently_traded[_mid] = _ep or 0.0

    _filtered: list[dict] = []
    for opp in opportunities:
        mid = opp["market"]["id"]
        if mid in _recently_traded:
            ep = _recently_traded[mid]
            cp = opp["market"]["yes_price"]
            move = abs(cp - ep) / max(ep, 0.001) if ep else 1.0
            if move < _min_move:
                logger.debug(
                    "5.2 replay-dedup: skip market=%s move=%.4f < min=%.4f",
                    mid,
                    move,
                    _min_move,
                )
                continue
        _filtered.append(opp)
    opportunities = _filtered

    # 5.6 — Per-strategy kill-switch: disable strategy if rolling PnL is negative
    # and enough trades have been recorded to be statistically meaningful.
    _killed_strategies: set[str] = set()
    for _strat in list({o["strategy"] for o in opportunities}):
        _count, _pnl = await _get_strategy_rolling_pnl(
            db, _strat, _risk_cfg.strategy_killswitch_window_s
        )
        if _count >= _risk_cfg.strategy_killswitch_min_trades and _pnl < 0:
            logger.warning(
                "KILLSWITCH: strategy=%s disabled — rolling_pnl=%.4f over %d trades",
                _strat,
                _pnl,
                _count,
            )
            _killed_strategies.add(_strat)
    opportunities = [
        o for o in opportunities if o["strategy"] not in _killed_strategies
    ]

    # 5.3 — Normalize signal_strength within each strategy bucket (z-score)
    opportunities = _normalize_signal_strengths(opportunities)

    # ── Quota allocation ──────────────────────────────────────────────────────
    # Reserve slots per strategy to prevent any single strategy crowding others.
    # P2: 15%, P3: 50%, P4: 25%, P5: remainder (min 1 each).
    p2_cap = max(1, int(max_trades * 0.15))
    p3_cap = max(1, int(max_trades * 0.50))
    p4_cap = max(2, int(max_trades * 0.25))
    p5_cap = max(1, max_trades - p2_cap - p3_cap - p4_cap)

    p2 = sorted(
        [o for o in opportunities if o["strategy"] == "P2_structured_event"],
        key=lambda x: x["signal_strength"],
        reverse=True,
    )[:p2_cap]
    p3 = sorted(
        [o for o in opportunities if o["strategy"] == "P3_calibration_bias"],
        key=lambda x: x["signal_strength"],
        reverse=True,
    )[:p3_cap]
    p4 = sorted(
        [o for o in opportunities if o["strategy"] == "P4_liquidity_timing"],
        key=lambda x: x["signal_strength"],
        reverse=True,
    )[:p4_cap]
    p5 = sorted(
        [o for o in opportunities if o["strategy"] == "P5_information_latency"],
        key=lambda x: x["signal_strength"],
        reverse=True,
    )[:p5_cap]
    opportunities = p2 + p3 + p4 + p5

    if opportunities:
        logger.info(
            "Found %d single-platform opportunities (from %d markets)",
            len(opportunities),
            len(markets),
        )

    for opp in opportunities:
        now = datetime.now(timezone.utc).isoformat()
        m = opp["market"]
        strategy = opp["strategy"]
        side = opp["side"]
        edge = opp["edge"]
        price = m["yes_price"]

        # Phase 2.3: Kelly-based sizing (replaces hardcoded min(10, 100*edge)).
        _kelly_f = compute_kelly_fraction(edge, 1.0, _risk_cfg.kelly_fraction)
        _bankroll = _risk_cfg.starting_capital
        _max_size = _bankroll * _risk_cfg.max_position_pct
        size = round(compute_position_size(_kelly_f, _bankroll, max_size=_max_size), 1)
        if size <= 0:
            logger.debug(
                "Kelly sizing zero for edge=%.4f strategy=%s — skip", edge, strategy
            )
            continue

        signal_id = f"sig_{uuid.uuid4().hex[:12]}"
        violation_id = f"viol_{uuid.uuid4().hex[:12]}"

        logger.info(
            "SINGLE-PLATFORM: %s | %s %s@%.3f spread=%.3f | %s | %s",
            strategy,
            side,
            m["platform"],
            price,
            m["spread"],
            m["title"][:50],
            m["id"][:25],
        )

        # Create self-referencing market pair for single-platform
        pair_id = f"sp_{m['id']}"
        try:
            await db.execute(
                """INSERT OR IGNORE INTO market_pairs
                   (id, market_id_a, market_id_b, pair_type, active, created_at, updated_at)
                   VALUES (?, ?, ?, 'single_platform', 1, ?, ?)""",
                (pair_id, m["id"], m["id"], now, now),
            )
        except Exception as e:
            logger.debug("Market pair insert error: %s", e)

        # Write violation
        try:
            await db.execute(
                """INSERT OR IGNORE INTO violations
                   (id, pair_id, violation_type, price_a_at_detect, price_b_at_detect,
                    raw_spread, net_spread, status, detected_at, updated_at)
                   VALUES (?, ?, 'single_platform', ?, ?, ?, ?, 'detected', ?, ?)""",
                (
                    violation_id,
                    pair_id,
                    price,
                    m["no_price"],
                    m["spread"],
                    edge,
                    now,
                    now,
                ),
            )
        except Exception as e:
            logger.debug("Violation insert error: %s", e)

        # Write signal
        try:
            kelly = min(edge / 0.50, 0.25)
            await db.execute(
                """INSERT OR IGNORE INTO signals
                   (id, violation_id, strategy, signal_type, market_id_a,
                    model_edge, kelly_fraction, position_size_a,
                    total_capital_at_risk, status, fired_at, updated_at)
                   VALUES (?, ?, ?, 'single', ?, ?, ?, ?, ?, 'fired', ?, ?)""",
                (
                    signal_id,
                    violation_id,
                    strategy,
                    m["id"],
                    edge,
                    kelly,
                    size,
                    size,
                    now,
                    now,
                ),
            )
        except Exception as e:
            logger.debug("Signal insert error: %s", e)

        # Get (or create) execution client for this market's platform
        platform = m["platform"]
        if platform not in _clients:
            _clients[platform] = _make_single_execution_client(
                db, execution_mode, platform
            )

        leg = OrderLeg(
            market_id=m["id"],
            platform=platform,
            side=side,
            size=size,
            limit_price=price,
            order_type="LIMIT",
        )
        result = await _clients[platform].submit_order(
            leg, signal_id=signal_id, strategy=strategy
        )

        if result.filled_price:
            # Phase 4: open position, NO synthetic exit price.
            # mark_and_close_positions() will close it after holding_period_s
            # at the then-current market price, giving a realistic realized PnL.
            pos_id = f"pos_{uuid.uuid4().hex[:12]}"
            try:
                await db.execute(
                    """INSERT INTO positions
                       (id, signal_id, market_id, strategy, side, entry_price,
                        entry_size, fees_paid, pnl_model, status, opened_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'realistic', 'open', ?, ?)""",
                    (
                        pos_id,
                        signal_id,
                        m["id"],
                        strategy,
                        side,
                        result.filled_price,
                        size,
                        result.fee_paid or 0,
                        now,
                        now,
                    ),
                )
            except Exception as e:
                logger.debug("Position insert error: %s", e)

            trades.append(
                {
                    "strategy": strategy,
                    "market": f"{m['platform']}:{m['id'][:20]}",
                    "side": side,
                    "price": result.filled_price,
                    "edge": edge,
                    "pos_id": pos_id,
                }
            )

            logger.info(
                "  OPENED: %s@%.4f edge=%.4f | pos=%s (holding until close)",
                side,
                result.filled_price,
                edge,
                pos_id,
            )

            # Batch commit every 50 trades
            if len(trades) % 50 == 0:
                await db.commit()

    # Final commit
    await db.commit()
    return trades
