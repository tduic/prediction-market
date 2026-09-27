# Handoff

_Last updated: 2026-09-27_

## State
- `main` includes the sandsthebrand fork's `main` (4 commits, merged 2026-09-27):
  paper-soak runtime telemetry in `/api/system-health`, Secret Manager strict
  mode, DB-failure / aged-position / execution-failure-rate alerts.
- Follow-up fix on top: resting (`status="pending"`) single-platform orders no
  longer count as circuit-breaker failures (`_record_circuit_breaker_result`
  in `core/strategies/single_platform.py`).
- Suite: 714 passing. `black` clean. `ruff` (88 BLE001) and `mypy` (9 errors)
  are pre-existing on main, so CI's lint/type jobs were already red.

## Next steps
1. **Before the next deploy**: the systemd units now set `SECRETS_STRICT=true`
   and `deploy.yml` no longer copies webhook/API secrets into `.env`. On the VM
   run `scripts/verify_prod_config.py --require-gcp` and confirm every secret
   (incl. `ALERT_DISCORD_WEBHOOK_URL`, `DASHBOARD_PASSWORD`) is in Secret
   Manager. Missing webhook = silent alerts; missing dashboard password = the
   dashboard refuses to bind publicly.
2. **Fork branch `sandsthebrand/phase1-arb-hardening`: not merged.** It breaks
   DB init (migration 020 spans two lines; the runner applies ALTERs line by
   line), under-counts sell-leg exposure (`size*sell_price` vs
   `size*(1-sell_price)`), treats unknown fills as zero without halting, and
   signs Kalshi reads without the `/trade-api/v2` prefix. Worth porting later,
   with fixes: Polymarket CLOB V2 client (legacy `py-clob-client` is archived),
   fee-curve profitability gate (`arb_profitability.py`; Kalshi fees must round
   up to the cent), persistent halt (migration 019), order-book depth sizing.
3. Main's fee model is flat (Kalshi 7% of notional, Polymarket 2%). The arb
   engine does not net out fees before firing.

## Gotchas
- `scripts/trading_session.py` must run from the repo root (migrations are
  found by a relative path). It calls `load_dotenv()`, which finds the repo
  `.env`, so pass `ALERT_DISCORD_WEBHOOK_URL=` explicitly for local smoke
  runs to avoid posting to Discord.
- With an empty DB the session has no matches and parks with only the
  dashboard up. It never reaches the streaming loop (`runtime.state` stays
  `starting`).
- `deploy.yml` triggers on published releases (or manual dispatch).
  `create_release.yml` is manual. The last Deploy run was v2.4.0
  (2026-04-17), even though releases up to v2.33.0 exist, most likely because
  releases created with `GITHUB_TOKEN` don't trigger workflows.
