# Prediction Market Trading System

An async Python trading system for detecting and exploiting pricing inefficiencies across Polymarket and Kalshi. Covers the full quant lifecycle: market ingestion, cross-platform pair matching, arbitrage detection, risk-managed order execution, and post-trade analytics — all in one process.

## Architecture

A single async process drives everything: websocket-fed price streams, latency-sensitive cross-platform arbitrage, scheduled single-platform strategies, reconciliation, and an embedded dashboard.

```
              ┌────────────────────────────────────────────────────────┐
              │                   trading_session.py                   │
              │                                                        │
 Polymarket ─►│  Ingestor (WS)   ─► live price cache                   │
 Kalshi     ─►│                                                        │
              │                                                        │
              │  ArbitrageEngine  (tick-driven, on every WS update)    │
              │    → cross-platform arb (P1)                           │
              │                                                        │
              │  ScheduledStrategyRunner  (every ~120s)                │
              │    → resolution pass                                   │
              │    → mark-to-market close-out                          │
              │    → reconciliation (every 5 cycles)                   │
              │    → invariant checks                                  │
              │    → single-platform strategies (P2–P5)                │
              │                                                        │
              │  Dashboard (FastAPI + React, embedded on :8000)        │
              └──────────────────────────┬─────────────────────────────┘
                                         │
                                 SQLite (WAL mode)
```

**Tick path** (latency-sensitive): `core/engine/arb_engine.py` reacts to every websocket price update. It checks the matched-pair book for cross-platform violations, sizes with Kelly, runs risk checks, and submits orders with exponential-backoff retries.

**Scheduled path** (every `--interval` seconds): `core/engine/scheduler.py` runs position lifecycle work — settle resolved markets, mark-to-market close expired holdings, reconcile internal state (orphaned positions, stuck pending orders, unbalanced arb legs), check invariants, then scan for P2–P5 opportunities.

**Dashboard**: React + Vite frontend built into `dashboard/dist/`, served by FastAPI from the same process. Portfolio overview, per-strategy scorecard, equity curve, trade log, risk metrics, fee breakdown. Time-range filter (1h–30d).

## Quick Start

### Prerequisites
- Python 3.12+ (3.12 on the VM; 3.14 works locally)
- Node.js 18+ (to build the dashboard frontend)

### Setup
```bash
git clone https://github.com/tyjodu/prediction-market.git
cd prediction-market
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Build dashboard frontend once (or any time the React code changes)
cd dashboard && npm install && npm run build && cd ..

# Configure
cp config/settings.example.env .env
# Edit .env with API credentials and EXECUTION_MODE
```

### Run

```bash
# Normal start — loads cached market pairs from DB, streams prices,
# trades continuously, serves the dashboard on :8000.
python scripts/trading_session.py --dashboard

# First run (or when you want fresh pair discovery, ~30 min):
python scripts/refresh_markets.py
python scripts/trading_session.py --dashboard

# Dashboard at http://localhost:8000
```

| Flag | Default | Description |
|------|---------|-------------|
| `--refresh` | off | Re-fetch all markets and re-run the matcher before streaming. Slow. Normal restarts skip this and load cached pairs. |
| `--interval N` | 120 | Seconds between scheduled-strategy cycles. |
| `--min-spread X` | 0.03 | Minimum cross-platform spread to open a P1 arb. Env var `MIN_SPREAD_CROSS_PLATFORM` overrides. |
| `--dashboard` | off | Start the embedded dashboard server. |
| `--dashboard-port` | 8000 | Dashboard port. |
| `--dashboard-host` | 127.0.0.1 | Bind address. Use `0.0.0.0` to expose publicly (HTTP Basic Auth required — see `deploy/DEPLOY.md`). |

Market re-matching runs separately via `scripts/refresh_markets.py` (invoked ad-hoc or by the `predictor-refresh.timer` systemd unit every Sunday).

### Tests

```bash
pytest tests/ -q
# 520 tests, all self-contained (in-memory aiosqlite with real migration schema).
# No external services required.
```

Type-check:
```bash
mypy core/ execution/ scripts/
```

## Trading Strategies

One real strategy (P1) plus four spread-bucket labels (P2–P5) applied to same-platform signals for PnL attribution. P2–P5 are not distinct algorithms — they flow through the same execution path; the label is chosen by spread magnitude and pair type (see `core/strategies/assignment.py`).

| Label | Where | What it means |
|-------|-------|---------------|
| **P1 – Cross-Market Arb** | `ArbitrageEngine.on_price_update` | Same event priced differently on Polymarket vs Kalshi. Buy cheap / sell rich simultaneously. This is the only true arb. |
| **P2 – Structured Event** | `detect_single_platform_opportunities` | Same-platform series where YES prices sum > 1.05. SELL the most overpriced leg. |
| **P3 – Calibration Bias** | same | Spread ≥ 0.05 and < 0.10 — mid-range mispricing. |
| **P4 – Liquidity Timing** | same | Complement-pair signals below the P3 threshold. |
| **P5 – Information Latency** | same | Spread > 0.10 — large mispricing, bet the direction. |

Per-cycle slot caps (`core/strategies/single_platform.py`) keep any one label from crowding out the others. Per-strategy enable flags (`STRATEGY_P{2..5}_ENABLED`) and a rolling-PnL kill-switch (`STRATEGY_KILLSWITCH_*`) let you disable a label without a deploy.

## Risk Controls

All monetary limits are percentages of portfolio value, so they scale as the account grows. Portfolio value = `STARTING_CAPITAL + realized_pnl - fees`.

Every risk check result is logged to `risk_check_log` for audit. Enforced inline before any order is submitted (`core/signals/risk.py`).

| Check | Env var | Default | Example ($10k portfolio) |
|---|---|---|---|
| Max position size | `MAX_POSITION_PCT` | 5% | $500 per trade |
| Daily loss limit | `MAX_DAILY_LOSS_PCT` | 2% | $200/day |
| Portfolio exposure cap | `MAX_PORTFOLIO_EXPOSURE_PCT` | 20% | $2,000 total deployed |
| Minimum edge | `MIN_EDGE_TO_TRADE` | 2% | Signal must clear 2% edge |
| Duplicate window | `DUPLICATE_SIGNAL_WINDOW_S` | 300s | No repeat trades within 5 min |
| Kelly fraction | `KELLY_FRACTION` | 0.25 | Quarter-Kelly sizing |
| Consecutive failures | `CONSECUTIVE_FAILURE_LIMIT` | 5 | Halt after N back-to-back order failures |

A daily-loss circuit breaker (`execution/circuit_breaker.py`) halts the whole process — both the tick engine and the scheduled runner — when the daily loss limit is breached. Halt is sticky; it clears at the next UTC midnight.

## Key Components

### Ingestion (`core/ingestor/`)
- `kalshi.py`, `polymarket.py` — REST pollers for market metadata, snapshots, and backfills.
- `streamer.py` — websocket feeds (Polymarket CLOB, Kalshi stream) that drive the live price cache. Sub-second updates.
- `store.py` — DB writes for market snapshots and price history.

### Matching (`core/matching/engine.py`)
Pairs related markets across platforms using title normalization and `sentence-transformers/all-MiniLM-L6-v2` embeddings. Matched pairs persist in `market_pairs` and are loaded on restart — the heavy 30-min matching pass only runs when you explicitly call `scripts/refresh_markets.py`.

### Engine (`core/engine/`)
- `arb_engine.py` — tick-driven P1 cross-platform arb. Retries on transient failures with exponential backoff, logs `UNBALANCED_ARB` when exactly one leg fills.
- `scheduler.py` — `ScheduledStrategyRunner.run_one_cycle()`: resolution → mark-to-market → reconciliation (every 5th) → invariants → P2–P5 scan.
- `resolution.py` — closes positions for markets that settled (`markets.status IN ('resolved','closed')`), computes PnL at settlement price, sets `resolution_outcome`.
- `reconciliation.py` — DB-level consistency: orphaned positions, stuck pending orders (>5 min), unbalanced arb pairs. Writes to `reconciliation_log`.
- `fire_state.py` — per-pair cooldown + hysteresis state to prevent re-firing the same arb on jitter.

### Strategies (`core/strategies/`)
- `assignment.py` — spread-bucket → strategy label mapping.
- `single_platform.py` — P2–P5 detection + `mark_and_close_positions` for expired holdings.
- `batch.py` — initial-sweep pass over all pairs on startup.

### Signals (`core/signals/`)
- `risk.py` — all pre-trade risk checks (position, daily loss, exposure, duplicate, min edge).
- `sizing.py` — Kelly fractional sizing, capped by `MAX_POSITION_PCT`.

### Execution (`execution/`)
- `clients/kalshi.py`, `clients/polymarket.py` — live clients with RSA-PSS auth. Polymarket routes through a SOCKS5 proxy for EU compliance.
- `clients/paper.py` — fills at real market prices with configurable slippage; writes identical DB rows to live mode so analytics work unchanged.
- `factory.py` — builds the correct client per `EXECUTION_MODE`.
- `circuit_breaker.py` — sticky daily-loss halt shared by tick and scheduled paths.

### Dashboard (`scripts/dashboard_api.py`, `dashboard/`)
FastAPI app embedded in the trading process. Serves the built React SPA and JSON endpoints for portfolio/strategy/trade/risk views. Optional HTTP Basic Auth via `DASHBOARD_USER` / `DASHBOARD_PASSWORD`.

### Snapshots & analytics (`core/snapshots/`, `core/analytics.py`)
Periodic PnL snapshots per strategy (`pnl_snapshots`, `strategy_pnl_snapshots`). `StrategyScorecard` produces summary/daily/comparison views for the dashboard.

### Invariants & alerting (`core/invariants.py`, `core/alerting.py`)
Cross-table sanity checks (violations recorded to `invariant_violations`). Violations optionally forward to a Discord webhook via `core.alerting.AlertManager`.

## Configuration

All settings load from environment variables. Key ones (see `core/config.py` for the full list):

| Variable | Default | Description |
|---|---|---|
| `EXECUTION_MODE` | `paper` | `paper`, `shadow`, or `live`. Only `live` requires all platform credentials. |
| `STARTING_CAPITAL` | `10000` | Baseline for portfolio-percentage risk limits. |
| `DB_PATH` | `prediction_market.db` | SQLite location. |
| `KALSHI_API_KEY` / `KALSHI_RSA_KEY_PATH` | — | Required in live mode. |
| `POLYMARKET_PRIVATE_KEY` / `POLYMARKET_WALLET_ADDRESS` | — | Required in live mode. |
| `POLYMARKET_PROXY` | — | `socks5://host:port` for EU routing. |
| `SECRETS_BACKEND` | `env` | `env` or `gcp` (GCP Secret Manager). |
| `GCP_PROJECT_ID` | — | Project for Secret Manager lookups. |
| `MAX_POSITION_PCT` | `0.05` | See Risk Controls. |
| `MAX_DAILY_LOSS_PCT` | `0.02` | See Risk Controls. |
| `MAX_PORTFOLIO_EXPOSURE_PCT` | `0.20` | See Risk Controls. |
| `KELLY_FRACTION` | `0.25` | Fractional Kelly. |
| `MIN_SPREAD_CROSS_PLATFORM` | `0.03` | Overrides `--min-spread`. Set to `99.0` to pause P1. |
| `STRATEGY_P{2,3,4,5}_ENABLED` | `true` | Per-label kill. |
| `LOG_FORMAT` | `text` | `json` for structured prod logging. |
| `DASHBOARD_PASSWORD` | — | Set to enable HTTP Basic Auth on the dashboard. |

## Database

SQLite with WAL mode. 19 live tables after `migrations/010`:

- **Market data:** `markets`, `market_prices`, `ingestor_runs`
- **Pair analysis:** `market_pairs`, `pair_spread_history`
- **Trading pipeline:** `violations`, `signals`, `risk_check_log`, `signal_events`
- **Execution:** `orders`, `order_events`, `positions`
- **Analytics:** `pnl_snapshots`, `strategy_pnl_snapshots`, `trade_outcomes`
- **Operational:** `system_events`, `reconciliation_log`, `invariant_violations`, `phase0_baseline`

Schema lives in `core/storage/migrations/` (numbered `001`–`010`). The migration runner tracks applied files in `migration_history` and is idempotent.

## Project Layout

```
prediction-market/
├── core/
│   ├── config.py              # Env-driven config (percentage risk limits)
│   ├── analytics.py           # StrategyScorecard (dashboard queries)
│   ├── invariants.py          # Cross-table sanity checks
│   ├── live_gate.py           # Live-mode guardrails
│   ├── logging_config.py      # Structured JSON / text logging
│   ├── secrets.py             # env or GCP Secret Manager
│   ├── alerting.py            # Discord webhook alerts
│   ├── engine/                # Tick + scheduled lifecycle (arb, resolution, reconciliation)
│   ├── ingestor/              # Polymarket + Kalshi REST/WS
│   ├── matching/              # Market-pair discovery
│   ├── signals/               # Risk checks + Kelly sizing
│   ├── snapshots/             # Periodic PnL snapshots
│   ├── storage/               # DB + migrations
│   └── strategies/            # P1 arb + P2–P5 spread buckets
├── execution/
│   ├── circuit_breaker.py     # Sticky daily-loss halt
│   ├── factory.py             # Client selector
│   ├── models.py              # Shared order models
│   └── clients/               # paper, kalshi, polymarket
├── dashboard/                 # React + Vite frontend
├── scripts/
│   ├── trading_session.py     # Main entry (streams + scheduled + dashboard)
│   ├── refresh_markets.py     # One-shot market re-fetch + re-match
│   ├── dashboard_api.py       # FastAPI app (embedded)
│   ├── take_baseline.py       # Phase 0 baseline snapshot tool
│   ├── verify_api_auth.py     # Auth smoke test
│   └── verify_prod_config.py  # Production config smoke test
├── tests/                     # 520 tests, all in-memory aiosqlite
├── deploy/                    # GCE provisioning, systemd units, CI/CD
├── docs/
│   └── archive/               # Phase 0–7 design docs (historical)
├── ROADMAP.md
└── requirements.txt
```

## Deployment

GCE `e2-medium` VM (us-central1-a) with a persistent data disk for SQLite. Optional `e2-micro` EU proxy VM running Dante SOCKS5 for Polymarket. systemd-managed service (`predictor.service`) with auto-restart; weekly market re-match via `predictor-refresh.timer`. GitHub Actions auto-deploys on release.

Full walkthrough in [`deploy/DEPLOY.md`](deploy/DEPLOY.md).

## License

MIT
