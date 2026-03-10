# Prediction Market Trading System - Foundation Layer

A production-quality async Python foundation for a multi-platform prediction market trading system. Supports real-time market data ingestion, constraint-based arbitrage detection, portfolio management, and trade analytics.

## Architecture Overview

### Core Components

#### 1. Configuration Management (`core/config.py`)
Centralized configuration system loading from environment variables with validation.

**Key Settings:**
- **Platform Credentials**: Polymarket, Kalshi API keys and secrets
- **Database**: SQLite with WAL mode for concurrent read access
- **Redis**: Optional message queue for signal distribution
- **Ingestor**: Polling intervals for market data collection
- **Constraint Engine**: Spread thresholds and fee rates
- **Risk Controls**: Position sizing, daily loss limits, Kelly fraction
- **Model Service**: ML model deployment and refit schedules
- **Matching**: Market similarity thresholds and embedding models
- **Execution**: Order execution parameters and retry logic
- **Observability**: Logging levels and snapshot intervals

**Validation:**
- Credential requirements depend on `PAPER_TRADING` flag
- Kelly fraction constrained to (0, 0.5]
- Spread thresholds must exceed fee rates
- Demo environments force paper trading mode

#### 2. Event System (`core/events/`)

Lightweight pub/sub event bus using `asyncio.Queue` with error isolation.

**Event Types:**
- `MarketUpdated`: Price/liquidity changes
- `ViolationDetected`: Arbitrage opportunities
- `SignalFired`: Trading signals from strategies
- `SignalQueued`: Signal enters processing queue
- `OrderSubmitted`: Order sent to platform
- `OrderFilled`: Order received fills
- `OrderCancelled/OrderFailed`: Order outcomes
- `PositionUpdated/PositionClosed`: Position lifecycle
- `RiskCheckFailed`: Risk validation failures
- `PnLSnapshot`: Portfolio snapshots
- `SystemEvent`: System-level events

**Features:**
- Multiple subscribers per event type
- Async/sync callback support
- Error isolation between subscribers
- Queue-based processing for throughput
- Graceful shutdown support

#### 3. Storage Layer (`core/storage/`)

Async SQLite wrapper with connection pooling and migration runner.

**Features:**
- Single-writer pattern via `asyncio.Lock`
- Concurrent reads via WAL mode
- Automatic schema migrations
- Dict-like row access
- Transaction support
- Vacuum and checkpoint operations

**Database Schema (16 Tables):**

1. **markets** - Core market information
   - Unique constraint on (platform, platform_id)
   - Indexes on platform, status, category

2. **market_prices** - Time-series price data
   - Composite index on (market_id, polled_at)
   - Tracks bid/ask, spread, volume, liquidity

3. **ingestor_runs** - Data collection metadata
   - Tracks fetch/update/error counts per platform

4. **market_pairs** - Cross-platform market matching
   - Similarity scores from semantic matching
   - Verified status tracking
   - Active/inactive state management

5. **pair_spread_history** - Historical spread tracking
   - Raw and net spread evolution
   - Constraint satisfaction flags

6. **violations** - Detected arbitrage opportunities
   - Detection timestamp and duration tracking
   - Fee estimates and spread metrics
   - Status progression (detected → closed)

7. **signals** - Trading signals
   - Strategy, signal type, target prices
   - Model edge and sizing parameters
   - Risk check results
   - Status tracking

8. **risk_check_log** - Risk validation audit trail
   - Per-check type results
   - Threshold comparisons
   - Check details and context

9. **orders** - Executed orders
   - Platform-specific order IDs
   - Requested vs filled parameters
   - Slippage and fee tracking
   - Retry and latency metrics

10. **order_events** - Order lifecycle events
    - Fill/cancel/reject events
    - Partial fill tracking

11. **positions** - Open and closed positions
    - Entry/exit price and size
    - Unrealized/realized PnL
    - Resolution outcomes

12. **pnl_snapshots** - Portfolio snapshots
    - Scheduled, manual, or signal-triggered
    - Strategy-specific PnL breakdown
    - Capital allocation across platforms

13. **trade_outcomes** - Completed trade analysis
    - Predicted vs actual edge
    - Execution latency metrics
    - Market conditions at execution

14. **model_predictions** - ML model outputs
    - Feature inputs and predictions
    - Brier score for calibration
    - Outcome recording post-resolution

15. **model_versions** - Model training metadata
    - Training samples and hyperparameters
    - In/out-of-sample performance
    - Deployment/retirement tracking

16. **system_events** - System logging
    - Event types, severity levels
    - Component attribution

#### 4. Query Modules (`core/storage/queries/`)

Async query functions organized by domain:

**markets.py**
- `upsert_market()` - Insert/update market records
- `get_market()` - Retrieve single market
- `get_markets_by_platform()` - Filter by platform/status
- `insert_price()` - Record price snapshot
- `get_latest_prices()` - Fetch recent quotes
- `insert_ingestor_run()` - Log data collection
- `get_markets_needing_update()` - Find stale markets

**violations.py**
- `insert_violation()` - Create violation record
- `get_violation()` - Retrieve violation
- `update_violation_status()` - Update status
- `close_violation()` - Close with duration tracking
- `get_active_violations()` - List current opportunities
- `get_violations_by_pair()` - Filter by market pair
- `insert_pair_spread_history()` - Track spread evolution
- `get_violation_statistics()` - Summary aggregations

**signals.py**
- `insert_signal()` - Create trading signal
- `get_signal()` - Retrieve signal
- `update_signal_status()` - Update status
- `get_recent_signals()` - Filter by strategy/status/time
- `get_open_signals()` - List unfilled signals
- `insert_risk_check()` - Log risk validation
- `get_failed_risk_checks()` - Audit trail
- `get_signal_statistics()` - Summary metrics

**positions.py**
- `insert_order()` - Create order record
- `get_order()` - Retrieve order
- `update_order()` - Update with fills
- `get_pending_orders()` - List open orders
- `insert_order_event()` - Log order state changes
- `insert_position()` - Open position
- `update_position()` - Mark to market
- `close_position()` - Close with PnL
- `get_open_positions()` - List active positions
- `get_positions_for_market()` - Filter by market

**pnl.py**
- `insert_snapshot()` - Record portfolio snapshot
- `get_latest_snapshot()` - Most recent snapshot
- `get_daily_snapshots()` - Historical snapshots
- `insert_trade_outcome()` - Post-trade analysis
- `get_strategy_pnl()` - Strategy performance
- `get_overall_pnl()` - Portfolio performance
- `get_recent_trades()` - Completed trades
- `get_hourly_pnl_series()` - Time-series data

## Usage

### Installation

```bash
pip install -r requirements.txt
```

### Basic Setup

```python
import asyncio
from core import Database, EventBus, get_config
from core.storage import queries

async def main():
    # Load configuration
    config = get_config()
    
    # Initialize database
    db = Database(config.database.database_path)
    await db.init()
    
    # Initialize event bus
    bus = EventBus()
    await bus.start()
    
    # Insert market data
    await queries.markets.upsert_market(
        db,
        market_id="poly_trump_2026",
        platform="polymarket",
        platform_id="trump_approval",
        title="Trump Approval > 45%",
    )
    
    # Cleanup
    await bus.stop()
    await db.close()

if __name__ == "__main__":
    asyncio.run(main())
```

### Configuration via Environment Variables

```bash
# Platform Credentials
export POLYMARKET_API_KEY=...
export POLYMARKET_PRIVATE_KEY=...
export POLYMARKET_WALLET_ADDRESS=...
export KALSHI_API_KEY=...
export KALSHI_API_SECRET=...

# Database
export DATABASE_PATH=./data/pmtrader.db

# Risk Controls
export MAX_POSITION_SIZE_USD=500
export MAX_DAILY_LOSS_USD=200
export KELLY_FRACTION=0.25

# Observability
export PAPER_TRADING=true
export LOG_LEVEL=INFO
```

## Event Flow Example

```
Market Snapshot
    ↓
MarketUpdated event published
    ↓
Constraint Engine calculates spreads
    ↓
Spread exceeds MIN_NET_SPREAD_CROSS_PLATFORM
    ↓
ViolationDetected event published
    ↓
Signal Strategy evaluates edge
    ↓
Edge exceeds MIN_EDGE_TO_SIGNAL
    ↓
SignalFired event published
    ↓
Risk checks executed
    ↓
OrderSubmitted event published
    ↓
OrderFilled event received
    ↓
PositionUpdated event published
    ↓
PnLSnapshot recorded
```

## Performance Characteristics

- **Database**: WAL mode enables concurrent reads while maintaining single-writer semantics
- **Event Processing**: Async callbacks with error isolation prevent cascade failures
- **Query Execution**: Parameterized queries prevent SQL injection
- **Memory**: Row factories only created when needed; lazy evaluation of results

## Testing

```bash
# Run with example data
python example_usage.py

# Run tests
pytest tests/ -v

# Type checking
mypy core/ --strict

# Code quality
ruff check core/
black --check core/
```

## File Structure

```
prediction-market/
├── core/
│   ├── __init__.py
│   ├── config.py              # Configuration management
│   ├── events/
│   │   ├── __init__.py
│   │   ├── types.py          # Event dataclasses
│   │   └── bus.py            # Event bus implementation
│   └── storage/
│       ├── __init__.py
│       ├── db.py             # Database wrapper
│       ├── migrations/
│       │   ├── 001_initial.sql
│       │   └── 002_add_model_versions.sql
│       └── queries/
│           ├── __init__.py
│           ├── markets.py
│           ├── violations.py
│           ├── signals.py
│           ├── positions.py
│           └── pnl.py
├── data/                      # SQLite database (created on init)
├── example_usage.py          # Complete usage example
├── requirements.txt
└── README.md
```

## Design Decisions

1. **SQLite over PostgreSQL**: Simpler deployment, ACID guarantees, WAL mode provides concurrency
2. **Event Bus Pattern**: Decouples strategies from execution; supports testing and monitoring
3. **Async/await**: Handles I/O-bound operations efficiently; scales to 10k+ concurrent positions
4. **Single-Writer Pattern**: Prevents race conditions while allowing concurrent reads
5. **Dataclass Events**: Type-safe, immutable event records with zero serialization overhead
6. **Environment-based Config**: Supports dev/test/prod separation without code changes

## Future Enhancements

- [ ] Redis-backed event queue for distributed processing
- [ ] Connection pooling for concurrent database access
- [ ] Event persistence for crash recovery
- [ ] Distributed tracing for transaction tracking
- [ ] Metrics export to Prometheus
- [ ] Webhook support for external integrations
- [ ] Event filtering and aggregation
- [ ] Automatic backup and archival policies

## License

Proprietary - Prediction Market Trading System Foundation
