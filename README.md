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

**Execution Service** consumes signals, enforces risk checks (position limits, daily loss, exposure caps, deduplication, minimum edge), routes orders to the appropriate platform client, tracks fills, manages position state, monitors market resolutions to close positions and record realized PnL, and runs periodic exchange reconciliation to halt trading if local state drifts from exchange-reported balances.

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
# Single command: fetch markets, stream prices, trade continuously, serve dashboard
python scripts/paper_trading_session.py --refresh --stream --dashboard

# Then open http://localhost:8000
```

| Flag | Description |
|------|-------------|
| `--refresh` | Fetch all markets from both exchanges, run matcher, persist matched pairs. Slow (~30s) but only needed to discover new matches. |
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
# 298 tests covering constraints, matching, models, risk, sizing,
# execution, ingestor, signal flow, arb engine, paper client,
# schema compliance, resolution, reconciliation, and state management.
```

### GCP Deployment

The system deploys to a GCE `e2-medium` VM (us-central1-a) with a separate persistent disk for SQLite. A lightweight `e2-micro` VM in europe-west4-a runs a Dante SOCKS5 proxy for Polymarket API calls.

```bash
# 1. Provision infrastructure (idempotent)
bash deploy/provision.sh          # Main VM
bash deploy/provision_proxy.sh    # EU proxy VM

# 2. Bootstrap VMs
bash deploy/vm_setup.sh           # Install Python 3.12, Node.js, Redis
bash deploy/setup_proxy.sh        # Install Dante SOCKS5

# 3. Deploy code
bash deploy/push.sh               # Tar, scp, extract, pip install, build dashboard, restart systemd

# 4. Access dashboard via SSH tunnel
ssh -L 8000:localhost:8000 predictor@<VM_IP>
# Then open http://localhost:8000
```

GitHub Actions (`deploy.yml`) auto-deploys on release publication.

### Run Live Services (Two-Service Mode)
```bash
docker compose up -d   # Start Redis
python -m core.main    # Terminal 1
python -m execution.main  # Terminal 2
```

## Trading Strategies

The system implements five strategy types (P1–P5):

| Strategy | Description |
|----------|-------------|
| **P1 – Cross-Market Arb** | Exploits price discrepancies for the same event across Polymarket and Kalshi |
| **P2 – Event Modeling** | Trades against mispriced structured events (FOMC, CPI) using specialized models |
| **P3 – Calibration Bias** | Identifies systematic over/under-pricing using calibration curves |
| **P4 – Liquidity Timing** | Captures temporary mispricings caused by low-liquidity periods |
| **P5 – Info Latency** | Exploits delayed price adjustments when one platform updates before another |

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
Pairs related markets across platforms using rule-based title matching and semantic embedding similarity. Matched pairs feed into the constraint engine for cross-platform analysis.

### Signal Generation (`core/signals/`)
Converts violations into actionable trading signals with Kelly criterion sizing and risk validation. The hardened signal queue provides message deduplication, a dead letter queue for failed signals, and backpressure monitoring.

### Prediction Models (`core/models/`)
Event-specific probability models (FOMC, CPI, calibration) with a registry pattern. The training pipeline (`scripts/train_models.py`) handles feature engineering, temporal train/test splits, and evaluation (Brier score, calibration bins, classification metrics). Results persist to the `model_evaluations` table.

### Execution (`execution/`)
Order routing with platform-specific clients:

- **Polymarket client** – CLOB API via `py-clob-client` with two-stage authentication
- **Kalshi client** – REST API with RSA-PSS (SHA-256) authentication
- **Paper client** – Executes against real market prices without placing orders; identical DB writes for analytics
- **Mock client** – Simulates fills with configurable latency, slippage, partial fills, and rejection rates
- **Order router** – Routes signal legs to the correct platform with retry logic and exponential backoff
- **Signal handler** – Enforces all risk checks before routing; rejects signals that fail any check; blocks all trading if reconciliation detects a discrepancy
- **Resolution monitor** – Detects market resolutions, closes positions with side-aware realized PnL, writes trade outcomes, and flags stale positions (open >72h)
- **Reconciliation engine** – Hourly balance check against both exchanges; compares local order history to exchange-reported balances; halts trading if discrepancy exceeds threshold (default 5%)

The execution mode is controlled by `EXECUTION_MODE` in the environment: `mock` (simulated), `paper` (real prices, no orders), or `live` (real orders).

### Analytics Dashboard (`scripts/dashboard_api.py`, `dashboard/`)
React + FastAPI dashboard served from the same process as the trading session. Features: portfolio overview cards (capital, PnL, fees, trades), per-strategy performance table (win rate, Sharpe, edge capture), strategy PnL over time chart, equity curve, risk metrics (drawdown, VaR, concentration, Sharpe), fee breakdown by platform and strategy, and a filterable trade log. Global time-range selector (1h to 30d) across all views.

### Event System (`core/events/`)
Async pub/sub event bus connecting all components. 13 event types with error isolation between subscribers.

## Configuration

All settings are loaded from environment variables. Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `EXECUTION_MODE` | `paper` | `mock`, `paper`, or `live` |
| `STARTING_CAPITAL` | `10000` | Paper trading starting capital |
| `DB_PATH` | `prediction_market.db` | SQLite database location |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection for signal queue |
| `KALSHI_API_KEY` | | Kalshi API key UUID |
| `KALSHI_RSA_KEY_PATH` | | Path to Kalshi RSA private key PEM |
| `POLYMARKET_PRIVATE_KEY` | | Ethereum hex private key for Polymarket |
| `POLYMARKET_WALLET_ADDRESS` | | Proxy wallet address |
| `POLYMARKET_PROXY` | | SOCKS5 proxy for EU routing (`socks5://host:port`) |
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
│   ├── main.py                # Core service entry point
│   ├── constraints/           # Constraint engine (4 rules + fees)
│   ├── events/                # Async event bus
│   ├── ingestor/              # Market data pollers
│   ├── matching/              # Market pair discovery
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
│   ├── models.py              # Shared Pydantic models
│   └── clients/               # Platform clients (polymarket, kalshi, paper, mock)
├── dashboard/
│   ├── src/                   # React frontend (Vite + Tailwind + Recharts)
│   └── dist/                  # Built frontend (served by FastAPI)
├── scripts/
│   ├── paper_trading_session.py  # Single-command paper trading + dashboard
│   ├── dashboard_api.py          # FastAPI dashboard server (embeddable)
│   ├── train_models.py           # Model training CLI
│   ├── queue_admin.py            # Signal queue admin CLI
│   ├── run_mock_session.py       # Mock harness (synthetic data)
│   └── ...                       # Utilities (backfill, seed, validate, export)
├── tests/
│   ├── unit/                  # Unit tests (constraints, matching, models, risk, sizing, signal hardening)
│   ├── integration/           # Integration tests (execution, ingestor, signal flow)
│   └── paper/                 # Paper trading tests (arb engine, matching, paper client, schema,
│                              #   resolution, reconciliation, state management)
├── deploy/
│   ├── provision.sh           # GCE VM provisioning (idempotent)
│   ├── provision_proxy.sh     # EU SOCKS5 proxy VM provisioning
│   ├── vm_setup.sh            # VM bootstrap (Python, Node, Redis)
│   ├── setup_proxy.sh         # Dante SOCKS5 proxy setup
│   ├── push.sh                # Code deployment (tar + scp + systemd restart)
│   ├── setup_cicd.sh          # GitHub Actions service account setup
│   ├── predictor.service      # systemd unit file
│   └── DEPLOY.md              # Deployment documentation
├── .github/workflows/
│   ├── ci.yml                 # CI pipeline
│   └── deploy.yml             # Auto-deploy on release publication
├── ROADMAP.md                 # Feature roadmap and go-live checklist
└── requirements.txt           # Python dependencies
```

## License

MIT
