# Handoff

_Last updated: 2026-09-27_

## State
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
- Checks: ~800 tests. `black` clean. ruff (88 BLE001) and mypy (9) are
  pre-existing baselines; main's CI lint/type jobs were already red.
  Smoke: `python scripts/smoke_paper_arb.py` (live public data, no creds).

## Next steps
1. **Before the next deploy**: the systemd units set `SECRETS_STRICT=true`.
   On the VM run `scripts/verify_prod_config.py --require-gcp`. Missing
   `ALERT_DISCORD_WEBHOOK_URL` = silent alerts; missing `DASHBOARD_PASSWORD`
   = dashboard won't bind publicly. Also run `scripts/verify_api_auth.py` on
   the VM: it authenticates through the V2 client and the proxy, and a
   failure means live Polymarket is broken. Fallback:
   `POLYMARKET_CLIENT=legacy`.
2. Open follow-ups (task chips were created):
   - The legacy Kalshi live client sends the internal `kal_` id as the ticker.
   - Single-platform/batch strategies store translated-sell (NO-book) fills
     as YES prices.
3. Fork pieces deliberately not ported: its engine rewrite, Kalshi V2 client
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
