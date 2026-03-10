# Prediction Market Trading System Architecture

Production-quality Python async system for ingesting prediction market data, generating probability predictions, and executing trading signals.

## Overview

The system consists of three main components:

1. **Ingestor** - Polls prediction markets and external data feeds
2. **Model Service** - Generates fair value predictions
3. **Signal Generator** - Produces trading signals with risk management

## Directory Structure

```
core/
├── ingestor/
│   ├── __init__.py              # Exports main classes
│   ├── polymarket.py            # Polymarket API client
│   ├── kalshi.py                # Kalshi API client
│   ├── external.py              # CME, Cleveland Fed, Metaculus, BLS clients
│   └── scheduler.py             # APScheduler job management
├── models/
│   ├── __init__.py              # Exports main classes
│   ├── base.py                  # Abstract base model
│   ├── fomc.py                  # FOMC rate decision model
│   ├── cpi.py                   # CPI print Bayesian model
│   ├── calibration.py           # Calibration curve model
│   └── registry.py              # Model version registry
└── signals/
    ├── __init__.py              # Exports main classes
    ├── generator.py             # Signal generation engine
    ├── risk.py                  # Risk checks (5 total)
    └── sizing.py                # Kelly fraction & position sizing
```

## Component Details

### 1. INGESTOR (core/ingestor/)

#### PolymarketClient (`polymarket.py`)
- **Base URL**: `https://clob.polymarket.com`
- **Rate Limiting**: Token bucket (10 req/s)
- **Authentication**: HMAC-SHA256 signed requests
- **Methods**:
  - `poll_markets()` → List[MarketData] - paginated market fetch
  - `get_market(condition_id)` → MarketData - fetch single market
  - `get_orderbook(token_id)` → OrderBook - fetch order book

#### KalshiClient (`kalshi.py`)
- **Base URL**: `https://trading-api.kalshi.com/trade-api/v2`
- **Rate Limiting**: Token bucket (10 req/s)
- **Authentication**: HMAC-SHA256 signed requests
- **Methods**:
  - `poll_markets(status, category)` → List[MarketData]
  - `get_market(ticker)` → MarketData
  - `get_orderbook(ticker)` → OrderBook

#### External Feed Clients (`external.py`)
Four specialized scrapers/clients:

1. **CMEFedWatchScraper**
   - Scrapes CME FedWatch Tool (cmegroup.com)
   - Returns: FedWatchData with rate decision probabilities
   - Graceful degradation on HTML structure changes

2. **ClevelandFedScraper**
   - Fetches Cleveland Fed Nowcast predictions
   - Returns: NowcastData with GDP/CPI nowcast + uncertainty
   - Logs errors without failing pipeline

3. **MetaculusClient**
   - Connects to Metaculus API
   - Endpoint: `/api2/questions/`
   - Returns: List[MetaculusQuestion]

4. **BLSCalendarFetcher**
   - Parses BLS economic release calendar
   - Returns: List[BLSRelease]

#### Scheduler (`scheduler.py`)
APScheduler-based job orchestration:

- **IngestorScheduler** class manages all polling jobs
- Configurable intervals (environment variables):
  - `POLL_INTERVAL_POLYMARKET_S` (default 30s)
  - `POLL_INTERVAL_KALSHI_S` (default 30s)
  - `POLL_INTERVAL_EXTERNAL_S` (default 300s)
- Methods:
  - `register_polymarket_job()` - Register Polymarket poller
  - `register_kalshi_job()` - Register Kalshi poller
  - `register_external_job()` - Register external feeds poller
  - `register_custom_job()` - Register custom interval job
  - `start()` / `stop()` - Control scheduler
  - `get_job_status()` / `list_jobs()` - Monitor jobs

### 2. MODEL SERVICE (core/models/)

#### BaseModel (`base.py`)
Abstract base class for all models:

```python
class BaseModel(ABC):
    @property
    @abstractmethod
    def name(self) -> str: pass

    @property
    @abstractmethod
    def version(self) -> str: pass

    @abstractmethod
    def train(self, data: pd.DataFrame) -> None: pass

    @abstractmethod
    def predict(self, features: dict) -> float: pass

    @abstractmethod
    def validate_data(self, data: pd.DataFrame) -> bool: pass
```

#### FOMCModel (`fomc.py`)
FOMC rate decision predictor:
- **Features**: cme_implied_prob, days_to_decision, recent_fed_speakers_hawkish_pct
- **Algorithm**: Linear regression (sklearn)
- **Validation**: 5-fold cross-validation, walk-forward testing
- **Output**: Probability [0, 1]

#### CPIModel (`cpi.py`)
CPI print predictor using Bayesian updating:
- **Prior**: Cleveland Fed Nowcast (normal distribution)
- **Likelihood**: Consensus forecast
- **Output**: P(actual CPI > consensus) using posterior distribution
- **Calibration**: Historical error-based factor

#### CalibrationModel (`calibration.py`)
Post-hoc calibration of market prices:
- **Method**: Decile-based calibration curves per category
- **Features**: raw_probability, category
- **Output**: Adjusted probability
- **Bias Detection**: Identifies tail underpricing, round number clustering
- **Algorithm**: Linear interpolation within deciles

#### ModelRegistry (`registry.py`)
Model version lifecycle management:

```python
class ModelRegistry:
    register_version(model_name, version, metrics) → ModelVersion
    get_active_version(model_name) → Optional[ModelVersion]
    deploy_version(model_name, version) → bool
    retire_version(model_name, version) → bool
    list_versions(model_name) → List[ModelVersion]
```

Status enum: DRAFT → ACTIVE → DEPRECATED → RETIRED

### 3. SIGNAL GENERATOR (core/signals/)

#### SignalGenerator (`generator.py`)
Main signal generation engine:

```python
class SignalGenerator:
    async def process_violation(self, violation: ViolationDetected) -> Optional[Signal]
```

Flow:
1. Receive ViolationDetected event
2. Run risk checks (5 total)
3. Compute position sizing
4. Create Signal record in DB
5. Emit to Redis queue (or log in paper trading mode)

**Signal JSON Schema**:
```json
{
  "schema_version": "1.0",
  "signal_id": "uuid",
  "strategy": "prediction_market",
  "signal_type": "mispricing|arbitrage",
  "legs": [
    {
      "leg_id": "uuid",
      "market_id": "string",
      "platform": "polymarket|kalshi",
      "platform_market_id": "string",
      "side": "buy|sell",
      "order_type": "limit|market",
      "target_price": 0.45,
      "price_tolerance": 0.02,
      "size_usd": 500.0,
      "expiry_s": 300
    }
  ],
  "execution_mode": "live|paper",
  "abort_on_partial": true,
  "max_total_slippage_usd": 100.0,
  "fired_at": "2026-03-10T15:30:00Z",
  "ttl_s": 300
}
```

#### Risk Checks (`risk.py`)
Five sequential risk checks with full logging:

1. **check_position_limit**
   - Signal size ≤ max_position_size_usd (default $5k)
   - RiskCheckResult(passed, check_type, value, threshold, detail)

2. **check_daily_loss_limit**
   - Realized loss today < max_daily_loss_usd (default $10k)
   - Queries DB for realized PnL

3. **check_concentration**
   - Portfolio concentration ≤ max_concentration_pct (default 30%)
   - (total_exposure + signal_size) / bankroll

4. **check_duplicate_signal**
   - No recent signals (< 5 min) on same markets
   - Prevents rapid re-signaling

5. **check_min_edge**
   - |edge| ≥ min_edge (default 2%)
   - Ensures minimum profitability threshold

Function:
```python
async def run_all_checks(
    signal, config, db
) -> tuple[bool, list[RiskCheckResult]]
```

#### Position Sizing (`sizing.py`)
Kelly-based position sizing:

```python
def compute_kelly_fraction(
    edge: float,           # fair_value - market_price
    odds: float,          # market_price (0-1)
    kelly_fraction: float # default 0.25 (quarter-Kelly)
) -> float
```

Kelly Criterion: f* = (bp - q) / b
- **Safety**: Quarter-Kelly (0.25) default, 0.5 hard cap
- **Handling short**: Negative edge → short position

```python
def compute_position_size(
    kelly_f: float,       # Kelly fraction (-0.5 to 0.5)
    bankroll: float,      # Total capital
    max_size: float       # Position size ceiling (default $10k)
) -> float
```

Position = kelly_f * bankroll, capped at max_size

Optional risk-adjusted sizing:
```python
compute_risk_adjusted_sizing(
    kelly_f, bankroll, max_size,
    volatility=None,      # Higher vol → smaller position
    confidence=None       # Lower confidence → smaller position
) -> float
```

## Data Classes

### Core Data Types

**MarketData** (polymarket.py)
```python
@dataclass
class MarketData:
    market_id: str
    platform: str          # "polymarket" | "kalshi"
    symbol: str
    question: str
    description: str
    resolution_date: Optional[datetime]
    last_price: float      # [0, 1]
    order_book: Optional[OrderBook]
    is_active: bool
    metadata: dict
```

**OrderBook** (polymarket.py)
```python
@dataclass
class OrderBook:
    token_id: str
    bids: list[dict]
    asks: list[dict]
    mid_price: Optional[float]
    timestamp: datetime
```

**Signal** (generator.py)
```python
@dataclass
class Signal:
    schema_version: str
    signal_id: str
    strategy: str
    signal_type: str
    legs: list[SignalLeg]
    execution_mode: ExecutionMode  # LIVE | PAPER
    abort_on_partial: bool
    max_total_slippage_usd: float
    fired_at: datetime
    ttl_s: int
```

**RiskCheckResult** (risk.py)
```python
@dataclass
class RiskCheckResult:
    passed: bool
    check_type: str
    check_value: float
    threshold: float
    detail: str
```

## Configuration

Environment variables:
```bash
# API Keys
POLYMARKET_API_KEY=xxx
POLYMARKET_API_SECRET=xxx
KALSHI_API_KEY=xxx
KALSHI_API_SECRET=xxx

# Polling Intervals (seconds)
POLL_INTERVAL_POLYMARKET_S=30
POLL_INTERVAL_KALSHI_S=30
POLL_INTERVAL_EXTERNAL_S=300

# Risk Management
MAX_POSITION_SIZE_USD=5000
MAX_DAILY_LOSS_USD=10000
MAX_CONCENTRATION_PCT=0.30
MIN_EDGE=0.02

# Sizing
KELLY_FRACTION=0.25      # Quarter-Kelly
BANKROLL_USD=100000
ORDER_EXPIRY_S=300

# Execution
PAPER_TRADING=false
STRATEGY_NAME=prediction_market
```

## Usage Examples

### Initialize Ingestor

```python
from core.ingestor import PolymarketClient, KalshiClient, IngestorScheduler

# Create clients
async with PolymarketClient() as pm:
    markets = await pm.poll_markets()

async with KalshiClient() as k:
    markets = await k.poll_markets(status="active")

# Schedule jobs
scheduler = IngestorScheduler(
    polymarket_poll_interval_s=30,
    kalshi_poll_interval_s=30
)

job_id = scheduler.register_polymarket_job(
    job_func=pm.poll_markets,
    job_name="poll_polymarket"
)

await scheduler.start()
```

### Train and Use Models

```python
from core.models import FOMCModel, ModelRegistry

# Train FOMC model
model = FOMCModel()
model.train(df_historical)

# Predict
prob = model.predict({
    "cme_implied_prob": 0.65,
    "days_to_decision": 14,
    "recent_fed_speakers_hawkish_pct": 0.45
})

# Register and deploy
registry = ModelRegistry()
registry.register_version("fomc", "1.0.0", metrics={"r2": 0.82})
registry.deploy_version("fomc", "1.0.0")
```

### Generate Signals

```python
from core.signals import SignalGenerator, ViolationDetected

generator = SignalGenerator(
    event_bus=event_bus,
    db=db,
    config={
        "bankroll_usd": 100000,
        "kelly_fraction": 0.25,
        "max_position_size_usd": 5000,
        "min_edge": 0.02,
    }
)

violation = ViolationDetected(
    violation_id="v123",
    market_id="m456",
    platform="polymarket",
    violation_type="mispricing",
    fair_value=0.65,
    market_price=0.60,
    edge=0.05
)

signal = await generator.process_violation(violation)
# Signal emitted to Redis queue (or logged in paper trading)
```

## Async/Await Pattern

All I/O operations use async/await:
- HTTP clients: httpx.AsyncClient
- Database operations: async methods
- Event publishing: async methods
- Scheduler: AsyncIOScheduler

## Error Handling

- **API errors**: Logged and skipped, job continues
- **Parse errors**: Logged with system_events, graceful degradation
- **Risk check failures**: Signal rejected, logged, no queue emit
- **DB errors**: Logged, checks may pass if DB unavailable

## Performance Characteristics

- **Polymarket polling**: ~10-20ms per call (rate limited)
- **Kalshi polling**: ~10-20ms per call (rate limited)
- **External feeds**: ~100-500ms per call (HTML scraping)
- **Model prediction**: <5ms (in-memory inference)
- **Risk checks**: ~50-200ms (may query DB)
- **Position sizing**: <1ms (pure math)

## Testing

All modules use type annotations. Verify with:
```bash
python3 -m py_compile core/**/*.py
mypy core/ --strict  # Optional
```

## Dependencies

```
httpx>=0.24.0          # Async HTTP client
beautifulsoup4>=4.12   # HTML scraping
apscheduler>=3.10.4    # Job scheduling
pandas>=2.0.0          # Data manipulation
numpy>=1.24.0          # Numerical computing
scikit-learn>=1.3.0    # Linear regression
scipy>=1.11.0          # Statistical functions
```

## Production Considerations

1. **Database Layer**: Implement async DB connection pool
2. **Event Bus**: Connect to Redis/RabbitMQ for signal emission
3. **Monitoring**: Add Prometheus metrics export
4. **Logging**: Configure structured JSON logging
5. **Graceful Shutdown**: Handle SIGTERM in scheduler
6. **Health Checks**: Implement readiness/liveness endpoints
7. **Rate Limiting**: Adjust token bucket based on API limits
8. **Backpressure**: Queue signals in memory if emission fails
