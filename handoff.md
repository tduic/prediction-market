# Handoff

_Last updated: 2026-10-17_

## State
- 2026-10-16 toolchain upgrade: CI tests Python 3.15 (and 3.14), lint on
  3.15, mypy `python_version = "3.15"`; dashboard on Node 24 and
  TypeScript 7.0 (added `src/vite-env.d.ts` so TS 7's checked side-effect
  imports accept `./index.css`). The GCP VM's Python was not touched
  (`deploy/vm_init_remote.sh` still installs 3.12); moving it is a separate,
  deliberate step.
- Fork `sandsthebrand/prediction-market` `main` (4 commits) merged 2026-09-27,
  plus a breaker fix: resting (`pending`) single-platform orders don't count
  as failures.
- Useful pieces of fork branch `phase1-arb-hardening` ported with fixes
  (plan: `docs/superpowers/plans/2026-09-27-phase1-port.md`). Each money-path
  commit was reviewed by an independent second model before push:
  - **Fee gate**: `core/engine/fees.py` (Kalshi 0.07·mult·C·P(1−P) rounded
    up to the cent; Polymarket r·(P(1−P))^e) and `execution/market_data.py`
    (public fee discovery keyed by `markets.platform_id`, served stale while
    refreshing, warmed at startup). Kelly sizes on the net edge.
    `MIN_EDGE_TO_TRADE` stays on the gross spread (fees aren't counted twice).
  - **Paper fees** follow the same curves.
  - **Depth**: size capped to executable book depth at our limits and floored
    to whole contracts.
  - **Execution halt** (migration 019, `scripts/clear_halt.py`): trips on an
    unknown fill or incomplete hedge. The hedge leg is sent only after the
    buy fills. `pending` is never retried.
  - **P&L fix**: translated Polymarket sells (BUY NO) are converted to YES
    space (`OrderResult.book`).
  - **Polymarket CLOB V2 client** (`execution/clients/polymarket_v2.py`) is
    the live default (`POLYMARKET_CLIENT=v2`; `legacy` = archived
    py-clob-client). It uses FAK orders, never guesses a fill (ambiguous
    means `pending`, which halts), and installs the proxy before key
    derivation. It has not placed a real order yet: its first live order is
    the real test.
- 2026-10-17: the **Kalshi live client** now uses CreateOrderV2
  (`POST /portfolio/events/orders`). It sends the exchange ticker from
  `markets.platform_id` (it used to send the internal `kal_` id), places
  immediate-or-cancel orders in whole contracts, and reads the final fill,
  average price and real fee from the 201 response with no polling. A 4xx
  is a clean failure. A 5xx, 408, 409, timeout or unreadable acceptance
  returns `pending`, which halts. The old client read GET-order fields
  Kalshi has since removed, and assumed full fills.
- 2026-10-17: single-platform positions from translated Polymarket sells
  (BUY NO) store `entry_price` in YES space with `book='NO'`. They used to
  store the raw NO price as a YES entry, which inverted mark-to-market P&L.
  The legacy Polymarket client now tags `book` too.
- 2026-10-17 bug-and-dead-code pass (three audits, each fix reviewed by a
  second model):
  - **Data loss:** market and pair refreshes used `INSERT OR REPLACE`,
    which cascade-deleted orders, signals, positions and outcomes on every
    `--refresh` or weekly refresh. They are upserts now.
  - **Arb engine:** halts on any exception after a buy fill, on a
    cancellation, if the position can't be recorded, or on the P&L sanity
    cap (and the cap now records the trade first). The halt fails closed
    and falls back to in-memory. No rollback of recorded fills. A no-fill
    isn't a breaker failure, and it mutes the pair on cooldown.
  - **Order clients:** V2 separates build from post (a build failure is a
    clean `failed`) and rounds prices to the tick only in our favour.
    Kalshi signing errors return `failed`. The legacy client treats a post
    exception as unknown.
  - **Accounting:** `actual_pnl` is net of fees everywhere. Readers used
    to subtract fees again, which hit the breaker, portfolio value, Kelly
    bankroll and the dashboard. SELL legs are costed at size×(1−p) in the
    risk checks and the fee-ratio invariant.
  - **Ops:** one dashboard password source (this was an auth bypass), task
    supervision with SIGTERM handling, push.sh uses `git archive` and never
    ships `.env` or keys, deploy-order and concurrency fixes, env_merge
    keeps values literal, StartLimit* moved to [Unit], clear_halt and
    verify_api_auth fixes, example env cleaned up.
  - **Alerting:** a standing invariant violation no longer pages every
    cycle, and failed sends are retried. A transient Secret Manager error
    no longer blanks a secret.
  - **P2 structured-event strategy is off by default**
    (`STRATEGY_P2_ENABLED=false`). It grouped markets that only share a
    title.
  - **Dead code removed:** batch.py, unused config fields, DB, analytics
    and ingestor helpers, and about 2,400 lines of tests that only tested
    stand-in classes.
- Checks: about 1,090 tests, zero warnings. pytest, mypy, ruff and black
  are clean. Smoke: `python scripts/smoke_paper_arb.py` (live public data,
  no creds).

## Next steps
1. **Before the next deploy**: the systemd units set `SECRETS_STRICT=true`.
   On the VM run `scripts/verify_prod_config.py --require-gcp`. Missing
   `ALERT_DISCORD_WEBHOOK_URL` = silent alerts; missing `DASHBOARD_PASSWORD`
   = dashboard won't bind publicly. Also run `scripts/verify_api_auth.py` on
   the VM: it authenticates through the V2 client and the proxy, and a
   failure means live Polymarket is broken. Fallback:
   `POLYMARKET_CLIENT=legacy`.
2. Before the first live Kalshi order, place one order in Kalshi's demo
   environment and confirm:
   - `average_fill_price` on an ask (sell) is in YES space;
   - `fill_count × average_fee_paid` matches the account's fee debit (fee
     rounding has per-order accumulators);
   - `get_balance()` still parses (it divides `balance` by 100; that schema
     hasn't been checked).
   After a 5xx or timeout, the order row holds only the `client_order_id`
   (`UNKNOWN-kalshi-<id>`), so reconcile against Kalshi by that id.
3. **The dashboard will become public on the next deploy.** The unit binds
   `0.0.0.0`; until now the guard misread the password and forced
   loopback. Decide on the GCP firewall rule for tcp:8000, TLS or an IP
   allowlist, and confirm `DASHBOARD_PASSWORD` exists in Secret Manager.
4. Not fixed (each needs a design decision):
   - Nothing marks markets resolved (no resolution pipeline). As a result,
     `close_resolved_positions` never runs, and capital locked in P1 arbs
     (recorded `closed` immediately) isn't counted toward the exposure cap.
   - `compute_position_size` returns dollars but the engine uses the
     result as a contract count. For arbs it's about 1:1; it undersizes
     single-platform buys.
   - Migration 007's backfill never ran (pnl_model DEFAULT 'realistic'), so
     legacy synthetic positions are labelled realistic.
   - The migration runner isn't atomic.
   - The VM still installs Python 3.12 while CI tests 3.14/3.15.
5. Live P2–P5 positions are "closed" by `mark_and_close_positions` in the
   DB only; nothing sends an exit order. That's fine for paper but not for
   live.
6. Fork pieces deliberately not ported: its engine rewrite, Kalshi V2 client
   (read signing broken, endpoint unverified), exchange reconciliation
   (3 bugs), CI narrowing, P2–P5 disable.

## Gotchas
- `scripts/trading_session.py` must run from the repo root (relative
  migrations path) and `load_dotenv()` finds the repo `.env`, so pass
  `ALERT_DISCORD_WEBHOOK_URL=` for local runs. `--refresh` needs Kalshi
  credentials (the ingestor signs market listing).
- The test helper `_risk_config()` in `tests/integration/test_arb_engine.py`
  builds `RiskControlConfig` without `__init__`, so new config fields must be
  added to its defaults.
- `core.engine` eagerly imports `arb_engine`. `execution.market_data`
  imports `core.engine.fees`, so `arb_engine` imports the market_data
  *module* (not the function) to avoid a cycle.
- `deploy.yml` triggers on published releases. The last Deploy run was
  v2.4.0 (2026-04-17).
