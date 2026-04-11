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
  Phase 1 — Fix the matcher root cause

  Status: COMPLETE

  What was implemented
  ─────────────────────
  1.1  Semantic pattern constants
       - _OU_PAT: matches over/under, O/U, over, under (case-insensitive).
       - _N_PLUS_PAT: matches N+, "at least N", "N or more" threshold terms.
       - _THRESHOLD_ANY_PAT: union of the two above — used for "does this
         title have any threshold terminology?"
       Added at module level after _PNL_SANITY_CAP_RATIO.

  1.2  compute_match_score now accepts raw_a="", raw_b="" parameters
       - When provided, the raw (un-normalized) titles are used to run
         extract_numbers before normalization strips decimals.
       - Hard reject (return 0.0) when O/U is on one side and N+ is on the
         other AND the numeric sets from the raw titles differ. This is the
         root cause of the "O/U 5.5 rebounds vs 5+ rebounds" false positive:
         after normalize_title, "5.5" → "5 5", making sets match spuriously.
       - Hard reject when BOTH sides have any threshold terminology AND their
         numeric sets differ.
       - Old callers passing only 4 args are unaffected (raw params default
         to ""; guard is skipped when raw_a/raw_b are empty strings).

  1.3  _find_matches_sync passes raw titles to compute_match_score
       - Line ~830: `compute_match_score(p_norm, k_norm, p_tokens, k_tokens,
         p_title, k_title_raw)` — both raw titles were already in scope.
       - No other changes to the matching loop.

  1.4  persist_matches marks high-spread pairs as pending_review
       - `spread = round(abs(poly_price - kalshi_price), 10)` (rounded to
         avoid float precision traps like 0.55 - 0.50 > 0.05).
       - spread > 0.05 → active=0, notes='pending_review'.
       - spread ≤ 0.05 → active=1, notes=None (unchanged behavior).
       - INSERT OR REPLACE now includes the notes column.

  1.5  mark_existing_pairs_pending_review(db) added
       - Deactivates all pairs with notes IS NULL (i.e., unreviewed).
       - Pairs with any existing notes value are left alone.
       - Returns count of rows deactivated.
       - load_cached_matches already filters WHERE active=1, so
         pending_review pairs are automatically excluded from the arb engine.

  Gaps and watch-outs
  ────────────────────
  - Quarter discrimination (Q1 vs Q2) is NOT fixed: "Q1"/"Q2" become "q1"/"q2"
    after normalization and extract_numbers doesn't find word-boundary-delimited
    numbers inside them. The scorer still gives Q1 vs Q2 ~0.85. This is a known
    limitation; fixing it would require a dedicated quarter-aware normalization
    step or a separate token-level check.
  - _PNL_SANITY_CAP_RATIO (Phase 0) is still the safety net; Phase 1 reduces
    false-positive matches entering the engine but doesn't eliminate all paths
    where a bad match could slip through.
  - mark_existing_pairs_pending_review must be called once against the live DB
    to retroactively deactivate unreviewed historical pairs. Not wired into
    startup — call manually before re-enabling P1 trades (lowering
    MIN_SPREAD_CROSS_PLATFORM below 99.0).

  Downstream impact
  ──────────────────
  - Phase 2 (risk choke point): no overlap with Phase 1 changes.
  - Phase 3 (re-arm): recently_fired still in place; Phase 3 will replace it
    with PairFireState. No Phase 1 changes affect that path.
  - Phase 7 (invariants): add call to mark_existing_pairs_pending_review in
    the post-deploy smoke test before any P1 trading resumes.

  ---
  Phase 2 — Route all execution through one risk choke point

  Status: COMPLETE

  What was implemented
  ─────────────────────
  2.1  Risk infrastructure imports at module level
       - `from core.config import RiskControlConfig` added to the top-level
         config import.
       - `run_all_checks`, `compute_kelly_fraction`, `compute_position_size`
         imported after dotenv setup (marked noqa: E402 per repo pattern).
       - `_RiskLeg` and `_RiskSignal` dataclasses added after the pattern
         constants to satisfy run_all_checks duck-typed interface without
         pulling in Redis-dependent Signal from core/signals/generator.py.

  2.2  ArbitrageEngine accepts risk_config and circuit_breaker
       - `__init__` gains `risk_config: RiskControlConfig | None = None` and
         `circuit_breaker = None` parameters.
       - Stored as `self._risk_config` (defaults to `RiskControlConfig()`) and
         `self._circuit_breaker`.

  2.3  circuit_breaker.should_halt() gates _execute_arb_trade
       - First thing in _execute_arb_trade: if circuit_breaker is set and
         should_halt() → log WARNING and return None.
       - Returns None without touching recently_fired (pair stays retryable).

  2.4  run_all_checks called before order submission
       - After sizing and before creating OrderLeg objects: builds a
         _RiskSignal proxy and calls `await run_all_checks(signal, self._risk_config, self.db)`.
       - On failure: logs RISK_REJECTED with failed check names, returns None.
       - Pair is NOT added to recently_fired on rejection (retryable on next tick).

  2.5  recently_fired.add deferred until after successful trade
       - In both on_price_update() and initial_sweep(), the `recently_fired.add(pair_id)`
         call was moved from BEFORE _execute_arb_trade to AFTER it returns a
         non-None trade dict.
       - Effect: circuit-breaker halts and risk check rejections leave the pair
         eligible to retry on the next price tick.

  2.6  Kelly-based position sizing replaces hardcoded min(10, 100*edge)
       - Three call sites replaced:
         · _execute_arb_trade (was line ~1402)
         · detect_single_platform_opportunities (was line ~2229)
         · detect_violations_and_trade / legacy batch path (was line ~1833)
       - Formula: compute_kelly_fraction(edge, 1.0, risk_config.kelly_fraction)
         then compute_position_size(kelly_f, bankroll, max_size=bankroll * max_position_pct)
       - Position size now scales with bankroll and is bounded by max_position_pct.

  2.7  ScheduledStrategyRunner accepts risk_config and circuit_breaker
       - Constructor gains same parameters as ArbitrageEngine.
       - New `run_one_cycle()` method extracted from run() loop:
         · Checks circuit_breaker.should_halt() first → returns [] if halted
         · Calls detect_single_platform_opportunities with risk_config
         · run() delegates to run_one_cycle() per cycle
       - Testable in isolation without a live event loop / stop_event.

  2.8  Circuit breaker and risk_config wired into main streaming loop
       - Instantiated at `cfg.risk_controls` from the existing `get_config()` call.
       - `DailyLossCircuitBreaker(db, starting_capital, max_daily_loss_pct,
         consecutive_failure_limit)` created alongside `ArbitrageEngine`.
       - Both passed to `ArbitrageEngine` and `ScheduledStrategyRunner`.

  What was NOT done (deferred)
  ─────────────────────────────
  - Full signal handler as single choke point (2.7 in the plan): would require
    Redis for the queue. Deferred. Inline checks are in both execution paths;
    two check paths exist but both enforce the same risk_config.
  - ReconciliationEngine wiring (2.5 in the plan): needs live exchange clients.
    The circuit breaker covers the daily-loss halt; reconciliation is deferred
    to Phase 7 when exchange integration is available.
  - Discord halt webhook (2.6 in the plan): circuit_breaker.should_halt() logs
    a WARNING already. Discord alerting will be wired in Phase 7 alongside the
    full invariant / alerting infrastructure.

  Gaps and watch-outs
  ────────────────────
  - run_all_checks also enforces a duplicate-signal window (default 300s).
    After Phase 2, a pair that fires and immediately passes another tick that
    would trigger it again (inside 300s) will be rejected by check_duplicate_signal
    rather than recently_fired — both mechanisms now protect it.
  - The `_RiskSignal` proxy uses `signal_id` for duplicate detection via the
    orders table (check_duplicate_signal looks at orders.submitted_at). If no
    order was actually submitted (circuit breaker halted before orders), the
    dedup window doesn't accumulate. This is correct behavior.
  - detect_violations_and_trade (legacy --once / --refresh path) uses a freshly
    instantiated `RiskControlConfig()` per trade (env-var defaults). It is not
    passed a circuit breaker — legacy paths run once and exit, so sticky halt
    semantics don't apply.

  Downstream impact
  ──────────────────
  - Phase 3 (re-arm): recently_fired semantics changed — it is now added only
    on success. Phase 3 will replace recently_fired with PairFireState entirely;
    the deferred-add behavior is the correct intermediate state.
  - Phase 4 (paper fill model): Kelly sizing will need to use portfolio_value
    (from get_portfolio_value) instead of starting_capital to account for P&L
    drift. Currently hardcoded to starting_capital as a safe default.
  - Phase 7 (invariants): add circuit_breaker.should_halt() assertion to the
    post-deploy smoke test; wire ReconciliationEngine; wire Discord alerts.

  ---
