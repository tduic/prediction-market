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
- **Polymarket client** – USDC-based trading via web3.py (EIP-712 signatures)
- **Kalshi client** – REST API with HMAC-SHA256 authentication
- **Mock client** – Simulates realistic fills with configurable latency, slippage, partial fills, and rejection rates
- **Order router** – Routes signal legs to the correct platform, handles retries with exponential backoff
- **Position state manager** – In-memory position tracking with periodic database flush

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
- Python 3.11+
- Redis (for live two-service mode; not needed for mock sessions)

### Setup
```bash
# Clone and install
git clone https://github.com/yourusername/prediction-market.git
cd prediction-market
pip install -r requirements.txt

# Configure
cp config/settings.example.env .env
# Edit .env with your settings (EXECUTION_MODE=mock for testing)

# Initialize database
sqlite3 data/pmtrader.db < core/storage/migrations/001_initial.sql
```

### Run a Mock Session
The mock session harness runs the complete trading lifecycle in-process without Redis:
```bash
python scripts/run_mock_session.py --num-markets 20 --num-violations 15 --verbose
```
This seeds markets, generates violations across all 5 strategies, executes through the mock router, records positions and PnL, and prints a formatted report.

### View Analytics Dashboard
```bash
python scripts/dashboard.py --db-path data/pmtrader.db --days 7
python scripts/dashboard.py --format json  # Machine-readable output
```

### Run Tests
```bash
pip install -r requirements-test.txt
pytest tests/ -v
```

### Run Live Services
```bash
# Terminal 1: Core service
python -m core.main

# Terminal 2: Execution service
python -m execution.main
```

## Configuration

All settings are loaded from environment variables. See `config/settings.example.env` for the full list. Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `EXECUTION_MODE` | `mock` | `mock` for simulated trading, `live` for real execution |
| `DATABASE_PATH` | `./data/pmtrader.db` | SQLite database location |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection for signal queue |
| `KELLY_FRACTION` | `0.25` | Fractional Kelly multiplier for position sizing |
| `MAX_POSITION_USD` | `100` | Maximum position size per trade |
| `DAILY_LOSS_LIMIT_USD` | `50` | Daily loss circuit breaker |
| `MIN_SPREAD_THRESHOLD` | `0.03` | Minimum spread to trigger a violation |

## Project Structure

```
prediction-market/
├── core/
│   ├── config.py              # Configuration management
│   ├── analytics.py           # Post-trade analytics engine
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
│   ├── run_mock_session.py    # Full lifecycle mock harness
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
