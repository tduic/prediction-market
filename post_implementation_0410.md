  Post-implementation notes: bulletproof the predictor trading system

  Companion to implementation_0410.md. One section per phase. Updated as each
  phase lands. Records what was done, gaps found during implementation, and
  any adjustments to downstream phases.

  ---
  Phase 0 — Stop the bleeding and get honest telemetry

  Status: COMPLETE (commit 5933313)

  What was implemented
  ─────────────────────
  0.1  STATUS log truthfulness
       - ArbitrageEngine.open_positions renamed to recently_fired. The old name
         implied "currently open" when the set is actually "ever fired and never
         cleared." The rename propagates through initial_sweep, on_price_update,
         and all tests.
       - stats() now returns: pairs_monitored, pairs_eligible_now, recently_fired,
         last_arb_fired_at, ticks_since_last_fire, ws_last_tick_age_ms_by_platform.
         The old open_positions and total_trades keys are gone.
       - pairs_eligible_now is computed live on every stats() call from the
         in-memory price cache — not a cached counter.
       - STATUS log format changed to:
         "STATUS: pairs=%d eligible=%d muted=%d pnl=$%.2f prices=%d |
          last_fire=%s ticks_since=%d | scheduled=%d"
       - _last_tick_at (market_id → timestamp) and _market_platform
         (market_id → "polymarket"/"kalshi") added to __init__ for
         ws_last_tick_age_ms_by_platform computation.

  0.2  PnL sanity cap
       - Module-level constant _PNL_SANITY_CAP_RATIO = 0.10.
       - Applied to all three execution paths:
           · _execute_arb_trade (ArbitrageEngine method, line ~1400)
           · _execute_dual_platform_arb (standalone function, line ~1708)
           · scheduled strategy loop (line ~2081)
       - Blocked trades log a WARNING and skip the DB insert. No exception raised.
         Arb paths return None; scheduled loop uses continue.
       - Catches false-positive P1 trades that were booking ~40% of size. Any
         legitimate arb with a real spread (2–5%) produces pnl ~2–5% of size,
         well under the 10% cap.

  0.3  Pause P1
       - MIN_SPREAD_CROSS_PLATFORM env var added. Checked after argparse; overrides
         args.min_spread if set.
       - Set MIN_SPREAD_CROSS_PLATFORM=99.0 in .env to produce zero P1 trades
         until the matcher is fixed in Phase 1. Requires a service restart
         (no hot-reload exists — confirmed by audit).

  0.4  Baseline snapshot
       - Migration 006_phase0_baseline.sql: phase0_baseline table captures
         pair_count, active_pair_count, p1–p5 realized PnL, total_realized_pnl,
         total_trade_count, and snapshot_timestamp.
       - take_phase0_baseline_snapshot(db) added to paper_trading_session.py.
         Safe to call multiple times (each call appends a new row).
       - NOT yet wired into the startup path — call manually or via Phase 7
         post-deploy smoke test.

  Gaps and watch-outs
  ────────────────────
  - ticks_since_last_fire only increments on meaningful deltas (>0.001). Raw WS
    message count will be higher. This is intentional — we want "ticks that
    caused the engine to evaluate" not "raw bytes received."
  - take_phase0_baseline_snapshot() is not called automatically at startup.
    Wire it in Phase 7 (post-deploy smoke test step).
  - MIN_SPREAD_CROSS_PLATFORM requires a restart. The plan accepted this.
  - The cap of 10% is a tourniquet, not a fix. A 9.9%-pnl trade would still
    pass through. The real fix is Phase 1 (matcher) + Phase 4 (fill model).

  Downstream impact
  ──────────────────
  - Phase 1 (matcher fix): touches extract_numbers / numeric penalty at line
    ~664. No overlap with Phase 0 changes.
  - Phase 3 (re-arm): will need to handle recently_fired vs the new
    PairFireState struct. recently_fired will be replaced entirely.
  - Phase 7 (invariants): add take_phase0_baseline_snapshot() call to the
    post-deploy smoke test.

  ---
