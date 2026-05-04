"""
Trading session entry point: fetches real markets, detects real violations,
generates signals, executes through paper/shadow/live client, and reports analytics.

Streams real-time prices from Polymarket and Kalshi websockets. The ArbitrageEngine
fires P1 trades instantly on every price tick; the ScheduledStrategyRunner runs
P2-P5 strategies on a fixed interval. All modes (paper/shadow/live) run the same
pipeline — only the execution client differs.

Usage:
    python scripts/trading_session.py              # Stream with cached matches
    python scripts/trading_session.py --refresh    # Re-fetch all markets, then stream
    python scripts/trading_session.py --dashboard  # Stream + analytics dashboard on :8000
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

print("[startup] Loading environment...", flush=True)
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

# Setup path for imports
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

print("[startup] Importing dependencies...", flush=True)
import aiosqlite  # noqa: E402

# Import directly from submodules to avoid core/__init__.py which eagerly
# loads EventBus, Database, etc. and can hang on first compilation.
print("[startup] Loading config...", flush=True)
from core.config import get_config  # noqa: E402

print("[startup] Loading storage...", flush=True)
from core.storage.db import Database  # noqa: E402

print("[startup] Loading logging config...", flush=True)
from core.logging_config import configure_from_env  # noqa: E402

print("[startup] All imports complete.", flush=True)

logger = logging.getLogger(__name__)


async def refresh_markets_and_matches(db: aiosqlite.Connection, cfg) -> list[dict]:
    """Full fetch + store + match + persist. Run once, then use cached matches."""
    from core.ingestor.store import (
        fetch_kalshi_markets,
        fetch_polymarket_markets,
        store_markets,
    )
    from core.matching.engine import find_matches, persist_matches

    logger.info("=" * 50)
    logger.info("Fetching ALL markets from exchanges...")

    poly_task = asyncio.create_task(fetch_polymarket_markets())
    kalshi_task = asyncio.create_task(
        fetch_kalshi_markets(
            api_key=cfg.platform_credentials.kalshi_api_key,
            rsa_key_path=cfg.platform_credentials.kalshi_rsa_key_path,
            api_base=cfg.platform_credentials.kalshi_api_base,
        )
    )
    poly_markets, kalshi_markets = await asyncio.gather(poly_task, kalshi_task)

    if not poly_markets and not kalshi_markets:
        logger.warning("No markets fetched")
        return []

    await store_markets(db, poly_markets, kalshi_markets)

    matches = await find_matches(db)
    if matches:
        await persist_matches(db, matches)
    else:
        logger.info("No cross-platform matches found")

    return matches


async def _build_subscription_lists(
    db: aiosqlite.Connection, matches: list[dict]
) -> tuple[list[str], dict[str, str], list[str]]:
    """Derive Polymarket asset IDs + Kalshi tickers from a match set.

    Returns ``(poly_platform_ids, poly_id_map, kalshi_tickers)`` where
    ``poly_id_map`` maps the exchange-facing asset_id back to our internal
    ``poly_XXX`` id so websocket ticks can be attributed to the right market.
    """
    kalshi_tickers = [m["kalshi_id"].replace("kal_", "") for m in matches]
    poly_internal_ids = list({m["poly_id"] for m in matches})
    poly_platform_ids: list[str] = []
    poly_id_map: dict[str, str] = {}
    for pid in poly_internal_ids:
        cursor = await db.execute(
            "SELECT platform_id FROM markets WHERE id = ?", (pid,)
        )
        row = await cursor.fetchone()
        if row and row[0]:
            poly_platform_ids.append(row[0])
            poly_id_map[row[0]] = pid
    return poly_platform_ids, poly_id_map, kalshi_tickers


def _spawn_ws_tasks(
    poly_platform_ids: list[str],
    poly_id_map: dict[str, str],
    kalshi_tickers: list[str],
    stop_event: asyncio.Event,
    on_price,
    cfg,
) -> list[asyncio.Task]:
    """Create websocket streaming tasks for the given asset/ticker sets."""
    from core.ingestor.streamer import (
        stream_prices_kalshi,
        stream_prices_polymarket,
    )

    tasks: list[asyncio.Task] = []
    if poly_platform_ids:
        tasks.append(
            asyncio.create_task(
                stream_prices_polymarket(
                    asset_ids=poly_platform_ids,
                    stop_event=stop_event,
                    on_price=on_price,
                    id_map=poly_id_map,
                )
            )
        )
    if kalshi_tickers:
        tasks.append(
            asyncio.create_task(
                stream_prices_kalshi(
                    tickers=kalshi_tickers,
                    stop_event=stop_event,
                    on_price=on_price,
                    api_key=cfg.platform_credentials.kalshi_api_key,
                    rsa_key_path=cfg.platform_credentials.kalshi_rsa_key_path,
                )
            )
        )
    return tasks


async def _pair_refresh_loop(
    db: aiosqlite.Connection,
    arb_engine,
    ws_state: dict,
    stop_event: asyncio.Event,
    cfg,
    on_price,
    interval: int = 1800,
) -> None:
    """Periodically reload cached matches and hot-swap the engine's pair list.

    The weekly ``predictor-refresh.service`` writes fresh ``market_pairs`` rows
    to the DB. Without this loop, the running trading session would keep
    trading the pair set it loaded at startup until someone restarted it. This
    loop checks every ``interval`` seconds and:

      1. loads the current cached matches,
      2. calls ``arb_engine.update_pairs()`` which diffs added / removed,
      3. if the websocket subscription set changed, cancels the WS tasks and
         spawns fresh ones with the new asset / ticker lists,
      4. runs an ``initial_sweep`` so new pairs already above the spread
         threshold fire immediately instead of waiting for a price delta.
    """
    from core.matching.engine import load_cached_matches

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            return
        except asyncio.TimeoutError:
            pass

        try:
            matches = await load_cached_matches(db)
            if not matches:
                logger.debug("pair refresh: no matches in DB")
                continue

            delta = arb_engine.update_pairs(matches)
            if delta["added"] == 0 and delta["removed"] == 0:
                logger.debug("pair refresh: no change")
                continue

            new_poly, new_map, new_kalshi = await _build_subscription_lists(db, matches)
            if (
                set(new_poly) == ws_state["poly_ids"]
                and set(new_kalshi) == ws_state["kalshi_tickers"]
            ):
                # Pair set changed but the underlying markets are the same
                # (e.g. one pair swapped for another using the same markets).
                logger.info(
                    "pair refresh: added=%d removed=%d (WS set unchanged)",
                    delta["added"],
                    delta["removed"],
                )
                await arb_engine.initial_sweep()
                continue

            logger.info(
                "pair refresh: added=%d removed=%d — restarting WS streams "
                "(%d poly, %d kalshi)",
                delta["added"],
                delta["removed"],
                len(new_poly),
                len(new_kalshi),
            )
            old_tasks = ws_state["tasks"]
            for t in old_tasks:
                t.cancel()
            await asyncio.gather(*old_tasks, return_exceptions=True)
            ws_state["tasks"] = _spawn_ws_tasks(
                new_poly, new_map, new_kalshi, stop_event, on_price, cfg
            )
            ws_state["poly_ids"] = set(new_poly)
            ws_state["kalshi_tickers"] = set(new_kalshi)
            await arb_engine.initial_sweep()
        except Exception:
            logger.exception("pair refresh loop error")


async def main():
    configure_from_env()

    parser = argparse.ArgumentParser(
        description="Trading session with real-time websocket streaming (paper/shadow/live)"
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force re-fetch all markets and re-match before streaming. "
        "Without this flag, uses cached matches from DB.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=120,
        help="Seconds between scheduled strategy cycles (default: 120)",
    )
    parser.add_argument(
        "--min-spread",
        type=float,
        default=0.03,
        help="Minimum spread to trade (default: 0.03)",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Launch the analytics dashboard web server alongside trading.",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8000,
        help="Port for the analytics dashboard (default: 8000)",
    )
    parser.add_argument(
        "--dashboard-host",
        type=str,
        default="127.0.0.1",
        help="Host to bind the dashboard to (default: 127.0.0.1; use 0.0.0.0 to expose publicly)",
    )
    args = parser.parse_args()

    # Allow env var to override --min-spread without requiring a code change.
    # Set MIN_SPREAD_CROSS_PLATFORM=99.0 in .env to pause all P1 arb until
    # the matcher is fixed.
    _env_min_spread = os.getenv("MIN_SPREAD_CROSS_PLATFORM")
    if _env_min_spread is not None:
        args.min_spread = float(_env_min_spread)
        logger.info(
            "  P1 min_spread overridden by MIN_SPREAD_CROSS_PLATFORM env: %.4f",
            args.min_spread,
        )

    cfg = get_config()

    # Phase 6: pre-live safety gate (no-op for paper/shadow)
    from core.live_gate import check_live_gate

    check_live_gate(cfg.execution.execution_mode)

    logger.info("Trading Session")
    logger.info("  Execution: %s", cfg.execution.execution_mode)
    logger.info("  Database: %s", cfg.database.db_path)
    logger.info("  Min spread: %.4f", args.min_spread)
    logger.info("  Strategy interval: %ds", args.interval)
    if args.dashboard:
        logger.info("  Dashboard: http://127.0.0.1:%d", args.dashboard_port)

    db_wrapper = Database(
        cfg.database.db_path, migrations_dir=cfg.database.migrations_dir
    )
    await db_wrapper.init()
    db = db_wrapper._conn

    try:
        # ── Dashboard (start FIRST so it's available during refresh) ──
        dashboard_task = None
        if args.dashboard:
            from scripts.dashboard_api import (
                create_dashboard_app,
                start_dashboard_server,
            )

            script_dir = Path(__file__).resolve().parent
            dist_dir = script_dir.parent / "dashboard" / "dist"
            static_dir = str(dist_dir) if dist_dir.is_dir() else None
            if not static_dir:
                logger.warning(
                    "dashboard/dist/ not found — run 'npm run build' in dashboard/. "
                    "API will still be available but no frontend."
                )
            dashboard_app = create_dashboard_app(
                db_path=cfg.database.db_path,
                static_dir=static_dir,
            )
            dashboard_task = asyncio.create_task(
                start_dashboard_server(
                    dashboard_app,
                    host=args.dashboard_host,
                    port=args.dashboard_port,
                )
            )
            # Yield to let uvicorn bind before continuing
            await asyncio.sleep(0.5)
            logger.info(
                "Dashboard live at http://%s:%d",
                args.dashboard_host,
                args.dashboard_port,
            )

        # ── Step 1: Get matches (refresh or load from cache) ──
        from core.matching.engine import load_cached_matches

        if args.refresh:
            logger.info("Refreshing markets and matches...")
            matches = await refresh_markets_and_matches(db, cfg)
        else:
            matches = await load_cached_matches(db)
            if not matches:
                logger.info("No cached matches found — running initial refresh...")
                matches = await refresh_markets_and_matches(db, cfg)

        from core.snapshots.pnl import print_analytics, take_trading_snapshot

        if not matches:
            logger.warning("No matches available. Run with --refresh to fetch markets.")
            await print_analytics(db)
            if dashboard_task:
                logger.info(
                    "No matches, but dashboard is live at http://127.0.0.1:%d  (Ctrl-C to quit)",
                    args.dashboard_port,
                )
                try:
                    await dashboard_task
                except asyncio.CancelledError:
                    pass
            return

        logger.info("Working with %d matched pairs", len(matches))

        # ── Step 2: Stream ──
        from core.engine import ArbitrageEngine, ScheduledStrategyRunner

        stop_event = asyncio.Event()

        # Take initial snapshot so dashboard has data immediately
        try:
            await take_trading_snapshot(db)
        except Exception as e:
            logger.warning("Initial snapshot failed (will retry later): %s", e)

        from core.alerting import get_alert_manager  # noqa: E402
        from core.live_gate import get_effective_risk_config  # noqa: E402
        from execution.circuit_breaker import DailyLossCircuitBreaker  # noqa: E402

        execution_mode = cfg.execution.execution_mode
        risk_config = get_effective_risk_config(execution_mode)
        circuit_breaker = DailyLossCircuitBreaker(
            db=db,
            starting_capital=risk_config.starting_capital,
            max_daily_loss_pct=risk_config.max_daily_loss_pct,
            consecutive_failure_limit=risk_config.consecutive_failure_limit,
        )

        arb_engine = ArbitrageEngine(
            db,
            matches,
            min_spread=args.min_spread,
            risk_config=risk_config,
            circuit_breaker=circuit_breaker,
        )
        await arb_engine.initial_sweep()

        # Shared live price cache — updated on every websocket tick.
        # ArbitrageEngine (P1) reads it directly via on_price_update.
        # ScheduledStrategyRunner (P2-P5) receives it as price_cache so
        # detect_single_platform_opportunities uses these prices instead of
        # reading stale rows from market_prices.
        _live_prices: dict[str, float] = {}

        async def on_price(market_id: str, price: float) -> None:
            """Single callback for all websocket ticks — feeds all strategies."""
            _live_prices[market_id] = price
            await arb_engine.on_price_update(market_id, price)

        scheduled = ScheduledStrategyRunner(
            db,
            interval=args.interval,
            max_trades_per_cycle=20,
            risk_config=risk_config,
            circuit_breaker=circuit_breaker,
            alert_manager=get_alert_manager(),
            price_cache=_live_prices,
        )

        # Build asset ID maps for websocket subscriptions.
        # Polymarket WS needs platform_ids (condition_id), not our internal IDs.
        (
            poly_platform_ids,
            poly_id_map,
            kalshi_tickers,
        ) = await _build_subscription_lists(db, matches)
        logger.info(
            "Starting streams: %d Polymarket assets + %d Kalshi tickers",
            len(poly_platform_ids),
            len(kalshi_tickers),
        )

        # ws_state is the shared handle for the pair-refresh loop: when the
        # weekly refresh lands new matches, the loop replaces ws_state["tasks"]
        # with new streams subscribed to the updated asset/ticker sets.
        ws_state: dict = {
            "tasks": _spawn_ws_tasks(
                poly_platform_ids,
                poly_id_map,
                kalshi_tickers,
                stop_event,
                on_price,
                cfg,
            ),
            "poly_ids": set(poly_platform_ids),
            "kalshi_tickers": set(kalshi_tickers),
        }

        pair_refresh_task = asyncio.create_task(
            _pair_refresh_loop(
                db,
                arb_engine,
                ws_state,
                stop_event,
                cfg,
                on_price,
                interval=1800,
            )
        )

        scheduled_task = asyncio.create_task(scheduled.run(stop_event))

        async def _log_status():
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=30)
                    break
                except asyncio.TimeoutError:
                    stats = arb_engine.stats()
                    _last_fire = (
                        f"{time.time() - stats['last_arb_fired_at']:.0f}s ago"
                        if stats["last_arb_fired_at"]
                        else "never"
                    )
                    logger.info(
                        "STATUS: pairs=%d eligible=%d muted=%d pnl=$%.2f "
                        "prices=%d | last_fire=%s ticks_since=%d | scheduled=%d",
                        stats["pairs_monitored"],
                        stats["pairs_eligible_now"],
                        stats["recently_fired"],
                        stats["total_pnl"],
                        stats["prices_tracked"],
                        _last_fire,
                        stats["ticks_since_last_fire"],
                        scheduled.total_trades,
                    )
                    try:
                        await take_trading_snapshot(db)
                    except Exception as snap_err:
                        logger.debug("Snapshot failed: %s", snap_err)

        status_task = asyncio.create_task(_log_status())

        async def _periodic_arb_scan():
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=60)
                    break
                except asyncio.TimeoutError:
                    try:
                        await arb_engine.periodic_scan()
                    except Exception as scan_err:
                        logger.warning("periodic_arb_scan error: %s", scan_err)

        arb_scan_task = asyncio.create_task(_periodic_arb_scan())

        # Supervisor tasks whose identity is stable for the lifetime of the
        # session. WS tasks live in ws_state["tasks"] and may be swapped out
        # by the pair refresh loop — we gather them separately at shutdown.
        supervisor_tasks = [
            scheduled_task,
            status_task,
            arb_scan_task,
            pair_refresh_task,
            *([dashboard_task] if dashboard_task else []),
        ]

        try:
            await asyncio.gather(
                *supervisor_tasks, *ws_state["tasks"], return_exceptions=True
            )
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            stop_event.set()
            await arb_engine.flush()
            current_ws_tasks = ws_state["tasks"]
            for t in [*supervisor_tasks, *current_ws_tasks]:
                t.cancel()
            await asyncio.gather(
                *supervisor_tasks, *current_ws_tasks, return_exceptions=True
            )
            total_arb = len(arb_engine.trades)
            total_sched = scheduled.total_trades
            logger.info(
                "Final: %d arb trades + %d scheduled trades = %d total",
                total_arb,
                total_sched,
                total_arb + total_sched,
            )

        await print_analytics(db)

    finally:
        await db_wrapper.close()
        logger.info("Session complete. DB: %s", cfg.database.db_path)


if __name__ == "__main__":
    asyncio.run(main())
