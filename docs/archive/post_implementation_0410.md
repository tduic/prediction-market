> **Archived 2026-04-16.** Companion to `implementation_0410.md`. All eight
> phases (0–7) are complete; commit SHAs are cited inline. File paths here
> reference the v1 layout (pre-Phase-E cleanup) — many modules have since been
> deleted or moved. Kept for historical context only.

---

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
  Phase 3 — Re-arm state machine

  Status: COMPLETE

  What was implemented
  ─────────────────────
  3.1  PairFireState dataclass
       - Added after _RiskSignal at module level in paper_trading_session.py.
       - Fields: last_fired_at: float, armed: bool,
         last_spread_seen_below: float | None = None.
       - Exported at module level so tests can import it directly.

  3.2  RiskControlConfig new fields
       - arb_cooldown_s: float (default 60.0, env: ARB_COOLDOWN_S)
       - arb_rearm_hysteresis: float (default 0.005, env: ARB_REARM_HYSTERESIS)
       - Added to core/config.py after consecutive_failure_limit.

  3.3  ArbitrageEngine.fired_state replaces recently_fired set
       - self.recently_fired: set[str] removed from __init__.
       - self.fired_state: dict[str, PairFireState] = {} added in its place.
       - Tracks last_fired_at and armed status per pair.

  3.4  recently_fired backward-compatibility property
       - @property recently_fired returns set(self.fired_state.keys()).
       - stats()["recently_fired"] and all external callers work unchanged.

  3.5  _is_eligible(pair_id) method
       - Returns True if pair has never fired (not in fired_state).
       - Returns False if not armed (waiting for spread reversion).
       - Returns False if time.time() - last_fired_at < arb_cooldown_s.
       - Never-fired pairs are always eligible regardless of cooldown.

  3.6  _check_rearm(pair_id, spread) method
       - No-op if pair not in fired_state or already armed.
       - Sets state.armed = True when spread < min_spread - arb_rearm_hysteresis.
       - Called on every tick (before the min_spread guard) so re-arm happens
         even when the spread itself is below the trading threshold.

  3.7  on_price_update and initial_sweep updated
       - Both use _check_rearm before the spread check, then _is_eligible
         instead of the old recently_fired membership test.
       - Double-checked under lock (re-arm and eligibility re-evaluated after
         lock acquisition to prevent TOCTOU).
       - Exception safety: try/except around _execute_arb_trade; exceptions are
         logged and swallowed — pair is NOT added to fired_state on failure.
       - fired_state[pair_id] set to PairFireState(last_fired_at=time.time(),
         armed=False) only after a successful trade return.

  3.8  periodic_scan() async method
       - Iterates all _pairs with the same check_rearm / is_eligible / lock
         pattern as on_price_update.
       - Catches pairs whose spread opened while both prices drifted
         simultaneously (no single tick would trigger on_price_update).
       - Respects cooldown: pairs in fired_state with armed=False or within
         the cooldown window are skipped.

  Gaps and watch-outs
  ────────────────────
  - periodic_scan() is defined but not yet wired into a timer loop. Caller
    must invoke it periodically (e.g., from ScheduledStrategyRunner or a
    separate asyncio.create_task). Wire in Phase 7 or the main streaming loop.
  - arb_cooldown_s=0.0 with arb_rearm_hysteresis=0.0 gives true immediate
    re-fire behavior; useful for tests but not for production. Both env vars
    should be explicitly set in .env.
  - last_spread_seen_below on PairFireState is unused in this phase. Reserved
    for Phase 5 (strategy hygiene) to detect price oscillation patterns.
  - The recently_fired property returns ALL pairs that have ever fired
    (including re-armed pairs that are now eligible again). stats()["recently_fired"]
    counts all fired pairs, not just currently-muted ones. This is acceptable
    for the STATUS log; a separate "muted_pairs" counter could be added if needed.

  Downstream impact
  ──────────────────
  - Phase 4 (paper fill model): no overlap. Kelly sizing still uses
    starting_capital; Phase 4 will switch to live portfolio_value.
  - Phase 5 (strategy hygiene): last_spread_seen_below is a stub for
    detecting oscillating pairs. Phase 5 may populate it.
  - Phase 7 (invariants): wire periodic_scan() into the main streaming loop
    or ScheduledStrategyRunner. Add assertion that all fired_state entries
    have last_fired_at > 0 to the post-deploy smoke test.

  ---
  Phase 4 — Realistic paper fill model

  Status: COMPLETE

  What was implemented
  ─────────────────────
  4.1/4.2  Removed synthetic exit price from detect_single_platform_opportunities
       - Previously set exit_price = price ± edge (fictional).
       - Now positions are written with status='open', exit_price=NULL,
         realized_pnl=NULL, closed_at=NULL, pnl_model='realistic'.

  4.3/4.4  mark_and_close_positions(db, holding_period_s) standalone function
       - Queries positions WHERE status='open' AND opened_at <= cutoff.
       - For each expired position: fetches latest market_prices.yes_price,
         computes realized_pnl, UPDATEs status='closed'.
       - Positions with no price data are left open (stale handling).
       - Returns count of positions closed.

  4.5  Slippage model in PaperExecutionClient
       - New params: slippage_bps: float = 0.0 (backward compat), fee_rate: float | None.
       - BUY: filled = market * (1 + slippage_factor * uniform(0,1)), capped at 0.99.
       - SELL: filled = market * (1 - slippage_factor * uniform(0,1)), floored at 0.01.

  4.6  Fee rate configurable via fee_rate param on PaperExecutionClient.

  4.7  pnl_model column and migration 007_phase4_fill_model.sql
       - ALTER TABLE positions ADD COLUMN pnl_model TEXT DEFAULT 'realistic'.
       - Existing closed positions back-tagged as pnl_model='synthetic'.

  4.8  RiskControlConfig: slippage_bps=10.0, strategy_holding_period_s=300.

  4.9  ScheduledStrategyRunner.run_one_cycle() calls mark_and_close_positions
       before each new cycle.

  What was NOT done (deferred)
  ─────────────────────────────
  - Dashboard split (synthetic vs realistic PnL) — deferred to Phase 7.
  - Backfill recomputation of existing synthetic positions — deferred.
  - P1 arb (_execute_arb_trade) unchanged — two-sided arb PnL is realized at fill.
  - Stop-loss and resolution-based exits — deferred to Phase 5/7.
  - Partial-fill probability — not implemented.

  Gaps and watch-outs
  ────────────────────
  - With slippage_bps=0 (current default), fills are at exact market price.
    Set SLIPPAGE_BPS=10 in .env for realistic slippage.
  - Orphaned positions (no price ever) accumulate as open forever.
    Wire cleanup in Phase 7.
  - PnL sanity cap no longer guards single-platform strategies (no realized_pnl
    at open). Cap still guards P1 arb.

  Downstream impact
  ──────────────────
  - Phase 5 (strategy hygiene): P3/P4/P5 now produce realistic open positions.
    Win rate will drop to plausible values. Phase 5 should improve signal
    quality against this new baseline.
  - Phase 7 (invariants): add open-position age assertion; wire dashboard split.

  ---
  Phase 5 — Strategy hygiene

  Status: COMPLETE

  What was implemented
  ─────────────────────
  5.1  Cross-strategy dedup (_cross_strategy_dedup)
       - Keeps only the highest signal_strength entry per market_id across all
         strategies before quota allocation.
       - Prevents the same market from being traded simultaneously under two
         different strategy labels.
       - Important overlap: P3's trigger zone (|price−0.50|>0.20) encompasses
         P5's trigger zone (price<0.25 or >0.75) entirely, and partially covers
         P4's lower zone (0.15–0.30) and upper zone (0.70–0.85). P3 therefore
         always beats P4/P5 in dedup when both fire. P4-only prices: 0.30–0.35
         and 0.65–0.70. P5 has no exclusive price range.

  5.2  Consecutive-cycle dedup
       - Skips any market_id that has an open position OR a recent closed
         position (within strategy_replay_cooldown_s) unless yes_price has
         moved ≥ strategy_replay_min_move (default 1%) since entry.
       - Prevents re-entering the same position in every scan cycle.

  5.3  Z-score normalization (_normalize_signal_strengths)
       - Adds signal_strength_normalized field to each opportunity.
       - Computed per strategy bucket; single-item buckets → 0.0.
       - Downstream: not yet used for routing/sizing but available for Phase 7
         invariant logging.

  5.4  Per-strategy enable flags
       - strategy_p2_enabled, strategy_p3_enabled, strategy_p4_enabled,
         strategy_p5_enabled added to RiskControlConfig (default True).
       - Filtered before dedup so disabled strategies are invisible to all
         downstream stages.

  5.5  (not in original plan — no step 5.5 was specified)

  5.6  Per-strategy kill-switch
       - _get_strategy_rolling_pnl(db, strategy, window_s) counts closed
         realistic positions and sums realized_pnl for a given window.
       - If count ≥ strategy_killswitch_min_trades and pnl < 0, the strategy
         is disabled for this cycle with a WARNING log.
       - Config: strategy_killswitch_window_s=604800 (7 days),
         strategy_killswitch_min_trades=5.

  New config fields (RiskControlConfig)
       - strategy_replay_cooldown_s=300
       - strategy_replay_min_move=0.01
       - strategy_p2_enabled=True
       - strategy_p3_enabled=True
       - strategy_p4_enabled=True
       - strategy_p5_enabled=True
       - strategy_killswitch_window_s=604800
       - strategy_killswitch_min_trades=5

  Test helpers updated
       - test_phase2_risk_choke._risk_config() and test_phase4_fill_model._risk_config()
         both updated with all Phase 5 fields to prevent AttributeError when
         run_one_cycle() invokes detect_single_platform_opportunities.

  Test regressions fixed (test_single_platform_strategies.py)
       - P4 zone tests: prices moved to P4-exclusive range (0.30–0.35 / 0.65–0.70)
         where P3 does not fire (distance ≤ 0.20).
         · test_lower_zone_buy: 0.20 → 0.32
         · test_upper_zone_sell: 0.75 → 0.68
       - P5 zone tests: P5's price range is a strict subset of both P3 and P4
         zones, so P5 can only be observed when both are disabled via risk_config.
         · test_wide_spread_low_price_buy, test_wide_spread_high_price_sell,
           test_edge_is_30_pct_of_spread: pass
           RiskControlConfig(strategy_p3_enabled=False, strategy_p4_enabled=False)
       - test_all_strategies_represented: restructured as two calls:
         (a) default config verifies P3 + P4 + P2; (b) post-first-call with P3/P4
         disabled verifies P5 (P5 market seeded after first call to avoid 5.2 dedup).

  What was NOT done (deferred)
  ─────────────────────────────
  - signal_strength_normalized is computed but not yet used for sizing.
    Kelly fraction still uses raw edge. Wire in Phase 7.
  - P5's structural overlap with P3/P4 means it fires far less often in practice.
    Consider widening P5's spread threshold or narrowing P3's zone in production.
  - Kill-switch reset mechanism not implemented: once triggered it only clears
    on the next cycle (new pnl sum). Persistent flags would need a separate table.

  Gaps and watch-outs
  ────────────────────
  - The 5.2 consecutive-cycle dedup relies on the positions table having FK-valid
    signal_id rows. Tests that call detect_ standalone (without _seed_signals)
    bypass this correctly because the DB starts empty.
  - 5.2 dedup uses entry_price from positions.entry_price (the filled price, not
    the signal price). Slippage can make this slightly different from yes_price.
    The move threshold comparison (abs(cp - ep) / ep) is therefore approximate.
  - Kill-switch uses pnl_model='realistic' to exclude synthetic back-fill. If
    pnl_model is NULL (old rows), they are excluded from the count — correct.

  Downstream impact
  ──────────────────
  - Phase 6 (pre-live gating): strategy enable flags and kill-switch provide two
    of the three levers needed for live gating; Phase 6 adds execution_mode guard.
  - Phase 7 (invariants): wire signal_strength_normalized into the daily PnL
    snapshot; add per-strategy kill-switch state to the STATUS log.

  ---
  Phase 6 — Pre-live gating and safety arming

  Status: COMPLETE

  What was implemented
  ─────────────────────
  New module: core/live_gate.py

  6.1  Sentinel file guard
       - SENTINEL_PATH = Path("/etc/predictor/ARMED_FOR_LIVE")
       - check_live_gate(execution_mode) raises LiveGateError if mode is 'live'
         and the sentinel file does not exist.
       - Error message includes the full path and the sudo command to create it.
       - Non-live modes (paper, mock, shadow) are no-ops.

  6.2  Daily confirmation code
       - check_live_gate also requires LIVE_CONFIRMATION_CODE env var to equal
         today's ISO date (YYYY-MM-DD) when mode is 'live'.
       - If confirmation_code is not passed as an argument, os.getenv() is used.
       - Wrong date: error shows both expected and actual values.
       - Forces manual env update each day; stale deploys with old dates fail fast.

  6.3  Stricter live risk config
       - get_effective_risk_config(execution_mode) → RiskControlConfig
       - Live defaults: max_position_pct=0.02 (vs paper 0.05), max_daily_loss_pct=0.01
         (vs paper 0.02), min_edge=0.05 (vs paper 0.02).
       - Each value overridable via LIVE_MAX_POSITION_PCT, LIVE_MAX_DAILY_LOSS_PCT,
         LIVE_MIN_EDGE_TO_TRADE env vars.
       - All other RiskControlConfig fields use standard env-var defaults.
       - shadow/mock/paper all return standard RiskControlConfig().

  6.4  Shadow execution mode
       - "shadow" added as a valid EXECUTION_MODE (config validation updated).
       - _make_execution_clients and _make_single_execution_client treat "shadow"
         as "paper": PaperExecutionClient is returned with a SHADOW log message.
       - No sentinel file or confirmation code required for shadow.
       - Intended use: run against real WS data for ≥1 week before flipping to live.

  Startup wiring
       - check_live_gate(cfg.execution.execution_mode) called in main() immediately
         after get_config(). Fails fast before DB init for live mode without gate.

  What was NOT done (deferred)
  ─────────────────────────────
  - get_effective_risk_config is not yet called at runtime — it exists but is not
    wired into ScheduledStrategyRunner or ArbitrageEngine. They still pull risk
    config from get_config().risk_controls. Wire in Phase 7 alongside the
    invariant checks.
  - Shadow mode does not suppress DB writes (positions still written to paper DB).
    If true no-DB-write shadow mode is needed, add an execution_mode check at the
    write sites or a flag on PaperExecutionClient.
  - No Discord alert for live gate failure — LiveGateError logged at startup is
    sufficient pre-live; post-live alerting belongs in Phase 7.

  Gaps and watch-outs
  ────────────────────
  - /etc/predictor/ARMED_FOR_LIVE must be owned by root and not world-writable.
    The deploy script must not touch this path.
  - LIVE_CONFIRMATION_CODE must be set fresh each day. Add to deploy runbook.
  - Live credentials (POLYMARKET_PRIVATE_KEY, KALSHI_API_KEY, etc.) are still
    validated at Config.__post_init__ when mode is live — those checks remain.

  Downstream impact
  ──────────────────
  - Phase 7 (invariants): wire get_effective_risk_config into the runtime risk
    checks so live mode automatically enforces tighter limits per trade.
  - Phase 7: add SENTINEL_PATH existence assertion to smoke test.

  ---
  Phase 7 — Invariants, alerting, and continuous verification

  Status: COMPLETE (core items)

  What was implemented
  ─────────────────────
  New module: core/invariants.py
  New migration: 008_phase7_invariants.sql

  7.1  Invariant assertion module
       Five invariant checks, each returning InvariantResult(name, passed, message):

       check_pnl_sanity(db)
         - Fails if any closed position has realized_pnl > entry_size * 1.001.
         - Physical impossibility: max profit on a binary is the full stake.

       check_position_duration(db)
         - Fails if any closed position has closed_at <= opened_at.
         - Instant open/close is a coding error in mark_and_close_positions.

       check_orphan_positions(db)
         - LEFT JOIN positions → signals; fails if any position lacks a signal row.
         - Catches FK bypass during migrations or bulk loads.

       check_fee_ratio(db)
         - Fails if sum(fees_paid) / sum(entry_size * entry_price) > 20%.
         - Catches bps-vs-fraction confusion in fee calculation.

       check_engine_state(arb_engine)
         - Synchronous; fails if len(fired_state) > len(_pairs).
         - Ghost entries in fired_state indicate a memory leak.

       check_all_invariants(db, arb_engine=None, mode="warn", alert_manager=None)
         - Runs all checks concurrently (asyncio.gather for DB checks).
         - mode="warn": logs + persists failures, never raises.
         - mode="halt": raises InvariantViolation on first failure.
         - On failure: inserts into invariant_violations table.
         - On failure: calls alert_manager.send() with severity=CRITICAL.

       Rule: start with mode="warn" for at least one week before promoting to "halt."

  7.3  Discord alerting on invariant violation
       - check_all_invariants accepts alert_manager parameter.
       - Wired to get_alert_manager() when called from run_one_cycle.
       - (Currently run_one_cycle calls without alert_manager — wire in next deploy.)
       - Existing AlertManager (core/alerting.py) handles dedup + rate limiting.

  7.5  Dashboard endpoints
       /api/pnl-split
         - Returns {realistic: {trade_count, total_pnl, total_fees},
                    synthetic: {trade_count, total_pnl, total_fees}}.
         - Groups closed positions by pnl_model column (Phase 4 addition).
         - NULL pnl_model treated as 'synthetic' for backward compat.

       /api/invariants
         - Returns {violation_count: int, recent_violations: [...]}.
         - Queries invariant_violations table, ordered by violated_at DESC.
         - Limit param (1–100, default 20).

  Scheduler wiring
       - ScheduledStrategyRunner.run_one_cycle() calls check_all_invariants(db,
         mode="warn") before detect_single_platform_opportunities.
       - Invariants run every strategy cycle (~every 120s in production).

  What was NOT done (deferred)
  ─────────────────────────────
  7.2  Golden-path integration test (WS feed playback) — deferred; requires
       a recorded tick stream and event-time compression infrastructure.
  7.4  Post-deploy smoke test script — deferred; needs production deploy runbook.
  - get_effective_risk_config not yet wired into runtime risk checks.
  - alert_manager not yet passed to check_all_invariants in run_one_cycle.
    Wire after confirming no spurious violations from warn-mode week.
  - invariant_violations table not cleared automatically — ops must archive.

  Gaps and watch-outs
  ────────────────────
  - check_pnl_sanity only covers closed positions. Open positions can have
    unrealized_pnl = None (not yet marked to market). The check will miss
    inflated unrealized PnL; add once mark-to-market is more frequent.
  - check_position_duration uses ISO string comparison. If timezones differ
    between opened_at and closed_at, the comparison can be incorrect. All
    timestamps should use UTC-aware isoformat (enforced in mark_and_close).
  - Fee ratio check aggregates across ALL strategies. Add per-strategy
    breakdown once there's enough trade volume to make it meaningful.

  Downstream impact
  ──────────────────
  - Wire alert_manager=get_alert_manager() in run_one_cycle after confirm
    no spurious violations in production warn mode.
  - Promote mode="halt" for check_pnl_sanity and check_orphan_positions
    first (tightest invariants); leave duration/fee checks in warn mode longer.

  ---
