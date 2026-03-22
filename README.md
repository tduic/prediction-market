# Prediction Market Trading System

An async Python trading system for detecting and exploiting pricing inefficiencies across prediction market platforms (Polymarket, Kalshi). Covers the full quant lifecycle: data ingestion, arbitrage detection, signal generation, risk management, order execution, and post-trade analytics.

## Architecture

The system runs as two async services communicating via Redis:

```
┌─────────────────────────────┐       Redis        ┌─────────────────────────────┐
│        Core Service         │  ───signals───────► │     Execution Service       │
│                             │                     │                             │
│  Ingestor → Constraint      │                     │  Signal Handler → Router    │
│  Engine → Signal Generator  │                     │  → Platform Clients         │
│  → Risk Checks              │                     │  → Position State           │
└──────────┬──────────────────┘                     └──────────┬──────────────────┘
           │                                                   │
           └──────────────── SQLite (WAL) ─────────────────────┘
```

**Core Service** polls market data, detects arbitrage opportunities through constraint violations, generates trading signals with Kelly-criterion sizing, and validates them against risk limits.

**Execution Service** consumes signals, routes orders to the appropriate platform client, tracks fills, manages position state, and writes trade outcomes for analytics.

Both services share a SQLite database (16 tables, WAL mode for concurrent access).

## Trading Strategies

The system implements five strategy types (P1–P5):

| Strategy | Description |
|----------|-------------|
| **P1 – Cross-Market Arbitrage** | Exploits price discrepancies for the same event across Polymarket and Kalshi |
| **P2 – Structured Event Modeling** | Trades against mispriced structured events (FOMC, CPI) using specialized models |
| **P3 – Calibration Bias** | Identifies systematic over/under-pricing using calibration curves |
| **P4 – Liquidity Timing** | Captures temporary mispricings caused by low-liquidity periods |
| **P5 – Information Latency** | Exploits delayed price adjustments when one platform updates before another |

## Key Components

### Data Ingestion (`core/ingestor/`)
Async pollers for Polymarket (CLOB API) and Kalshi (REST API) that fetch market metadata, prices, volume, and liquidity on configurable intervals. An external data poller handles supplementary data sources (FRED, news APIs). All data is written to the `markets` and `market_prices` tables.

### Constraint Engine (`core/constraints/`)
Evaluates four mathematical constraint rules across market pairs to detect arbitrage:

- **Subset/Superset** – If event A implies event B, then P(A) ≤ P(B)
- **Mutual Exclusivity** – If A and B cannot both occur, then P(A) + P(B) ≤ 1
- **Complementarity** – If A and B are exhaustive complements, then P(A) + P(B) = 1
- **Cross-Platform** – Same event on two platforms should have the same price (net of fees)

Violations that exceed fee-adjusted thresholds are emitted as trading opportunities.

### Market Matching (`core/matching/`)
Pairs related markets across platforms using rule-based title matching and semantic embedding similarity. Matched pairs feed into the constraint engine for cross-platform analysis.

### Signal Generation (`core/signals/`)
Converts violations into actionable trading signals with:

- **Kelly criterion sizing** – Fractional Kelly (default 0.25×) for position sizing based on estimated edge
- **Risk validation** – Daily loss limits, max position size, portfolio exposure caps, max drawdown checks
- All risk checks are logged to `risk_check_log` for auditability

### Prediction Models (`core/models/`)
Event-specific probability models with a registry pattern:

- **FOMC model** – Fed rate decision probabilities from futures data
- **CPI model** – Inflation print probabilities from economic indicators
- **Calibration model** – Historical calibration bias detection

### Execution (`execution/`)
Order routing with platform-specific clients:

- **Polymarket client** – CLOB API via `py-clob-client` with two-stage authentication (credential derivation → signed order submission)
- **Kalshi client** – REST API with RSA-PSS (SHA-256) authentication, matching the production Kalshi API
- **Paper client** – Executes against real market prices without placing orders; identical DB writes to live mode for analytics
- **Mock client** – Simulates realistic fills with configurable latency, slippage, partial fills, and rejection rates
- **Order router** – Routes signal legs to the correct platform, handles retries with exponential backoff
- **Position state manager** – In-memory position tracking with periodic database flush
- **Rate limiting** – Token bucket rate limiters on both platform clients (10 req/s Kalshi, 5 req/s Polymarket)

The execution mode is controlled by `EXECUTION_MODE=mock|live` in the environment.

### Analytics (`core/analytics.py`, `scripts/dashboard.py`)
Post-trade analysis covering:

- Trade lifecycle tracking (fill → position → outcome)
- Per-strategy performance: Sharpe ratio, win rate, average PnL, edge capture
- Execution quality: fill rates, latency distributions, slippage by platform
- Risk metrics: max drawdown, market concentration
- Portfolio PnL snapshots over time

### Event System (`core/events/`)
Async pub/sub event bus connecting all components. 13 event types (MarketUpdated, ViolationDetected, SignalFired, OrderFilled, etc.) with error isolation between subscribers.

### Infrastructure

- **Structured logging** (`core/logging_config.py`) – JSON format for production (`LOG_FORMAT=json`), human-readable for development
- **Redis reconnection** – Execution service auto-reconnects with exponential backoff (up to 10 attempts) on connection loss
- **WAL checkpointing** – Scheduled database maintenance to keep WAL file size bounded
- **CI/CD** – GitHub Actions workflow runs tests and linting on Python 3.14

## Database

SQLite with WAL mode. 16 tables organized around the trading lifecycle:

**Market data:** `markets`, `market_prices`, `ingestor_runs`
**Pair analysis:** `market_pairs`, `pair_spread_history`
**Trading pipeline:** `violations`, `signals`, `risk_check_log`
**Execution:** `orders`, `order_events`, `positions`
**Analytics:** `pnl_snapshots`, `trade_outcomes`
**ML:** `model_predictions`, `model_versions`
**System:** `system_events`

Schema is in `core/storage/migrations/001_initial.sql`. Query modules in `core/storage/queries/` provide 50+ typed async functions covering all tables.

## Quick Start

### Prerequisites

- Python 3.14
- Redis (for live two-service mode; not needed for mock sessions)

### Setup
```bash
git clone https://github.com/tyjodu/prediction-market.git
cd prediction-market
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Configure
cp config/settings.example.env .env
# Edit .env with your API credentials (EXECUTION_MODE=mock for testing)
```

### Paper Trading Session

The paper trading system fetches real markets from Polymarket (~35k) and Kalshi (~30k), matches identical events across platforms, and executes simulated trades with full analytics.

```bash
# First run: fetch all markets, match across platforms, persist matches, trade once
python scripts/paper_trading_session.py --refresh

# Subsequent runs: use cached matches, trade once using stored prices
python scripts/paper_trading_session.py --once

# Continuous mode: cached matches + websocket price streaming, trades every 30s
python scripts/paper_trading_session.py --stream

# With options
python scripts/paper_trading_session.py --stream --interval 15 --min-spread 0.05
```

| Flag | Description |
|------|-------------|
| `--refresh` | Fetch all markets from both exchanges, run matcher, persist matched pairs to DB. Slow (~30s) but only needed to discover new matches. |
| `--once` | Load cached matches from DB, run one trading cycle, exit. Auto-refreshes if no cached matches exist. |
| `--stream` | Load cached matches, open websocket connections for real-time prices, run trading cycles continuously until Ctrl+C. |
| `--interval N` | Seconds between trading cycles in stream mode (default: 30). |
| `--min-spread X` | Minimum price spread to trigger a trade (default: 0.03). |

### Mock Session (Synthetic Data)
```bash
python scripts/run_mock_session.py --db-path prediction_market.db --num-markets 20 --num-violations 15 --verbose
```

### View Analytics Dashboard
```bash
python scripts/dashboard.py --days 7
python scripts/dashboard.py --format json
```

### Run Tests
```bash
pytest tests/ -v
python -m black --check core/ execution/ scripts/ tests/
python -m ruff check core/ execution/ scripts/ tests/
```

### Run Live Services
```bash
docker compose up -d   # Start Redis
python -m core.main    # Terminal 1
python -m execution.main  # Terminal 2
```

## Configuration

All settings are loaded from environment variables. See `config/settings.example.env` for the full list. Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `EXECUTION_MODE` | `mock` | `mock` for simulated trading, `live` for real execution |
| `DB_PATH` | `prediction_market.db` | SQLite database location |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection for signal queue |
| `KALSHI_API_KEY` | | Kalshi API key UUID |
| `KALSHI_RSA_KEY_PATH` | | Path to Kalshi RSA private key PEM file |
| `POLYMARKET_PRIVATE_KEY` | | Ethereum hex private key for Polymarket |
| `POLYMARKET_WALLET_ADDRESS` | | Proxy wallet address |
| `KELLY_FRACTION` | `0.25` | Fractional Kelly multiplier for position sizing |
| `MAX_POSITION_SIZE_USD` | `500` | Maximum position size per trade |
| `MAX_DAILY_LOSS_USD` | `200` | Daily loss circuit breaker |
| `LOG_FORMAT` | `text` | `json` for structured production logging |

## Project Structure

```
prediction-market/
├── core/
│   ├── config.py              # Configuration management
│   ├── analytics.py           # Post-trade analytics engine
│   ├── logging_config.py      # Structured logging (JSON/text)
│   ├── main.py                # Core service entry point
│   ├── constraints/           # Constraint engine (4 rules + fees)
│   ├── events/                # Async event bus
│   ├── ingestor/              # Market data pollers
│   ├── matching/              # Market pair discovery
│   ├── models/                # Prediction models (FOMC, CPI, calibration)
│   ├── signals/               # Signal generation, risk, sizing
│   └── storage/               # Database, migrations, query modules
├── execution/
│   ├── main.py                # Execution service entry point
│   ├── handler.py             # Signal processing
│   ├── router.py              # Order routing with retry logic
│   ├── state.py               # In-memory position management
│   ├── models.py              # Shared Pydantic models
│   └── clients/               # Platform clients (polymarket, kalshi, mock)
├── scripts/
│   ├── paper_trading_session.py # Paper trading with real market data + websocket streaming
│   ├── run_mock_session.py    # Full lifecycle mock harness (synthetic data)
│   ├── dashboard.py           # Analytics dashboard CLI
│   ├── export_analytics.py    # CSV export utility
│   ├── backfill_prices.py     # Historical price backfill
│   ├── seed_pairs.py          # Market pair seeding
│   └── validate_pairs.py      # Pair validation
├── tests/
│   ├── unit/                  # Unit tests (constraints, matching, models, risk, sizing)
│   └── integration/           # Integration tests (execution, ingestor, signal flow)
├── config/
│   └── settings.example.env   # Example environment configuration
├── .github/workflows/ci.yml   # GitHub Actions CI (tests + lint)
└── ROADMAP.md                 # Planned features and improvements
```

## Testing

144 tests across unit and integration suites:
```bash
pytest tests/ -v                    # All tests
pytest tests/unit/ -v               # Unit tests only
pytest tests/integration/ -v        # Integration tests only
```

## License

MIT
