# Roadmap

Features and improvements, roughly ordered by priority within each section.

## Completed

### Real-Time Price Feeds ✓
Websocket connections to both Polymarket's CLOB feed and Kalshi's streaming API, replacing the original polling-based ingestion. Sub-second price updates drive the ArbitrageEngine in stream mode, which is critical for P5 (Information Latency) strategy. Implemented in `paper_trading_session.py` via `stream_prices_polymarket()` and `stream_prices_kalshi()`.

### Web Dashboard ✓
React + FastAPI analytics dashboard served from the same process as the trading session. Shows portfolio overview (total capital, realized PnL, net return), per-strategy performance (win rate, Sharpe, edge capture), equity curve, strategy PnL over time, trade log, risk metrics (drawdown, VaR, concentration), and fee breakdown. Runs on `--dashboard` flag — no separate terminal needed.

### Single-Command Paper Trading ✓
Unified `python scripts/paper_trading_session.py --refresh --stream --dashboard` runs market fetching, websocket streaming, all five strategies, periodic PnL snapshots, and the dashboard server in one process. Previously required three separate terminals.

### Paper Execution Client ✓
Executes against real market prices without placing orders. Writes identical DB rows to live mode so the full analytics pipeline (dashboard, scorecard, snapshots) works in paper mode.

### Idempotent Migrations ✓
SQLite migration runner with history tracking (`migration_history` table), duplicate-column-safe ALTER TABLE handling, and automatic migration discovery. Prevents "duplicate column" errors on re-runs.

### Dashboard UX Improvements ✓
Friendly strategy names (e.g., "Cross-Market Arb" instead of "P1_cross_market_arb"), global time-range selector (1h/6h/24h/7d/30d), strategy metrics summary table, additional overview cards (total fees, total trades), improved number formatting, and better empty-state messaging.

### Model Training Pipeline ✓
Feature engineering from trade outcomes (spread, volume, time-of-day, strategy, platform — 9 features), temporal train/test split, lightweight logistic regression with gradient descent, and evaluation suite (Brier score, calibration bins, accuracy/precision/recall/F1). Results persist to `model_evaluations` table. CLI: `python scripts/train_models.py`.

### Redis Signal Queue Hardening ✓
Message deduplication via Redis SET with TTL, dead letter queue with retry/purge, backpressure monitoring with overload detection, and unified `HardenedSignalQueue` wrapping all three. CLI admin tool: `python scripts/queue_admin.py`.

## Go-Live Blockers (All Resolved)

_All three blockers have been resolved. The system is architecturally ready for `EXECUTION_MODE=live`._

### ~~Enforce Risk Controls in Execution Path~~ ✓
All five risk checks (position limit, daily loss, exposure cap, duplicate, min edge) are now enforced in `execution/handler.py` via `run_all_checks()` before any order reaches an exchange client. Failed signals are logged to `risk_check_log` and rejected. All limits are percentage-based, scaling with portfolio value.

### ~~Position Close and Exit Logic~~ ✓
New `execution/resolution.py` module monitors market resolutions and closes positions automatically. Computes realized PnL (side-aware), writes trade outcomes, updates position status, and removes from in-memory state. Includes manual exit via `force_close_position()` for stop losses and stale position detection for alerting. `execution/state.py` updated with `close_position()` that writes through to DB and `load_positions_from_db()` for startup recovery.

### ~~Exchange Reconciliation~~ ✓
New `execution/reconciliation.py` runs periodic balance reconciliation against both Polymarket and Kalshi. Compares local order history against exchange-reported balances, logs every check to `reconciliation_log` table (new migration 005), and halts trading if discrepancy exceeds threshold (default 5%). The halt flag is checked by the signal handler before processing any signal. All clients now implement `get_balance()`.

### GCP Deployment Infrastructure ✓
Full deployment pipeline for GCE. Main VM (`e2-medium`, us-central1-a) with persistent disk for SQLite at `/data`. EU proxy VM (`e2-micro`, europe-west4-a, Netherlands) running Dante SOCKS5 for Polymarket API calls. Tar-based deployment via `deploy/push.sh`, systemd service with auto-restart, GitHub Actions CI/CD on release publication. SSH tunnel for dashboard access.

### Event Loop Fix for Dashboard ✓
The `find_matches()` function processed ~75k markets in a synchronous loop, blocking the asyncio event loop and starving the dashboard server. Fixed by extracting the CPU-bound matching into `_find_matches_sync()` and calling it via `asyncio.to_thread()`, keeping the event loop responsive for uvicorn.

## Pre-Launch Checklist

_Required before the first real trade, but not architectural blockers._

### Paper Mode Soak Test (In Progress)
System deployed to GCE and running with ~49k Polymarket + ~26k Kalshi markets. Monitoring for: all five strategies firing, PnL snapshot accumulation, dashboard accuracy, memory/DB growth stability, websocket reconnection resilience.

### Credential Rotation and Secrets Management
Production Polymarket and Kalshi API keys are currently in plaintext `.env`. Generate fresh credentials and move them to a secrets vault (AWS Secrets Manager, HashiCorp Vault, or macOS Keychain for local use).

### Alerting
Slack or PagerDuty notifications for: risk limit breaches, reconciliation failures, stuck positions (open >72 hours), execution failures (>3 in 10 minutes), and database errors.

### Circuit Breaker
Halt all trading automatically if the system detects: daily loss limit exceeded, >3 consecutive order failures, reconciliation discrepancy above threshold, or manual kill switch triggered.

### Partial Fill Handling
Current behavior assumes a single fill per order. If an order partially fills, the unfilled portion is ignored. Fix: track cumulative fills per order, implement cancel-and-replace for stale partial fills, and ensure position tracking reflects the actual filled quantity.

### Failure Runbook
Documented procedures for: stuck orders (never fill), partial fills (50% filled, stuck), exchange API down (HTTP 500), local DB corruption, and position state desync.

## Medium-Term

### Monitoring & Alerting
Structured JSON logging is in place, but there's no metrics layer. Planned: Prometheus metrics export, Grafana dashboards, and integration with the alerting system above.

### Docker Deployment
Dockerfile and docker-compose as an alternative to the current GCE deployment. Health checks, graceful shutdown, volume mounts for the SQLite database, and log aggregation.

### Backtesting Framework
Use historical `market_prices` and `pair_spread_history` data to replay constraint violations through the signal pipeline. Compare simulated PnL against actual outcomes to validate strategy parameters before deploying changes.

### Multi-Outcome Markets
Current constraint rules assume binary (yes/no) markets. Extending to multi-outcome markets (e.g., "Who wins the election?" with 5+ candidates) requires generalized probability simplex constraints.

### Live Platform API Testing
The Polymarket and Kalshi execution clients have correct auth but haven't been tested against production with real orders. Run read-only API calls (fetch markets, check balances) to verify auth, then place small canary trades on Kalshi demo before production.

## Longer-Term

### Additional Platforms
Extend ingestors and execution clients to support Metaculus, PredictIt, or other prediction market platforms. The platform-agnostic design (market pairs, constraint engine) should make this straightforward.

### ML Feature Store
Centralized feature computation and caching for prediction models. Market-level features (price momentum, volume patterns, spread history) and event-level features (economic indicators, sentiment) stored for efficient model training.

### Portfolio Optimization
Replace per-signal Kelly sizing with portfolio-level optimization that considers correlation between open positions, joint probability of outcomes, and capital allocation across strategies.
