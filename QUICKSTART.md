# Quick Start Guide

## Installation

```bash
cd /sessions/zealous-determined-johnson/mnt/Predictor/prediction-market/

# Install dependencies
pip install httpx beautifulsoup4 apscheduler pandas numpy scikit-learn scipy

# Verify all files compile
python3 -m py_compile core/ingestor/*.py core/models/*.py core/signals/*.py
```

## Basic Usage

### 1. Poll Prediction Markets

```python
import asyncio
from core.ingestor import PolymarketClient, KalshiClient

async def main():
    # Polymarket
    async with PolymarketClient() as pm:
        markets = await pm.poll_markets(limit=100)
        print(f"Polymarket markets: {len(markets)}")
        for m in markets[:3]:
            print(f"  {m.symbol}: ${m.last_price:.2f}")

    # Kalshi
    async with KalshiClient() as k:
        markets = await k.poll_markets(status="active")
        print(f"Kalshi markets: {len(markets)}")

asyncio.run(main())
```

### 2. Train a Model

```python
import pandas as pd
from core.models import FOMCModel

# Prepare historical data
data = pd.DataFrame({
    'cme_implied_prob': [0.60, 0.65, 0.70, ...],
    'days_to_decision': [30, 25, 20, ...],
    'recent_fed_speakers_hawkish_pct': [0.40, 0.45, 0.50, ...],
    'final_market_price': [0.62, 0.67, 0.72, ...],
})

# Train
model = FOMCModel()
model.train(data)

# Predict
prob = model.predict({
    'cme_implied_prob': 0.65,
    'days_to_decision': 14,
    'recent_fed_speakers_hawkish_pct': 0.45
})
print(f"P(rate hike) = {prob:.2%}")
```

### 3. Generate Trading Signal

```python
import asyncio
from core.signals import SignalGenerator, ViolationDetected

async def main():
    generator = SignalGenerator(
        event_bus=None,  # Set to actual bus later
        db=None,         # Set to actual DB later
        config={
            'bankroll_usd': 100000,
            'kelly_fraction': 0.25,
            'max_position_size_usd': 5000,
            'min_edge': 0.02,
            'paper_trading': True,  # Start in paper trading
        }
    )

    violation = ViolationDetected(
        violation_id='v1',
        market_id='m1',
        platform='polymarket',
        violation_type='mispricing',
        fair_value=0.65,
        market_price=0.60,
        edge=0.05,
    )

    signal = await generator.process_violation(violation)
    if signal:
        print(f"Signal generated: {signal.signal_id}")
        print(f"Signal details: {signal.to_dict()}")

asyncio.run(main())
```

### 4. Schedule Polling Jobs

```python
import asyncio
from core.ingestor import PolymarketClient, IngestorScheduler

async def main():
    # Create clients
    pm = PolymarketClient()
    scheduler = IngestorScheduler(
        polymarket_poll_interval_s=30,
        kalshi_poll_interval_s=30,
        external_poll_interval_s=300,
    )

    # Register jobs
    async with pm:
        scheduler.register_polymarket_job(
            job_func=pm.poll_markets,
            job_name='poll_polymarket'
        )

        # Start scheduler
        await scheduler.start()

        # Let it run for 10 seconds
        await asyncio.sleep(10)

        # Check job status
        status = scheduler.get_job_status('poll_polymarket')
        print(f"Job status: {status}")

        # Stop scheduler
        await scheduler.stop()

asyncio.run(main())
```

## Configuration

Set environment variables:

```bash
# API Keys
export POLYMARKET_API_KEY=your_key
export POLYMARKET_API_SECRET=your_secret
export KALSHI_API_KEY=your_key
export KALSHI_API_SECRET=your_secret

# Polling (seconds)
export POLL_INTERVAL_POLYMARKET_S=30
export POLL_INTERVAL_KALSHI_S=30
export POLL_INTERVAL_EXTERNAL_S=300

# Risk Management
export MAX_POSITION_SIZE_USD=5000
export MAX_DAILY_LOSS_USD=10000
export MAX_CONCENTRATION_PCT=0.30
export MIN_EDGE=0.02

# Sizing
export KELLY_FRACTION=0.25
export BANKROLL_USD=100000

# Execution
export PAPER_TRADING=false
```

## Documentation

- **ARCHITECTURE.md** - Detailed system design
- **INDEX.md** - Complete reference guide
- **FILES_SUMMARY.md** - File-by-file breakdown
- **IMPLEMENTATION_COMPLETE.md** - Full status report

## Key Classes

### Ingestor
- `PolymarketClient` - Polymarket API
- `KalshiClient` - Kalshi API
- `CMEFedWatchScraper` - CME data
- `ClevelandFedScraper` - Fed Nowcast
- `MetaculusClient` - Metaculus API
- `BLSCalendarFetcher` - BLS data
- `IngestorScheduler` - Job coordination

### Models
- `BaseModel` - Abstract base
- `FOMCModel` - FOMC predictions
- `CPIModel` - CPI predictions (Bayesian)
- `CalibrationModel` - Probability calibration
- `ModelRegistry` - Version management

### Signals
- `SignalGenerator` - Signal generation
- `RiskCheckResult` - Risk check results
- Functions: `run_all_checks()`, `compute_kelly_fraction()`, `compute_position_size()`

## Common Tasks

### Check if trained
```python
model = FOMCModel()
if model.is_trained():
    print("Model ready")
else:
    print("Model not trained")
```

### List models in registry
```python
registry = ModelRegistry()
versions = registry.list_versions("fomc")
for v in versions:
    print(f"{v.version}: {v.status}")
```

### Run risk checks manually
```python
from core.signals.risk import run_all_checks

all_passed, results = await run_all_checks(signal, config, db)
for result in results:
    print(f"{result.check_type}: {result.detail}")
```

### Calculate position size
```python
from core.signals.sizing import compute_kelly_fraction, compute_position_size

kelly_f = compute_kelly_fraction(
    edge=0.05,
    odds=0.60,
    kelly_fraction=0.25
)

size = compute_position_size(
    kelly_f=kelly_f,
    bankroll=100000,
    max_size=5000
)
print(f"Position: ${size:.2f}")
```

## Testing

All modules pass syntax checks:
```bash
python3 -m py_compile core/**/*.py
```

Type checking (optional):
```bash
pip install mypy
mypy core/ --strict
```

## Next Steps

1. Implement database layer (PostgreSQL + asyncpg)
2. Connect event bus (Redis/RabbitMQ)
3. Add unit tests (pytest + asyncio)
4. Implement execution layer (order placement)
5. Add monitoring (Prometheus + Grafana)

---

See ARCHITECTURE.md for full system design.
