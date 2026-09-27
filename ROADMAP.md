# Roadmap

Features and improvements, roughly ordered by priority within each section.

## Completed

### Single-Process v2 Architecture ✓
Unified `scripts/trading_session.py` runs the ingestor websocket feeds, tick-driven `ArbitrageEngine`, periodic `ScheduledStrategyRunner` (P2–P5 spread-bucket opportunities), and the embedded dashboard in one async process. Replaced the earlier v1 two-service + Redis-queue design.

### Real-Time Price Feeds ✓
Websocket connections to Polymarket's CLOB feed and Kalshi's streaming API. Sub-second price updates drive `ArbitrageEngine.on_price_update()`, which is critical for latency-sensitive cross-market arbitrage. Implemented in `core/ingestor/streamer.py` (one connection per exchange, Polymarket chunked at 450 assets/socket).

### Hot Pair Refresh ✓
`ArbitrageEngine.update_pairs()` supports diffing a new match set against the in-memory index — adds new pairs, drops stale ones, preserves `fired_state` and live prices for retained markets. A background loop in `scripts/trading_session.py` polls cached matches every 30 minutes; if the websocket subscription set changed, it cancels and respawns the Polymarket / Kalshi streams against the new asset and ticker lists. Closes the gap where the weekly `predictor-refresh.service` wrote new matches but the running process kept trading the old set until a restart.

### Web Dashboard ✓
React + FastAPI analytics dashboard served from the same process as the trading session. Shows portfolio overview, per-strategy performance, equity curve, trade log, risk metrics, and fee breakdown. Runs on `--dashboard` flag — no separate terminal needed.

### Paper Execution Client ✓
`execution/clients/paper.py` executes against real market prices without placing orders. Writes identical DB rows to live mode so the full analytics pipeline (dashboard, snapshots, reconciliation) works in paper mode.

### Idempotent Migrations ✓
SQLite migration runner with history tracking (`migration_history` table), duplicate-column-safe ALTER TABLE handling, and automatic migration discovery. 18 migrations to date; migration 010 dropped unused ML tables and later migrations add Polymarket book support and operational indexes.

## Go-Live Blockers (All Resolved)

_All blockers resolved. The system is architecturally ready for `EXECUTION_MODE=live`._

### ~~Enforce Risk Controls in Execution Path~~ ✓
All five risk checks (position limit, daily loss, exposure cap, duplicate, min edge) are enforced via `core/signals/risk.py::run_all_checks()` before any order reaches an exchange client. Called inline from `ArbitrageEngine._execute_arb_trade` (P1) and from `core/strategies/single_platform.py` (P2–P5). Failed signals are logged to `risk_check_log` and rejected. All limits are percentage-based, scaling with portfolio value.

### ~~Position Close and Market Resolution~~ ✓
`core/engine/resolution.py` closes positions once their market resolves. Called from `ScheduledStrategyRunner.run_one_cycle` before mark-to-market. Settlement price comes from `markets.outcome_value` when present, falling back to YES/NO label inference. Computes side-aware realized PnL (BUY: `(exit-entry)*size - fees`, SELL reversed), writes `trades` rows, and updates `positions.status` to `resolved` with `resolution_outcome`.

### ~~DB-Level Reconciliation~~ ✓
`core/engine/reconciliation.py` runs three internal-consistency checks every 5 scheduler cycles: orphaned positions (no parent signal), stuck pending orders (>300s), and unbalanced cross-market arb pairs. Discrepancies are written to `reconciliation_log` with `check_type`, `discrepancy`, `status`, and `action_taken` for alerting.

### GCP Deployment Infrastructure ✓
Deployment pipeline for GCE. Main VM (`e2-medium`, us-central1-a) with persistent disk for SQLite at `/data`. Optional EU proxy VM (`e2-micro`, Netherlands) running Dante SOCKS5 for Polymarket API calls. Tar-based deployment via `deploy/push.sh`, systemd service with auto-restart, GitHub Actions CI/CD on release publication.

### Event Loop Fix for Dashboard ✓
The market-match routine processed ~75k markets in a synchronous loop, blocking the asyncio event loop and starving the dashboard server. Fixed by extracting the CPU-bound matching into a sync helper and calling it via `asyncio.to_thread()`.

### Redis Dependency Removed ✓
The v1 signal queue / event bus lived on Redis. After the Phase E rewrite to a single-process tick-driven design, Redis served no purpose. Dropped from `requirements.txt`, the GitHub Actions service container, the `predictor.service` / `predictor-refresh.service` unit dependencies, and `config/settings.example.env`. The running VM no longer needs a Redis install.

## Pre-Launch Checklist

_Required before the first real trade, but not architectural blockers._

### Paper Mode Soak Test (In Progress)
System deployed to GCE and running with ~49k Polymarket + ~26k Kalshi markets. Monitoring for: strategy firing cadence, PnL snapshot accumulation, dashboard accuracy, memory/DB growth stability, and websocket reconnection resilience.

### Credential Rotation and Secrets Management
Code hardening is complete: production uses strict GCP Secret Manager lookups
and does not fall back to plaintext environment credentials. Remaining
deployment prerequisites are for an authorized administrator to provision the
required secrets, VM access, and protected Kalshi PEM path. Any deployed
`.env` usage is limited to non-secret runtime configuration.

### Alerting (Partial)
`core/alerting.py` ships an `AlertManager` with Discord and Slack webhook
transports, configured through the secrets backend. It is wired to
invariant-violation checks, reconciliation discrepancies, and circuit-breaker
events. Remaining: paging rules for stuck positions (open >72h), execution
failures (>3 in 10 minutes), and material DB errors; optional second transport
(PagerDuty or email) for severity tiering.

### Circuit Breaker ✓ (wiring pending live mode)
`execution/circuit_breaker.py` halts trading on daily loss limit, consecutive order failures, or reconciliation discrepancy above threshold. Checked at the start of `ScheduledStrategyRunner.run_one_cycle`. Manual kill switch and on-call dashboard control still TODO.

### Partial Fill Handling
Current behavior assumes a single fill per order. If an order partially fills, the unfilled portion is ignored. Fix: track cumulative fills per order, implement cancel-and-replace for stale partial fills, and ensure position tracking reflects the actual filled quantity.

### Failure Runbook
Documented procedures for: stuck orders, partial fills, exchange API down (HTTP 500), local DB corruption, and position state desync.

## Medium-Term

### Metrics Export
Structured JSON logging is in place, but there's no metrics layer. Planned: Prometheus metrics export, Grafana dashboards, and integration with the alerting system above.

### Backtesting Framework
Use historical `market_prices` and `pair_spread_history` data to replay constraint violations through the signal pipeline. Compare simulated PnL against actual outcomes to validate strategy parameters before deploying changes.

### Multi-Outcome Markets
Current constraint rules assume binary (yes/no) markets. Extending to multi-outcome markets (e.g., "Who wins the election?" with 5+ candidates) requires generalized probability simplex constraints.

### Live Platform API Testing
The Polymarket and Kalshi execution clients have correct auth but haven't been tested against production with real orders. Run read-only API calls (fetch markets, check balances) to verify auth, then place small canary trades on Kalshi demo before production.

## Longer-Term

### Additional Platforms
Extend ingestors and execution clients to support Metaculus, PredictIt, or other prediction market platforms. The platform-agnostic design (market pairs, constraint engine) should make this straightforward.

### Portfolio Optimization
Replace per-signal Kelly sizing with portfolio-level optimization that considers correlation between open positions, joint probability of outcomes, and capital allocation across strategies.
