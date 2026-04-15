"""
Trading session entry point: fetches real markets, detects real violations,
generates signals, executes through paper/live/shadow/mock client, and reports analytics.

This runs the full pipeline end-to-end using live market data. All data is
written to the same DB tables as live trading, so the dashboard and analytics
work identically.

All session types (paper/live/shadow/mock) route through this entry point with
only config differences — the execution mode is controlled via the
EXECUTION_MODE environment variable.

Usage:
    python scripts/trading_session.py --refresh     # Fetch all markets, match, persist, trade once
    python scripts/trading_session.py --once        # Use cached matches, trade once
    python scripts/trading_session.py --stream      # Use cached matches + websocket prices, trade continuously
    python scripts/trading_session.py --stream --dashboard  # Stream + analytics dashboard on :8000
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


async def run_trading_cycle(
    db: aiosqlite.Connection,
    matches: list[dict],
    min_spread: float,
    price_cache: dict | None = None,
) -> int:
    """Run one trading cycle using provided matches and optional live prices."""
    from core.strategies.batch import detect_violations_and_trade
    from core.strategies.single_platform import detect_single_platform_opportunities

    # If we have a live price cache from websockets, update match prices
    if price_cache:
        updated = 0
        for m in matches:
            poly_price = price_cache.get(m["poly_id"])
            kalshi_price = price_cache.get(m["kalshi_id"])
            if poly_price is not None:
                m["poly_price"] = poly_price
                updated += 1
            if kalshi_price is not None:
                m["kalshi_price"] = kalshi_price
                updated += 1
        if updated:
            logger.info("Updated %d prices from live websocket cache", updated)

    all_trades = []

    # 1. Cross-platform arbitrage
    if matches:
        cross_trades = await detect_violations_and_trade(
            db, matches, min_spread=min_spread
        )
        all_trades.extend(cross_trades)
    else:
        logger.info("No cross-platform matches to trade")

    # 2. Single-platform strategies
    single_trades = await detect_single_platform_opportunities(db, max_trades=20)
    all_trades.extend(single_trades)

    logger.info(
        "Cycle complete: %d trades (%d cross-platform, %d single-platform)",
        len(all_trades),
        len(all_trades) - len(single_trades),
        len(single_trades),
    )
    return len(all_trades)


async def main():
    configure_from_env()

    parser = argparse.ArgumentParser(
        description="Trading session with real market data (paper/live/shadow/mock)"
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force re-fetch all markets and re-match (slow, ~30s). "
        "Without this flag, uses cached matches from DB.",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Stream prices via websocket. Arb trades fire instantly on "
        "price updates; scheduled strategies run on --interval.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run single batch cycle using cached matches and exit.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=120,
        help="Seconds between scheduled strategy cycles in stream mode (default: 120)",
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
    args = parser.parse_args()

    # Allow env var to override --min-spread without requiring a code change.
    # Set MIN_SPREAD_CROSS_PLATFORM=99.0 in .env to pause all P1 arb until
    # the matcher is fixed (Phase 0.3 / Phase 1).
    _env_min_spread = os.getenv("MIN_SPREAD_CROSS_PLATFORM")
    if _env_min_spread is not None:
        args.min_spread = float(_env_min_spread)
        logger.info(
            "  P1 min_spread overridden by MIN_SPREAD_CROSS_PLATFORM env: %.4f",
            args.min_spread,
        )

    cfg = get_config()

    # Phase 6: pre-live safety gate (no-op for paper/mock/shadow)
    from core.live_gate import check_live_gate

    check_live_gate(cfg.execution.execution_mode)

    mode = "stream" if args.stream else ("refresh+once" if args.refresh else "once")
    logger.info("Trading Session")
    logger.info("  Mode: %s (execution: %s)", mode, cfg.execution.execution_mode)
    logger.info("  Database: %s", cfg.database.db_path)
    logger.info("  Min spread: %.4f", args.min_spread)
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
        if args.dashboard and args.stream:
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
                    host="127.0.0.1",
                    port=args.dashboard_port,
                )
            )
            # Yield to let uvicorn bind before continuing
            await asyncio.sleep(0.5)
            logger.info(
                "Dashboard live at http://127.0.0.1:%d",
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
                # Dashboard already running — just keep it alive
                logger.info(
                    "No matches, but dashboard is live at http://127.0.0.1:%d  (Ctrl-C to quit)",
                    args.dashboard_port,
                )
                try:
                    await dashboard_task
                except asyncio.CancelledError:
                    pass
            elif args.dashboard:
                from scripts.dashboard_api import (
                    create_dashboard_app,
                    start_dashboard_server,
                )

                script_dir = Path(__file__).resolve().parent
                dist_dir = script_dir.parent / "dashboard" / "dist"
                static_dir = str(dist_dir) if dist_dir.is_dir() else None
                dashboard_app = create_dashboard_app(
                    db_path=cfg.database.db_path,
                    static_dir=static_dir,
                )
                logger.info(
                    "No matches, but dashboard is live at http://127.0.0.1:%d  (Ctrl-C to quit)",
                    args.dashboard_port,
                )
                await start_dashboard_server(
                    dashboard_app, host="127.0.0.1", port=args.dashboard_port
                )
            return

        logger.info("Working with %d matched pairs", len(matches))

        # ── Step 2: Trade ──
        if args.stream:
            # ════════════════════════════════════════════════════════════
            #  STREAMING MODE
            # ════════════════════════════════════════════════════════════
            from core.engine import ArbitrageEngine, ScheduledStrategyRunner
            from core.ingestor.streamer import (
                stream_prices_kalshi,
                stream_prices_polymarket,
            )

            stop_event = asyncio.Event()

            # Take initial snapshot so dashboard has data immediately
            try:
                await take_trading_snapshot(db)
            except Exception as e:
                logger.warning("Initial snapshot failed (will retry later): %s", e)

            # Phase 2: instantiate risk config and circuit breaker alongside engine.
            # Phase 6: get_effective_risk_config selects tighter limits for live mode.
            from execution.circuit_breaker import DailyLossCircuitBreaker  # noqa: E402
            from core.live_gate import get_effective_risk_config  # noqa: E402
            from core.alerting import get_alert_manager  # noqa: E402

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

            scheduled = ScheduledStrategyRunner(
                db,
                interval=args.interval,
                max_trades_per_cycle=20,
                risk_config=risk_config,
                circuit_breaker=circuit_breaker,
                alert_manager=get_alert_manager(),
            )

            # Build asset ID maps for websocket subscriptions
            # Polymarket WS needs platform_ids (condition_id), not our internal IDs
            poly_platform_ids = []
            poly_id_map: dict[str, str] = {}  # platform_id -> internal poly_XXX id
            kalshi_tickers = []

            for m in matches:
                kalshi_tickers.append(m["kalshi_id"].replace("kal_", ""))

            # Look up Polymarket platform_ids in bulk
            poly_internal_ids = list({m["poly_id"] for m in matches})
            for pid in poly_internal_ids:
                cursor = await db.execute(
                    "SELECT platform_id FROM markets WHERE id = ?", (pid,)
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    poly_platform_ids.append(row[0])
                    poly_id_map[row[0]] = pid

            logger.info(
                "Starting streams: %d Polymarket assets + %d Kalshi tickers",
                len(poly_platform_ids),
                len(kalshi_tickers),
            )

            # Launch all concurrent tasks
            ws_tasks = []

            # Polymarket websocket → arb engine
            if poly_platform_ids:
                ws_tasks.append(
                    asyncio.create_task(
                        stream_prices_polymarket(
                            asset_ids=poly_platform_ids,
                            stop_event=stop_event,
                            on_price=arb_engine.on_price_update,
                            id_map=poly_id_map,
                        )
                    )
                )

            # Kalshi websocket → arb engine
            if kalshi_tickers:
                ws_tasks.append(
                    asyncio.create_task(
                        stream_prices_kalshi(
                            tickers=kalshi_tickers,
                            stop_event=stop_event,
                            on_price=arb_engine.on_price_update,
                            api_key=cfg.platform_credentials.kalshi_api_key,
                            rsa_key_path=cfg.platform_credentials.kalshi_rsa_key_path,
                        )
                    )
                )

            # Scheduled strategies (runs on timer)
            scheduled_task = asyncio.create_task(scheduled.run(stop_event))

            # Status logging + snapshot task (runs every 30s)
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
                        # Write dashboard snapshot every status cycle
                        try:
                            await take_trading_snapshot(db)
                        except Exception as snap_err:
                            logger.debug("Snapshot failed: %s", snap_err)

            status_task = asyncio.create_task(_log_status())

            # Gather all long-running tasks. The dashboard task is included
            # so the process stays alive even if websockets disconnect.
            all_tasks = [
                *ws_tasks,
                scheduled_task,
                status_task,
                *([dashboard_task] if dashboard_task else []),
            ]

            try:
                await asyncio.gather(*all_tasks, return_exceptions=True)
            except KeyboardInterrupt:
                logger.info("Shutting down...")
            finally:
                stop_event.set()
                await arb_engine.flush()
                for t in all_tasks:
                    t.cancel()
                await asyncio.gather(*all_tasks, return_exceptions=True)
                total_arb = len(arb_engine.trades)
                total_sched = scheduled.total_trades
                logger.info(
                    "Final: %d arb trades + %d scheduled trades = %d total",
                    total_arb,
                    total_sched,
                    total_arb + total_sched,
                )

        else:
            # ════════════════════════════════════════════════════════════
            #  BATCH MODE (--once or --refresh)
            # ════════════════════════════════════════════════════════════
            await run_trading_cycle(db, matches, args.min_spread)
            try:
                await take_trading_snapshot(db)
            except Exception as e:
                logger.warning("Post-batch snapshot failed: %s", e)

        await print_analytics(db)

        # If --dashboard was passed in batch mode, keep serving until Ctrl-C
        if args.dashboard and not args.stream:
            from scripts.dashboard_api import (
                create_dashboard_app,
                start_dashboard_server,
            )

            script_dir = Path(__file__).resolve().parent
            dist_dir = script_dir.parent / "dashboard" / "dist"
            static_dir = str(dist_dir) if dist_dir.is_dir() else None
            dashboard_app = create_dashboard_app(
                db_path=cfg.database.db_path,
                static_dir=static_dir,
            )
            logger.info(
                "Batch complete. Dashboard at http://127.0.0.1:%d  (Ctrl-C to quit)",
                args.dashboard_port,
            )
            await start_dashboard_server(
                dashboard_app, host="127.0.0.1", port=args.dashboard_port
            )

    finally:
        await db_wrapper.close()
        logger.info("Session complete. DB: %s", cfg.database.db_path)


if __name__ == "__main__":
    asyncio.run(main())
