> **Archived 2026-04-16.** This is the original Phase 0–7 bulletproofing plan
> (written 2026-04-10). All phases have since landed — see
> `post_implementation_0410.md` for the per-phase completion notes. File paths
> referenced here (e.g. `paper_trading_session.py`, `execution/handler.py`,
> `core/constraints/`) describe v1 and no longer exist in the current tree.
> Kept for historical context only.

---

  Implementation plan: bulletproof the predictor trading system

  Summary of findings across all five audits

  Every audit converged on the same structural problem: the system is producing synthetic PnL, not real signal,
  and the primary execution path (paper_trading_session.py) bypasses the risk/sizing/halt infrastructure
  entirely.

  ┌──────────────────────────────────────────────────────────────────────────────────────┬──────────┬────────┐
  │                                       Finding                                        │ Severity │ Source │
  ├──────────────────────────────────────────────────────────────────────────────────────┼──────────┼────────┤
  │ Matcher produces 100% false positives on current cache (38/38 are O/U X.5 ↔ N+       │          │ Agent  │
  │ mismatches; root cause: decimal point stripped during normalization before number    │ P0       │ 1      │
  │ extraction, scripts/paper_trading_session.py:647–666)                                │          │        │
  ├──────────────────────────────────────────────────────────────────────────────────────┼──────────┼────────┤
  │ Paper client fills deterministically at requested price with zero slippage           │          │        │
  │ (execution/clients/paper.py:156–158); exit_price for scheduled strategies is         │ P0       │ Agent  │
  │ hardcoded as entry ± edge (paper_trading_session.py:2076–2079); positions open and   │          │ 2      │
  │ close in the same atomic DB write                                                    │          │        │
  ├──────────────────────────────────────────────────────────────────────────────────────┼──────────┼────────┤
  │ ArbitrageEngine.open_positions set is added to but never removed; exception inside   │          │        │
  │ _execute_arb_trade leaks the pair into the mute set permanently; network calls are   │ P0       │ Agent  │
  │ awaited inside_trade_lock, starving concurrent ticks                                │          │ 3      │
  │ (paper_trading_session.py:1261–1271)                                                 │          │        │
  ├──────────────────────────────────────────────────────────────────────────────────────┼──────────┼────────┤
  │ All four scheduled strategies (P2/P3/P4/P5) share the synthetic-exit pattern;        │          │ Agent  │
  │ holding_period_ms = 5000 is hardcoded fiction; no dedup against markets traded in    │ P0       │ 4      │
  │ prior cycle (paper_trading_session.py:1802–2177)                                     │          │        │
  ├──────────────────────────────────────────────────────────────────────────────────────┼──────────┼────────┤
  │ ArbitrageEngine and scheduled strategies bypass run_all_checks(),                    │          │        │
  │ DailyLossCircuitBreaker, and ReconciliationEngine entirely; hardcoded size =         │ P0       │ Agent  │
  │ round(min(10.0, 100.0 * edge), 1) ignores MAX_POSITION_PCT, KELLY_FRACTION, and      │          │ 5      │
  │ portfolio exposure limits                                                            │          │        │
  └──────────────────────────────────────────────────────────────────────────────────────┴──────────┴────────┘

  These are interlocking. Fixing any one in isolation leaves leakage behind: fixing the matcher without fixing
  the re-arm still produces phantom bursts; fixing re-arm without fixing the paper client just lets phantom PnL
  accumulate faster; fixing the paper client without routing through the risk layer means losses are still
  unbounded. The plan is therefore phased, with each phase either providing observability or plugging a specific
  leak, and with a clear "system is safe to run without supervision" gate at the end.

  ---
  Guiding principles

  1. No new features until the accounting is trustworthy. Do not chase strategy improvements while reported PnL
  is 50–70% synthetic. Every hour spent tuning P3 on fake PnL is wasted.
  2. Every execution path must go through one choke point. Right now there are two parallel worlds: the signal
  handler (with all the checks) and the ArbEngine/scheduled scanner (with none). Consolidate to one.
  3. Invariants should be asserted in code, not enforced by developer discipline. If "position must hold for >0
  ms" is a rule, it should be an assertion that crashes the service, not a documented expectation.
  4. Paper mode should be a realistic shadow of live mode, not a perfect oracle. If paper mode is profitable only
   because the fills are fiction, live mode will not be profitable.
  5. Add observability before adding logic. The STATUS line currently lies about open_positions. Fix what the
  system reports before fixing what it does — debugging the fix without honest telemetry is guesswork.

  ---
  Phase 0 — Stop the bleeding and get honest telemetry (today, ~2 hours)

  Goal: before touching the matcher, re-arm, or fill model, make the system's state observable so every later fix
   can be verified.

  0.1 STATUS log truthfulness. Replace stats() at paper_trading_session.py:1496 with fields that reflect reality
  post-rearm: pairs_monitored, pairs_eligible_now (spread ≥ threshold this instant), pairs_muted,
  last_arb_fired_at, ticks_since_last_fire, ws_last_tick_age_ms_by_platform. Rename open_positions →
  recently_fired inside the engine to end the name confusion.

  0.2 Sanity-cap synthetic PnL immediately. Add a sentinel assertion at the DB position-insert sites
  (paper_trading_session.py ArbitrageEngine _execute_arb_trade around 1404, single-platform around 2090) that
  rejects inserts where realized_pnl > size × 0.10. Current P1 trades are booking 0.4 spread × size → would get
  caught; legitimate arb trades shouldn't come close. This is a circuit breaker on fake profit, not a fix; it
  buys time to do the rest properly.

  0.3 Pause P1. Set min_spread to a value so high that the ArbitrageEngine produces zero trades until the matcher
   is fixed. The cached 38 pairs are 100% false positives per Agent 1; any trade the engine fires is bad data.
  Use config, not a code edit.

  0.4 Snapshot current state into a file/DB table: pair count, strategy pnl breakdown, current match cache. This
  is your baseline — every future metric comparison needs to compare against Phase 0.

  Edge cases:

- The sanity cap in 0.2 might spuriously reject a legitimate high-edge trade. Log the rejection with full
  context; tune the threshold after observing.
- If 0.3 uses a config knob that isn't hot-reloaded (Agent 5 confirms nothing is), a restart is required.
  Accept the ~5s downtime.
- The STATUS rename will break any external grep/alert that matches on open_positions. Grep the repo for log
  consumers before renaming.

  Verification: New STATUS line appears in journal; pairs_eligible_now is a live number; realized_pnl_total stops
   growing from P1.

  ---
  Phase 1 — Fix the matcher at the root (1–2 days)

  Goal: stop the production of false-positive pairs. Agent 1's root cause is precise and actionable.

  1.1 Root cause fix. In scripts/paper_trading_session.py around line 664 (extract_numbers), extract numbers from
   the raw title before normalization strips the decimal point. Agent 1 verified: currently "O/U 5.5" normalizes
  to "o u 5 5", then regex \b\d+\.?\d*\b finds {'5'} — identical to the Kalshi {'5'} from "5+", so the numeric
  penalty never triggers. Running extract_numbers on the original title first preserves {'5.5'} vs {'5'}, the
  penalty fires, and similarity drops below 0.80.

  1.2 Strengthen the numeric penalty. The current −0.3 penalty (line 707–708) is too weak when other signals are
  strong. For the specific class "both titles contain threshold terminology (O/U, ≥, at least, N+) and the
  extracted numeric sets differ", apply a hard reject (return 0.0 similarity) rather than a soft penalty. Raw
  token overlap and SequenceMatcher alone should never be able to match a 5.5 O/U to a 5+ threshold.

  1.3 Semantic preflight rule. Before scoring any pair, run a cheap regex check: if exactly one side contains
  \bO/U\b or over/under and the other contains \b\d+\+\b or \bat least \d+\b or \b\d+ or more\b, reject outright.
   Agent 1 rates this at 85% confidence. Combine with 1.1 for defense in depth.

  1.4 Spread sanity cap on new pairs. When persisting a match to market_pairs, if the current observed spread
  exceeds 0.05 (Agent 1's "5% rule"), mark the pair as pending_review instead of active. A true cross-platform
  arb on the same event rarely has a spread > 2–3%; anything wider is suspicious by default. Human review
  required to activate. This is an orthogonal safety net that protects against future matcher bugs we haven't
  thought of.

  1.5 Backfill the existing DB. Mark all 38 currently cached pairs as active=0 (or pending_review). Do not delete
   — keep them for later comparison. When the new matcher runs, it will produce a fresh set.

  1.6 Re-run matching once, carefully. Trigger refresh_markets_and_matches on a copy of the DB in a shell,
  inspect the output, and only when manually satisfied, point production at the new cache. This is a one-time
  supervised operation, not a continuous process.

  Edge cases:

- A legitimate pair like "Will 2026 be the hottest year on record?" on both platforms has no numeric thresholds
   and should still match. Verify with regression cases before deploying.
- Markets with multiple numbers in the title ("Smith 5+ assists, 3+ steals"): numeric set {5,3} could still
  produce false matches. Test.
- Range markets ("S&P between 4000–4200" vs "S&P at 4100") — Agent 1 flagged these as a latent class. Phase 1
  fix may not catch them. Accept this and cover via the 5% spread cap in 1.4.
- Time-window mismatches ("Q1 2025" vs "Q2 2025") — the numeric penalty should now catch these since {1,2025}
  vs {2,2025} triggers the penalty under the stronger rule in 1.2.
- Inverted logic ("Trump wins" vs "Trump loses") — the matcher has NO semantic guard for this. Logged as an
  open issue; not in scope for Phase 1. Mitigated by the 5% spread cap (inverted markets will have spread ~1.0).

  Verification: Re-running the matcher on current market data produces an empty or very small cache. Any pair it
  does produce passes manual inspection. Zero O/U-vs-N+ pairs.

  ---
  Phase 2 — Route all execution through one risk choke point (2–3 days)

  Goal: eliminate the bypass. After this phase, every trade regardless of source goes through run_all_checks, the
   circuit breaker, and config-driven sizing.

  2.1 Construct a signal object inside _execute_arb_trade (not a DB insert yet). Use the same
  Signal/ViolationDetected shape as core/signals/generator.py produces. Populate strategy, edge, kelly_fraction,
  position_size_a, total_capital_at_risk, fired_at.

  2.2 Call run_all_checks(signal, risk_config, db) before any order is submitted. If any check fails: log
  structured rejection, return without executing, do NOT add the pair to the recently-fired set (so it can retry
  on next legitimate tick). Same pattern inside detect_single_platform_opportunities for scheduled strategies.

  2.3 Replace hardcoded sizing with compute_position_size from core/signals/sizing.py. Pass max_size = bankroll *
   cfg.max_position_pct. Remove every instance of size = round(min(10.0, 100.0* edge), 1) — Agent 5 found three.
   Kelly must come from compute_kelly_fraction using cfg.kelly_fraction, not the ad-hoc min(edge/0.50, 0.25)
  (which is dimensionally wrong — that's edge divided by a price, producing garbage units).

  2.4 Instantiate circuit breaker in the streaming loop. Construct DailyLossCircuitBreaker and
  ReconciliationEngine alongside the ArbitrageEngine (around paper_trading_session.py:2609). Gate every
  trade-attempt path on await circuit_breaker.should_halt(). On halt: stop all new trades, continue to serve
  dashboard and scheduled strategies' signal generation (for observability), but do not execute.

  2.5 Wire the reconciliation engine into the streaming loop so it runs once per status cycle. Agent 5 notes it
  exists but is orphaned. Its job is to detect state drift between in-memory, DB, and (eventually) exchange
  balances.

  2.6 Halt propagation. When the circuit breaker fires, emit a structured log event AND post to the Discord
  webhook (/data/predictor/.env → DISCORD_WEBHOOK_URL). This is the first line of human-visible alerting.

  2.7 Kill the dead signal path. Either wire the signal handler into the paper trading session or delete it.
  Having two paths — one with checks, one without — was exactly the bug. Pick one. My recommendation: use the
  signal handler as the single choke point; the ArbitrageEngine and scheduled scanners become producers that
  queue signals, and the handler is the consumer that validates and executes.

  Edge cases:

- If run_all_checks is slow (DB queries), calling it per tick could throttle the arb engine. Profile it. If
  needed, keep a short cache (last 1s) of a check result keyed by strategy + market.
- _trade_lock must NOT be held across the run_all_checks and network calls — this is Agent 3's "Risk D" latency
   bug. Move the lock to guard only the state mutation, not the IO. Specifically: check dedup → acquire lock →
  re-check dedup → mutate set → release lock → then run checks → then submit orders → then write DB. If order
  submission fails, remove from the dedup set inside a second brief lock acquisition.
- If the signal handler is on a separate asyncio queue from the ArbEngine, watch for ordering issues when
  multiple strategies fire on the same market within one tick.
- Config reloads mid-session: decide whether to support hot reload or require restart. Agent 5 says nothing is
  hot-reloadable today. Defer unless required.

  Verification: New position inserts include a non-null reference to a signal record that passed run_all_checks.
  DB invariant: positions.signal_id JOIN to signals must match, and every signal must have passed all risk checks
   (new column or table risk_check_passes). Manual test: set max_position_pct = 0.001; confirm every new trade
  gets rejected with a logged reason.

  ---
  Phase 3 — Re-arm state machine for the arbitrage engine (1–2 days)

  Goal: fix the one-shot-per-pair behavior correctly. Agent 3 recommended Design C (cooldown AND
  spread-reversion); I agree.

  3.1 Replace open_positions: set[str] with a structured dict:

- fired_state: dict[pair_id, PairFireState] where PairFireState holds last_fired_at: datetime, armed: bool,
  last_spread_seen_below: datetime | None.

  3.2 Fire conditions. A pair is eligible to fire if and only if ALL of:

  1. Current spread ≥ min_spread
  2. now - last_fired_at >= cfg.arb_cooldown_s (new config knob, default 60s)
  3. armed is True (was below min_spread - hysteresis at some point since last fire, or never fired)
  4. run_all_checks passes (from Phase 2)

  3.3 Re-arm trigger. On any tick, if spread < min_spread - hysteresis (new config arb_rearm_hysteresis, default
  0.005), set armed = True and record last_spread_seen_below = now.

  3.4 Initial sweep compatibility. On startup, set armed = True for all pairs (they've never fired). Set
  last_fired_at = epoch so the cooldown check trivially passes. The initial_sweep can then fire normally.

  3.5 Exception safety. If _execute_arb_trade raises after mutating fired state, wrap in try/finally that rolls
  back the state changes. Agent 3's "Edge case 8": pair gets permanently muted on exception. Fix.

  3.6 Lock placement correction. Move the lock scope from "around the entire fire attempt including IO" to
  "around state reads/writes only". The order submission and DB write must happen outside the lock. This is Agent
   3's Risk D.

  3.7 WS reconnect safety. On reconnect, the WS replay of subscriptions will re-deliver prices for all markets.
  The early-return at line 1234 (abs(new_price - old_price) < 0.001) mostly handles this — but if a pair has
  drifted since disconnect, it will re-evaluate. With the new armed/cooldown state, this is safe: a pair won't
  re-fire unless it genuinely re-opened.

  3.8 Drift guard. Agent 3 raised the case where a pair gradually crosses the threshold with ticks smaller than
  the 0.001 early-return. Fix: also compare new_price to a "last checked" baseline per market, not just the
  previous tick. Or cheaper: periodically (every 10s) re-scan all pairs regardless of tick deltas. I recommend
  the periodic scan — it's simpler and catches all drift scenarios.

  Edge cases (beyond Agent 3's list):

- Pair with only one leg receiving ticks: the unvisited leg stays at seed price from match data. Over time,
  seed price is very stale. Mitigation: stale-price rejection — if the leg's price is from match_data and older
  than 60s, skip the pair and emit a warning counter.
- Cooldown change via config reload: existing timers use the old cooldown until their next trigger. Accept this
   — it's a transient inconsistency.
- Pair oscillates exactly at threshold: with hysteresis of 0.005, any pair that oscillates within [0.025,
  0.035] will arm/disarm but not fire. Correct.
- Match data refresh replaces pair list: use _pairs.keys() - new_pairs.keys() to drop orphaned fired_state
  entries, and initialize new ones with armed=True.

  Verification: Unit test with a synthetic WS feed of 100 price updates; assert that a pair fires → is muted →
  becomes eligible after reversion → fires again. Assert that a pair with no reversion never re-fires even after
  the cooldown elapses. Assert that an exception during _execute_arb_trade leaves fired_state unchanged.

  ---
  Phase 4 — Realistic paper fill model (3–5 days)

  Goal: stop the system from booking fake profit. This is the biggest behavioral change and requires care.

  4.1 Remove the synthesized exit price. paper_trading_session.py:2076–2079 currently sets exit_price = price ±
  edge. This is fiction. Delete it. The scheduled-strategy code path must be restructured so that position
  opening and closing are separate events in time.

  4.2 Positions must actually hold. A new BUY/SELL writes a row with status='open', exit_price=NULL,
  realized_pnl=NULL, and closed_at=NULL. No atomic open-and-close. The position stays open until a separate
  mark-to-market / exit decision closes it.

  4.3 Mark-to-market engine. A new background task (or extension of the existing ScheduledStrategyRunner) walks
  open positions every N seconds, queries the current WS price (or latest market_prices row), and updates
  unrealized_pnl = (current_price - entry_price) * size for BUY (negated for SELL). This is not the exit — it's
  mark-to-market for reporting.

  4.4 Exit logic. Each strategy needs an exit rule. Options:

- Time-based: close after N seconds (P4 liquidity timing style)
- Price-target: close when mid reaches some target
- Stop-loss: close when mid drops below entry − stop_pct
- Resolution-based: close when the underlying market resolves (requires ingestor integration)

  Start simple: every strategy gets a time-based holding period from config. P3/P4/P5: close after
  cfg.strategy_holding_period_s (e.g. 300s) at the then-current mid. P2 (structured event): close when the
  structural inconsistency closes (sum of YES drops below 1.01) or at a max holding period.

  4.5 Slippage model in paper client. execution/clients/paper.py:156 currently fills at the requested price. Add
  a slippage parameter to the paper client:

- Buy fills: filled_price = limit_price *(1 + slippage_bps* random) clamped to [limit_price, best_ask]
- Sell fills: symmetric
- slippage_bps comes from config and defaults to a realistic value (Polymarket ~5bps on liquid markets, 25bps
  on thin; Kalshi similar).
- Add a partial-fill probability for large sizes relative to market_prices.liquidity.

  4.6 Fee model truthfulness. Agent 2 flagged that 2%/7% rates are likely wrong. Check actual Polymarket and
  Kalshi public fee docs, update defaults. Keep the rates configurable.

  4.7 Backfill PnL recomputation. The existing positions table has ~1340 trades with phantom PnL. Add a flag
  pnl_model='synthetic' to all existing rows, then recompute their PnL under the new model as a side experiment
  (store in positions_realistic_pnl column). Do NOT delete the old data — keep it for comparison.

  4.8 Dashboard split. The dashboard should report two numbers side by side: "Booked PnL (synthetic fill model —
  legacy)" and "Realistic PnL (with slippage + holding)". This is the single most important visualization for the
   user during the transition. Once the user is convinced the realistic PnL is trustworthy, the synthetic number
  can be retired.

  Edge cases:

- Mark-to-market requires a current price for every open market. If WS hasn't delivered a tick for a given
  market, mark stays stale. Emit a metric for max-stale-mark-age.
- Exit at time-based holding may hit a market that's illiquid or closed (resolved). Need graceful handling — if
   market is resolved, exit at the resolution price; if illiquid, exit at last-known mid and log.
- Stop-losses under a synthetic fill model become lossy quickly. Set a sane default (e.g. 5%) and monitor.
- Backfill recompute is expensive but one-shot. Run offline.
- The invariant in Phase 0 (realized_pnl <= size * 0.10) will still apply and may block legitimate outlier wins
   — relax once realistic fills are trusted.

  Verification: Realistic-model PnL on the existing trade set is materially lower than booked — expected, and the
   delta is the leakage estimate. Win rate drops from 95% to something plausible (~50–60%). Average holding
  period is >0. Spot-check specific trades for believability.

  ---
  Phase 5 — Strategy hygiene and signal quality (ongoing)

  Goal: the strategies themselves. Only worth tackling after Phase 4; working on them before is polishing brass
  on a sinking ship.

  5.1 Cross-strategy market dedup. detect_single_platform_opportunities can currently fire multiple strategies on
   the same market in one cycle. Add a post-selection dedup: after sorting and capping, if the same market_id
  appears twice, keep only the highest signal_strength entry. Agent 4 recommended this.

  5.2 Consecutive-cycle dedup. A market traded in cycle N is currently eligible again in cycle N+1 with zero
  intervening price change. Add a DB lookup: reject any market that was traded within the last
  cfg.strategy_replay_cooldown_s (e.g. 300s) unless the price has moved more than cfg.strategy_replay_min_move
  (e.g. 1%).

  5.3 Per-strategy quota and signal_strength normalization. Agent 4 confirmed the new code allocates per-strategy
   quotas (15/50/25/remainder for P2/P3/P4/P5 of the 20-slot budget). This is okay but the signal_strength
  metrics are not normalized across strategies. Normalize within each strategy (z-score or percentile), then rank
   within each bucket. Don't try to compare raw signal strengths across strategies.

  5.4 Kill P3 and P4. Controversial recommendation. Agent 4 rates them LOW confidence under realistic fills —
  both assume 0.50 is the fair-value midpoint, which is wrong for tail events. After Phase 4, if P3/P4 show
  negative realistic PnL, disable them via a strategy enable flag rather than tuning. P2 is LOW confidence also
  but more structurally defensible. P5 is the only strategy Agent 4 rated as "MEDIUM-HIGH" — it's the closest to
  real signal.

  5.5 Ground strategies in actual models. For strategies that continue: replace the hardcoded edge = distance *
  0.04 type formulas with either a trained model output (via core/models/) or a confidence-interval model. The
  current formulas are numerology.

  5.6 Strategy kill-switches. Per-strategy enable flag in config, per-strategy circuit breaker (halt strategy if
  rolling 7-day realistic PnL is negative). Agent 5 flagged this as missing.

  Edge cases:

- Strategy replay cooldown could prevent legitimate re-entry after a genuine price move. Tune.
- Per-strategy circuit breaker requires realistic PnL to be trustworthy — so 5.6 depends on Phase 4 landing
  first.

  Verification: Per-strategy PnL tracked in a time series; dashboard shows realistic PnL per strategy; disabled
  strategies produce zero trades; re-enabling works via config change + restart.

  ---
  Phase 6 — Pre-live gating and safety arming (1 day)

  Goal: make it impossible to accidentally flip to live mode. Agent 5 identified this as P0 for pre-live.

  6.1 Live-mode sentinel file. Require /etc/predictor/ARMED_FOR_LIVE to exist as a condition for running with
  EXECUTION_MODE=live. Absence → refuse to start in live mode. File must be created manually with root privilege,
   is not part of any deploy tarball, and documents the date/operator who armed it.

  6.2 Live-mode confirmation on startup. Require an env var LIVE_CONFIRMATION_CODE=<today's date in YYYY-MM-DD>
  that must match the current date. Any deploy that leaves an old value in the env will fail to start. Forces
  someone to update it daily.

  6.3 Stricter risk config for live. Separate config section live_risk: that overrides paper defaults with
  stricter values — lower max_position_pct, lower daily_loss_limit, higher min_edge_to_trade. The code should
  automatically select which section based on EXECUTION_MODE.

  6.4 Dry-run mode. Add a mode between paper and live: shadow — the system generates signals, runs all risk
  checks, logs what it would submit, but does NOT call exchange APIs. This is how you would run the new realistic
   fill model against the live API for one week before flipping to real money.

  Edge cases:

- Someone edits the sentinel file name expecting a subtle rename — the deploy script should not touch this
  path.
- The date confirmation code will create friction. That's the point.

  Verification: Attempting to start in live mode without the sentinel fails fast with a clear error. Attempting
  to start with wrong confirmation code fails. Shadow mode produces logs but no orders.

  ---
  Phase 7 — Invariants, alerting, and continuous verification (ongoing)

  Goal: turn "these rules should hold" into "the system crashes if they don't."

  7.1 Invariant assertion module. Create a new module that imports a list of invariants from every other module.
  Run on every status cycle and at every DB write site. Examples (pulled from the five agents):

- positions.realized_pnl <= spread_at_entry * size - fees (Agent 2, Agent 4)
- positions.closed_at - positions.opened_at > 100ms (Agent 2)
- positions.exit_price matches a real market price stream within 5 bp (Agent 2)
- len(open_positions_db) == len(arb_engine.tracked_positions) (Agent 3)
- sum(fees) / sum(notional) in [0.02, 0.10] per platform (Agent 2)
- A pair's fired_state armed toggles at least once between consecutive fires (Agent 3)
- len(fired_state) <= len(_pairs) (Agent 3)
- Win rate per strategy has an upper bound given fees and average edge (Agent 2)
- No position insert without a matching signal row that passed risk checks (Agent 5)

  Failures go to Discord and halt the strategy that violated them. Do not auto-resume — human must intervene.

  7.2 Golden-path integration test. A single end-to-end test that plays a recorded WS feed against the system,
  runs for simulated 1 hour, and asserts expected trade count, PnL bounds, and DB state. Run in CI before every
  deploy.

  7.3 Alerting. Discord webhook for: circuit breaker fire, invariant violation, match cache size change > 10%,
  strategy disable, unusual fee ratios, WS disconnect > 60s, no trades in > 10 min on any enabled strategy. The
  existing webhook URL is in .env.

  7.4 Post-deploy smoke test. The deploy process (currently just rsyncs files) should be followed by an automatic
   service restart (see earlier conversation about this) AND a smoke test that reads the first 30 seconds of the
  new service's logs, asserts: Starting streams, Loaded X cached matches, no errors, at least one tick received,
  STATUS line populated. If smoke test fails → rollback.

  7.5 Observability dashboard. Extend the existing dashboard at :8000 with: realistic PnL time series, synthetic
  PnL time series, per-strategy PnL, match cache health, fired_state distribution, WS tick rates per platform,
  invariant violation counter, last halt event. No new infra needed — FastAPI already exists.

  Edge cases:

- Invariant assertions that are too tight will cause nuisance halts. Start with warn-only mode for 1 week, then
   promote to halt.
- The golden-path test will be slow unless the playback is compressed. Use event time, not wall time.

  Verification: Deliberately introduce a bug (e.g., raise realized_pnl to 10x normal) and confirm the invariant
  catches it and halts. Discord receives the alert.

  ---
  Dependency graph

  Phase 0  (observability + tourniquet)
     │
     ├─► Phase 1  (matcher fix — independent of everything else)
     │     │
     │     └─► Phase 3  (re-arm — safe to deploy only after matcher is clean)
     │
     ├─► Phase 2  (risk choke point — independent, parallelizable with Phase 1)
     │     │
     │     └─► Phase 3  (re-arm must call run_all_checks from Phase 2)
     │
     └─► Phase 4  (paper fill model — requires Phase 2 for risk checks)
           │
           └─► Phase 5  (strategy hygiene — depends on realistic PnL from Phase 4)
                 │
                 └─► Phase 6  (pre-live gating — only meaningful after 1–5)
                       │
                       └─► Phase 7  (invariants — ongoing, tightens after each phase)

  Phases 1 and 2 can run in parallel by different people. Phase 3 needs both. Phase 4 is the biggest and can
  start after Phase 2. Phase 5 is judgment-heavy and slow. Phases 6–7 are wrap-up and ongoing.

  ---
  What NOT to do

- Don't tune strategies on phantom PnL. Every minute spent optimizing P2's edge formula against the synthetic
  fill model is wasted. Land Phase 4 first.
- Don't delete the old data. The 1340 existing trades are a valuable dataset for comparing synthetic vs
  realistic PnL models. Add a column, don't drop rows.
- Don't flip to live mode until every phase is landed and Phase 7's invariants have been in warn-only mode for
  at least a week. The gaps Agent 5 found are unlimited-loss gaps. Even one trade through the current
  ArbitrageEngine bypass with real money is a serious risk.
- Don't add new strategies during this work. More strategies = more paths to audit. Land the foundation first.
- Don't skip Phase 0. The observability fixes (STATUS truthfulness, sanity cap, pause P1) are cheap and protect
   all later work.

  ---
  Open questions for the user

  1. Do you want P3/P4 disabled after Phase 4 shows them unprofitable under realistic fills? My recommendation is
   yes; Agent 4's confidence rating was LOW for both. But you may have conviction I don't see.
  2. Should the signal handler become the single choke point, or should we add risk checks inline in the
  ArbEngine? The single-choke-point approach is cleaner but is a bigger refactor. Inline is faster but creates
  two parallel check paths. I lean toward single choke point.
  3. Do you want realistic fills to replace synthetic fills outright, or run both in parallel for a bakeoff
  period? Parallel is safer; replacement is simpler.
  4. What's the timeline pressure? If you want to be live-trading in two weeks, we cut Phase 7.2 and Phase 5's
  deeper rework. If the timeline is a quarter, we do all of it.
  5. Who has Kalshi/Polymarket real-fee-model knowledge? Agent 2's recommendation to fix the fee model assumes
  someone can verify actual rates.

  Want me to take any of these phases deeper, or start turning Phase 0 into concrete file-level diffs? Reminder:
  no code yet per your instruction — I'm only producing the plan.
