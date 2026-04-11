# Prediction Market Trading System

An async Python trading system for detecting and exploiting pricing inefficiencies across prediction market platforms (Polymarket, Kalshi). Covers the full quant lifecycle: data ingestion, arbitrage detection, signal generation, risk management, order execution, and post-trade analytics.

## Architecture

The system runs as two async services communicating via Redis, with an embedded analytics dashboard:

```
┌─────────────────────────────┐       Redis        ┌─────────────────────────────┐
│        Core Service         │  ───signals───────► │     Execution Service       │
│                             │                     │                             │
│  Ingestor → Constraint      │                     │  Signal Handler → Risk      │
│  Engine → Signal Generator  │                     │  Checks → Router → Clients  │
│  → Risk Checks              │                     │  → Position State           │
└──────────┬──────────────────┘                     └──────────┬──────────────────┘
           │                                                   │
           └──────────────── SQLite (WAL) ─────────────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │   Dashboard (FastAPI +     │
                    │   React on :8000)          │
                    └───────────────────────────┘
```

**Core Service** polls market data, detects arbitrage opportunities through constraint violations, generates trading signals with Kelly-criterion sizing, and validates them against risk limits.

**Execution Service** consumes signals from Redis, enforces risk checks (position limits, daily loss, exposure caps, deduplication, minimum edge), routes orders to the appropriate platform client, tracks fills, manages position state, monitors market resolutions to close positions and record realized PnL, and runs periodic exchange reconciliation to halt trading if local state drifts from exchange-reported balances.

**Dashboard** is a React + FastAPI app embedded in the trading session process. Shows portfolio overview, per-strategy performance, equity curve, trade log, risk metrics, and fee breakdown — all updated every 30 seconds.

Both services share a SQLite database (20 tables, WAL mode for concurrent access).

## Quick Start

### Prerequisites

- Python 3.12+ (3.12 on GCE production VM, 3.14 on local dev)
- Node.js 18+ (for dashboard frontend build)
- Redis (for live two-service mode; not needed for paper trading)

### Setup
```bash
git clone https://github.com/tyjodu/prediction-market.git
cd prediction-market
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Build dashboard frontend
cd dashboard && npm install && npm run build && cd ..

# Configure
cp config/settings.example.env .env
# Edit .env with your API credentials
```

### Paper Trading (Recommended Start)

The paper trading system fetches real markets from Polymarket (~49k) and Kalshi (~26k), matches identical events across platforms, and executes simulated trades with full analytics.

```bash
# Single command: stream prices, trade continuously, serve dashboard
# (loads cached match pairs from DB; fast restart)
python scripts/paper_trading_session.py --stream --dashboard

# First run or forced re-match: fetch all markets and re-discover pairs (slow, ~30 min)
python scripts/paper_trading_session.py --refresh --stream --dashboard

# Then open http://localhost:8000
```

| Flag | Description |
|------|-------------|
| `--refresh` | Fetch all markets from both exchanges, run matcher, persist matched pairs. Slow (~30 min) but only needed to discover new matches. Omit on normal restarts — cached pairs load in seconds. |
| `--once` | Load cached matches, run one trading cycle, exit. |
| `--stream` | Open websocket connections for real-time prices, trade continuously until Ctrl+C. |
| `--dashboard` | Serve the analytics dashboard on port 8000 (embedded in the same process). |
| `--dashboard-port N` | Dashboard port (default: 8000). |
| `--interval N` | Seconds between scheduled strategy cycles in stream mode (default: 120). |
| `--min-spread X` | Minimum price spread to trigger a trade (default: 0.03). |

### Model Training
```bash
# Train prediction models on historical trade data
python scripts/train_models.py --days 30

# Train a specific model
python scripts/train_models.py --model calibration --days 90
```

### Signal Queue Administration
```bash
# Check queue health
python scripts/queue_admin.py --status

# List failed signals in dead letter queue
python scripts/queue_admin.py --dlq-list

# Retry all failed signals
python scripts/queue_admin.py --dlq-retry
```

### Run Tests
```bash
pytest tests/ -v
# 410+ tests covering constraints, matching, models, risk, sizing,
# execution, ingestor, signal flow, arb engine, paper client,
# dashboard API, schema compliance, resolution, reconciliation,
# state management, and all five trading strategies (P1-P5).
#
# Note: 20 tests require a running Redis instance (signal hardening suite).
# All other tests are self-contained with in-memory SQLite.
```

### Run Live Services (Two-Service Mode)
```bash
docker compose up -d   # Start Redis
python -m core.main    # Terminal 1: ingestor + signal generation
python -m execution.main  # Terminal 2: order execution
```

## GCP Deployment

The system deploys to a GCE `e2-medium` VM (us-central1-a) with a separate persistent disk for SQLite. A lightweight `e2-micro` VM in europe-west4-a runs a Dante SOCKS5 proxy for Polymarket API calls.

### First-Time Setup
```bash
# 1. Provision infrastructure (idempotent)
bash deploy/provision.sh          # Main VM
bash deploy/provision_proxy.sh    # EU proxy VM

# 2. Bootstrap VMs
bash deploy/vm_setup.sh           # Install Python 3.12, Node.js, Redis
bash deploy/setup_proxy.sh        # Install Dante SOCKS5

# 3. First deploy (code + systemd service + weekly refresh timer)
bash deploy/push.sh
```

### Auto-Deploy on Release
GitHub Actions (`deploy.yml`) auto-deploys on release publication:
```bash
git tag v2.x.y && git push origin v2.x.y
gh release create v2.x.y --title "v2.x.y" --notes "..."
```

The workflow: authenticates to GCP → builds dashboard in CI → packages code → uploads to VM → rsyncs to `/data/predictor/prediction-market/` (preserving `.env` and DB) → installs Python deps → installs systemd units → restarts service → writes/merges secrets into `.env` → health check → Discord notification.

### VM Management

Access the VM via SSH:
```bash
gcloud compute ssh predictor-vm --zone=us-central1-a
```

Useful commands:
```bash
# Tail live logs
sudo journalctl -u predictor -f

# Check service health
sudo systemctl is-active predictor
sudo systemctl status predictor

# Restart service (fast — loads cached market pairs from DB, ~5 seconds)
sudo systemctl restart predictor

# Force a full market re-match without restarting the service
sudo systemctl start predictor-refresh.service
sudo journalctl -u predictor-refresh -f

# Check when the weekly market refresh timer last ran / next runs
sudo systemctl list-timers predictor-refresh.timer

# Check .env (secrets)
sudo cat /data/predictor/.env

# Flip to live trading mode
sudo sed -i 's/EXECUTION_MODE=paper/EXECUTION_MODE=live/' /data/predictor/.env
sudo systemctl restart predictor

# SSH tunnel to dashboard (port 8000)
# Run this locally:
gcloud compute ssh predictor-vm --zone=us-central1-a -- -L 8000:localhost:8000
# Then open http://localhost:8000
```

### State Across Deployments

The following persists across deploys and is **never overwritten** by the deploy workflow:

| What | Where | Notes |
|------|-------|-------|
| SQLite database | `/data/predictor/prediction_market.db` | All trade history, market pairs, signals, PnL. Excluded from rsync (`--exclude='*.db'`). |
| Secrets & config | `/data/predictor/.env` | Credentials, risk limits, execution mode. Excluded from rsync. Deploy merges new values in without overwriting `EXECUTION_MODE` or other manually-set keys. |
| Python venv | `/data/predictor/venv/` | Rebuilt from `requirements.txt` on each deploy. |
| Kalshi RSA key | `/data/predictor/kalshi.pem` | Set once at provision time. Never touched by deploy. |

The following is **replaced on each deploy**:

| What | Notes |
|------|-------|
| Application code | `/data/predictor/prediction-market/` rsynced from CI |
| Dashboard frontend | Rebuilt in CI (`npm ci && npm run build`), packed into tarball |
| systemd service files | `predictor.service`, `predictor-refresh.service`, `predictor-refresh.timer` |

**Service restart behavior**: The service starts in seconds (loads cached market pairs from `market_pairs` table). A full market re-fetch (~30 min) only runs when the DB is empty (first deploy) or when `predictor-refresh.service` is triggered. The weekly timer triggers this automatically every Sunday at 03:00 UTC.

### EU Proxy Setup

Polymarket CLOB API requires EU-region routing. A Dante SOCKS5 proxy runs on a separate e2-micro VM:

```
predictor-vm (us-central1-a)
  └─── POLYMARKET_PROXY=socks5://10.164.0.2:1080 ───► predictor-proxy (europe-west4-a)
                                                         └─── api.polymarket.com
```

The proxy IP `10.164.0.2` is the internal GCP address of the proxy VM. It's whitelisted in Dante's config to accept connections only from the trading VM's internal IP. Set via `.env`:
```
POLYMARKET_PROXY=socks5://10.164.0.2:1080
```

To verify the proxy is working:
```bash
# From the trading VM:
curl --socks5-hostname 10.164.0.2:1080 https://api.polymarket.com/markets?limit=1
```

## Trading Strategies

The system implements five strategy types (P1–P5). Each is assigned based on the detected violation type and triggers through `detect_violations_and_trade` (cross-platform) or `detect_single_platform_opportunities` (single-platform).

| Strategy | Where Detected | Signal |
|----------|---------------|--------|
| **P1 – Cross-Market Arb** | `ArbitrageEngine.on_price_update()` + `detect_violations_and_trade()` | Same event priced differently across Polymarket and Kalshi. Buy the cheap side, sell the expensive side simultaneously. |
| **P2 – Structured Event** | `detect_single_platform_opportunities()` | Same-platform event series where YES prices sum > 1.05 (over-pricing of mutually exclusive outcomes). SELL the most overpriced member. |
| **P3 – Calibration Bias** | `detect_single_platform_opportunities()` | Market price is far from center (>20% distance from 0.50), indicating systematic over- or under-pricing. Bet toward center. |
| **P4 – Liquidity Timing** | `detect_single_platform_opportunities()` | Market in a "transition zone" (0.15–0.35 or 0.65–0.85) where liquidity premium can be captured. |
| **P5 – Information Latency** | `detect_single_platform_opportunities()` | Wide bid-ask spread (≥8%) combined with an extreme price indicates market makers haven't caught up to recent information. Bet in the price direction. |

**Per-strategy slot caps** (applied in `detect_single_platform_opportunities`): P2 ≤ 15%, P3 ≤ 50%, P4 ≤ 25%, P5 ≤ remainder — so no single strategy can crowd out the others.

## Risk Controls

All monetary limits are expressed as percentages of portfolio value, so they scale automatically as the account grows or shrinks. Portfolio value is computed as `starting_capital + realized_pnl - fees`.

Risk checks are enforced in the execution handler before any order reaches an exchange client. Every check result is logged to the `risk_check_log` table for audit.

| Check | Config Variable | Default | Example ($10k portfolio) |
|-------|----------------|---------|--------------------------|
| Max position size | `MAX_POSITION_PCT` | 5% | $500 per trade |
| Daily loss limit | `MAX_DAILY_LOSS_PCT` | 2% | $200/day |
| Portfolio exposure cap | `MAX_PORTFOLIO_EXPOSURE_PCT` | 20% | $2,000 total deployed |
| Minimum edge | `MIN_EDGE_TO_TRADE` | 2% | Signal must clear 2% edge |
| Duplicate window | `DUPLICATE_SIGNAL_WINDOW_S` | 300s | No repeat trades within 5 min |
| Kelly fraction | `KELLY_FRACTION` | 0.25 | Quarter-Kelly sizing |

## Key Components

### Data Ingestion (`core/ingestor/`)
Async pollers for Polymarket (CLOB API) and Kalshi (REST API) that fetch market metadata, prices, volume, and liquidity on configurable intervals. Real-time websocket feeds provide sub-second price updates in stream mode.

### Constraint Engine (`core/constraints/`)
Evaluates four mathematical constraint rules across market pairs to detect arbitrage: subset/superset, mutual exclusivity, complementarity, and cross-platform price parity. Violations that exceed fee-adjusted thresholds are emitted as trading opportunities.

### Market Matching (`core/matching/`)
Pairs related markets across platforms using rule-based title matching and semantic embedding similarity (all-MiniLM-L6-v2). Matched pairs are stored in `market_pairs` and reused across restarts. A weekly systemd timer (`predictor-refresh.timer`) re-runs the matcher to pick up new markets without blocking the live service.

### Signal Generation (`core/signals/`)
Converts violations into actionable trading signals with Kelly criterion sizing and risk validation. The hardened signal queue (Redis-backed) provides message deduplication, a dead letter queue for failed signals, and backpressure monitoring.

### Prediction Models (`core/models/`)
Event-specific probability models (FOMC, CPI, calibration) with a registry pattern. The training pipeline (`scripts/train_models.py`) handles feature engineering, temporal train/test splits, and evaluation (Brier score, calibration bins, classification metrics). Results persist to the `model_evaluations` table.

### Execution (`execution/`)
Order routing with platform-specific clients:

- **Polymarket client** – CLOB API via `py-clob-client` with RSA-PSS auth; routes through SOCKS5 proxy for EU compliance
- **Kalshi client** – REST API with RSA-PSS (SHA-256) authentication
- **Paper client** – Executes against real market prices without placing orders; identical DB writes for analytics
- **Mock client** – Simulates fills with configurable latency, slippage, partial fills, and rejection rates
- **Order router** – Routes signal legs to the correct platform with retry logic and exponential backoff
- **Signal handler** – Enforces all risk checks before routing; rejects signals that fail any check; blocks all trading if reconciliation detects a discrepancy
- **Resolution monitor** – Detects market resolutions, closes positions with side-aware realized PnL, writes trade outcomes, and flags stale positions (open >72h)
- **Reconciliation engine** – Hourly balance check against both exchanges; halts trading if discrepancy exceeds threshold (default 5%)

The execution mode is controlled by `EXECUTION_MODE` in the environment: `mock` (simulated), `paper` (real prices, no orders), or `live` (real orders).

### Analytics Dashboard (`scripts/dashboard_api.py`, `dashboard/`)
React + FastAPI dashboard served from the same process as the trading session. Features: portfolio overview cards (capital, PnL, fees, trades), per-strategy performance table (win rate, Sharpe, edge capture), strategy PnL over time chart, equity curve, risk metrics (drawdown, VaR, concentration, Sharpe), fee breakdown by platform and strategy, circuit-breaker status, and a filterable trade log. Global time-range selector (1h to 30d) across all views.

### Event System (`core/events/`)
Async pub/sub event bus connecting all components. 13 event types with error isolation between subscribers.

## Configuration

All settings are loaded from environment variables. Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `EXECUTION_MODE` | `paper` | `mock`, `paper`, or `live` |
| `STARTING_CAPITAL` | `10000` | Starting capital for risk calculations |
| `DB_PATH` | `prediction_market.db` | SQLite database location |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection for signal queue |
| `KALSHI_API_KEY` | | Kalshi API key UUID |
| `KALSHI_RSA_KEY_PATH` | | Path to Kalshi RSA private key PEM |
| `POLYMARKET_PRIVATE_KEY` | | Ethereum hex private key for Polymarket |
| `POLYMARKET_WALLET_ADDRESS` | | Proxy wallet address |
| `POLYMARKET_PROXY` | | SOCKS5 proxy for EU routing (`socks5://host:port`) |
| `SECRETS_BACKEND` | `env` | `env` (read from .env) or `gcp` (GCP Secret Manager) |
| `GCP_PROJECT_ID` | | GCP project for Secret Manager lookups |
| `MAX_POSITION_PCT` | `0.05` | Max position size as % of portfolio |
| `MAX_DAILY_LOSS_PCT` | `0.02` | Daily loss limit as % of portfolio |
| `MAX_PORTFOLIO_EXPOSURE_PCT` | `0.20` | Max total deployed capital as % of portfolio |
| `KELLY_FRACTION` | `0.25` | Fractional Kelly multiplier |
| `LOG_FORMAT` | `text` | `json` for structured production logging |

## Database

SQLite with WAL mode. 20 tables organized around the trading lifecycle:

**Market data:** `markets`, `market_prices`, `ingestor_runs`
**Pair analysis:** `market_pairs`, `pair_spread_history`
**Trading pipeline:** `violations`, `signals`, `risk_check_log`, `signal_events`
**Execution:** `orders`, `order_events`, `positions`
**Analytics:** `pnl_snapshots`, `strategy_pnl_snapshots`, `trade_outcomes`
**ML:** `model_predictions`, `model_versions`, `model_evaluations`
**System:** `system_events`, `reconciliation_log`

Schema is in `core/storage/migrations/`. Query modules in `core/storage/queries/` provide 50+ typed async functions.

## Project Structure

```
prediction-market/
├── core/
│   ├── config.py              # Configuration (all env-based, percentage limits)
│   ├── analytics.py           # Post-trade analytics engine
│   ├── logging_config.py      # Structured logging (JSON/text)
│   ├── secrets.py             # Secret management (env or GCP Secret Manager)
│   ├── alerting.py            # Discord webhook alerts (severity levels)
│   ├── main.py                # Core service entry point
│   ├── constraints/           # Constraint engine (4 rules + fees)
│   ├── events/                # Async event bus (13 event types)
│   ├── ingestor/              # Market data pollers (Polymarket, Kalshi, external)
│   ├── matching/              # Market pair discovery (rule-based + semantic)
│   ├── models/                # Prediction models + training pipeline
│   ├── signals/               # Signal generation, risk, sizing, queue hardening
│   └── storage/               # Database, migrations, query modules
├── execution/
│   ├── main.py                # Execution service entry point
│   ├── handler.py             # Signal processing + risk enforcement
│   ├── router.py              # Order routing with retry logic
│   ├── state.py               # In-memory position management + DB recovery
│   ├── resolution.py          # Market resolution monitor + position closure
│   ├── reconciliation.py      # Exchange balance reconciliation + halt logic
│   ├── circuit_breaker.py     # Daily loss circuit breaker (sticky halt)
│   ├── models.py              # Shared Pydantic models (TradingSignal, OrderLeg)
│   └── clients/               # Platform clients (polymarket, kalshi, paper, mock)
├── dashboard/
│   ├── src/                   # React frontend (Vite + Tailwind + Recharts)
│   └── dist/                  # Built frontend (served by FastAPI)
├── scripts/
│   ├── paper_trading_session.py  # Single-command trading session + dashboard
│   ├── dashboard_api.py          # FastAPI dashboard server (embeddable)
│   ├── verify_prod_config.py     # Production config smoke test (secrets + alerting)
│   ├── train_models.py           # Model training CLI
│   ├── queue_admin.py            # Signal queue admin CLI
│   ├── run_mock_session.py       # Mock harness (synthetic data)
│   └── ...                       # Utilities (backfill, seed, validate, export)
├── tests/
│   ├── unit/                  # Unit tests (constraints, matching, models, risk, sizing, signal hardening, strategy assignment)
│   ├── integration/           # Integration tests (execution, ingestor, signal flow)
│   └── paper/                 # Paper trading tests (arb engine, initial sweep,
│                              #   all 5 strategies, matching, paper client, schema,
│                              #   resolution, reconciliation, state, circuit breaker,
│                              #   alerting, dashboard API, DB persistence)
├── deploy/
│   ├── provision.sh              # GCE VM provisioning (idempotent)
│   ├── provision_proxy.sh        # EU SOCKS5 proxy VM provisioning
│   ├── vm_setup.sh               # VM bootstrap (Python, Node, Redis)
│   ├── setup_proxy.sh            # Dante SOCKS5 proxy setup
│   ├── push.sh                   # Code deployment (tar + scp + systemd restart)
│   ├── setup_cicd.sh             # GitHub Actions service account setup
│   ├── predictor.service         # systemd unit (paper_trading_session.py --stream --dashboard)
│   ├── predictor-refresh.service # systemd oneshot for periodic market re-matching
│   ├── predictor-refresh.timer   # Timer: Sunday 03:00 UTC, Persistent=true
│   ├── env_merge.py              # Merge .env updates without overwriting existing keys
│   └── DEPLOY.md                 # Deployment documentation
├── .github/workflows/
│   ├── ci.yml                 # CI pipeline
│   └── deploy.yml             # Auto-deploy on release (9-step: build → scp → rsync → pip → systemd → secrets → health → Discord)
├── ROADMAP.md                 # Feature roadmap and go-live checklist
└── requirements.txt           # Python dependencies
```

## License

MIT
