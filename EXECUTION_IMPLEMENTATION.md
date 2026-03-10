# Execution Service & Scripts Implementation Guide

## Overview

This document describes the complete execution service and utility scripts for the prediction market trading system. All components are production-quality, fully async, with comprehensive error handling and type annotations.

## Directory Structure

```
prediction-market/
├── execution/
│   ├── __init__.py
│   ├── main.py              # Entry point for execution service
│   ├── handler.py           # Signal validation and processing
│   ├── router.py            # Order routing with retry logic
│   ├── state.py             # Position state management
│   └── clients/
│       ├── __init__.py
│       ├── polymarket.py    # Polygon/web3.py client
│       └── kalshi.py        # REST API client
├── scripts/
│   ├── backfill_prices.py   # Historical price backfill
│   ├── seed_pairs.py        # Load curated pairs
│   ├── validate_pairs.py    # Interactive pair validation
│   └── export_analytics.py  # CSV export tools
├── config/
│   ├── settings.example.env # Configuration template
│   └── pairs_seed.json      # Initial market pairs
├── core/
│   └── main.py              # Core service entry point
├── docker-compose.yml       # Redis + optional Postgres
├── requirements.txt         # Production dependencies
├── requirements-dev.txt     # Development dependencies
└── EXECUTION_IMPLEMENTATION.md  # This file
```

## Execution Service Architecture

### Main Components

#### 1. ExecutionService (execution/main.py)
**Responsibilities:**
- Redis queue consumption (BRPOP blocking with 1-second timeout)
- Signal deserialization and validation
- Signal handler delegation
- Processed signal tracking for idempotency
- Graceful shutdown on SIGINT/SIGTERM

**Key Features:**
- Idempotent processing: maintains in-memory set of processed signal_ids
- Database-backed duplicate detection: queries order_events table
- Logging at every step for debugging
- Proper async context management

**Usage:**
```python
service = ExecutionService(
    redis_url="redis://localhost:6379",
    signal_queue_name="trading_signals",
    db_path="prediction_market.db"
)
await service.run()
```

#### 2. SignalHandler (execution/handler.py)
**Responsibilities:**
- Pydantic schema validation for TradingSignal and OrderLeg
- TTL expiration checking
- Parameter validation independent from core (defense-in-depth)
- Signal intent logging to database

**Validation Logic:**
```
1. Schema validation (TradingSignal model)
   - schema_version == "1.0"
   - All required fields present
   - expires_at_utc > now (TTL check)

2. Per-leg parameter validation
   - Size bounds (0 < size <= 10000)
   - Price bounds for LIMIT orders (0 <= price <= 1)
   - Market existence verification
   - Platform validation (polymarket or kalshi)

3. Routing to OrderRouter
```

**Database Logging:**
- signal_events table tracks: signal_id, status, details, timestamp

#### 3. OrderRouter (execution/router.py)
**Responsibilities:**
- Route orders to appropriate platform client
- Implement execution modes (simultaneous vs sequential)
- Exponential backoff retry logic
- Abort on partial fills
- Order event logging

**Retry Logic:**
```
MAX_ORDER_RETRIES = 3
RETRY_BACKOFF_BASE_S = 1

Attempt 1: immediate
Attempt 2: wait 1s (2^0)
Attempt 3: wait 2s (2^1)
```

**Execution Modes:**

*Simultaneous:*
```python
tasks = [
    route_order(leg1),
    route_order(leg2)
]
results = await asyncio.gather(*tasks)
```

*Sequential:*
```python
result1 = await route_order(leg1)
if result1.status == "ACCEPTED":
    await asyncio.sleep(0.5)
    result2 = await route_order(leg2)
```

**Abort on Partial:**
```python
if abort_on_partial:
    failed_legs = [r for r in results if r.status == "REJECTED"]
    if failed_legs:
        await _cancel_all_orders(accepted_results)
```

#### 4. Platform Clients

**PolymarketExecutionClient (execution/clients/polymarket.py)**
- Uses web3.py for Polygon network
- USDC pre-approval before market orders
- Transaction confirmation waiting (1-3 blocks, ~2-6 seconds)
- Handles ContractLogicError and TransactionFailed exceptions
- Records submission and fill latencies

```python
# Pre-approve USDC
await client._approve_usdc_spending(amount)

# Submit order
result = await client.submit_order(leg)
# Returns: OrderResult with order_id, status, latencies
```

**KalshiExecutionClient (execution/clients/kalshi.py)**
- REST API client with HMAC-SHA256 authentication
- POST /orders for submissions
- DELETE /orders/{order_id} for cancellations
- GET /orders/{order_id} for status checks
- Handles timeouts and HTTP errors gracefully

```python
# Sign request
headers = await client._sign_request("POST", "/orders", body_json)

# Submit order
response = await http_client.post(
    "https://trading-api.kalshi.com/trade-api/v2/orders",
    content=body_json,
    headers=headers
)
```

#### 5. PositionStateManager (execution/state.py)
**Responsibilities:**
- In-memory position tracking with write-through to database
- PnL calculation and updates
- Periodic flush to database (configurable interval)
- Position closure and exposure calculation

**Data Structure:**
```python
@dataclass
class Position:
    position_id: str
    market_id: str
    platform: str
    side: str  # "BUY" or "SELL"
    quantity: float
    entry_price: float
    entry_timestamp: float
    current_price: Optional[float] = None
    unrealized_pnl: float = 0.0
```

**PnL Calculation:**
```python
if side == "BUY":
    unrealized_pnl = (current_price - entry_price) * quantity
else:  # SELL
    unrealized_pnl = (entry_price - current_price) * quantity
```

## Utility Scripts

### 1. backfill_prices.py
**Purpose:** Load historical market prices into the database

**Usage:**
```bash
python scripts/backfill_prices.py \
    --platform polymarket \
    --since 30 \
    --until 2026-03-10 \
    --db prediction_market.db
```

**Features:**
- Fetches from Polymarket API: `/markets` and `/markets/{id}/history`
- Fetches from Kalshi API: `/markets` and `/markets/{id}/history`
- Progress bars with tqdm for large datasets
- INSERT OR IGNORE for idempotency
- Handles API errors gracefully

### 2. seed_pairs.py
**Purpose:** Load curated market pairs from JSON into the database

**Usage:**
```bash
python scripts/seed_pairs.py \
    --file config/pairs_seed.json \
    --db prediction_market.db
```

**Features:**
- Pydantic validation of pair structure
- Marks pairs as verified=1 and match_method='manual'
- Prevents duplicate insertions
- Detailed logging of validation errors

**Input Format:**
```json
{
  "pairs": [
    {
      "name": "Pair Name",
      "description": "Description",
      "category": "Category",
      "resolution_criteria": "How to resolve",
      "leg_a": {
        "platform": "polymarket",
        "market_id": "market_id",
        "description": "Description"
      },
      "leg_b": {
        "platform": "kalshi",
        "market_id": "market_id",
        "description": "Description"
      }
    }
  ]
}
```

### 3. validate_pairs.py
**Purpose:** Interactively review and approve/reject auto-discovered pairs

**Usage:**
```bash
python scripts/validate_pairs.py \
    --status unverified \
    --db prediction_market.db
```

**Status Options:**
- `unverified`: Only unverified pairs (default)
- `verified`: Only verified pairs
- `all`: All pairs

**Interactive Prompts:**
- `(a)ccept`: Mark pair as verified=1
- `(r)eject`: Mark pair as verified=0
- `(s)kip`: Don't change status
- `(q)uit`: Exit early

**Display Format:**
```
Pair 1/42
Name: FOMC Rate Decision - March 2026
Description: Federal Open Market Committee interest rate decision
Category: FOMC
Resolution Criteria: Based on official Federal Reserve FOMC announcement...
Leg A (POLYMARKET): 0x1234... (Will the Fed hike rates?)
Leg B (KALSHI): fomc-rate-mar-2026 (Federal Reserve rate hike probability)
```

### 4. export_analytics.py
**Purpose:** Export analytics tables to CSV for analysis

**Usage:**
```bash
python scripts/export_analytics.py \
    --output analytics_export \
    --db prediction_market.db
```

**Exports:**
1. `trade_outcomes.csv` - All completed trades with PnL
2. `pnl_snapshots.csv` - Historical PnL snapshots
3. `violations_summary.csv` - Constraint violations aggregated by type

**Violations Summary Query:**
```sql
SELECT
    violation_type,
    COUNT(*) as count,
    AVG(severity) as avg_severity,
    MIN(timestamp_utc) as first_violation,
    MAX(timestamp_utc) as last_violation
FROM constraint_violations
GROUP BY violation_type
ORDER BY count DESC
```

## Configuration

### Environment Variables (config/settings.example.env)

**Redis:**
```
REDIS_URL=redis://localhost:6379
SIGNAL_QUEUE_NAME=trading_signals
EVENT_BUS_CHANNEL=market_events
```

**Execution:**
```
MAX_ORDER_RETRIES=3
RETRY_BACKOFF_BASE_S=1
ORDER_TIMEOUT_S=30
SIGNAL_EXPIRY_S=300
ORDER_EXECUTION_MODE=simultaneous
ABORT_ON_PARTIAL=false
```

**Polymarket:**
```
POLYMARKET_RPC_URL=https://polygon-rpc.com
POLYMARKET_CHAIN_ID=137
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_WALLET_ADDRESS=0x...
```

**Kalshi:**
```
KALSHI_API_KEY=your_api_key
KALSHI_API_SECRET=your_api_secret
KALSHI_API_BASE=https://trading-api.kalshi.com/trade-api/v2
```

## Database Schema

### Required Tables

```sql
-- Orders
CREATE TABLE orders (
    order_id TEXT PRIMARY KEY,
    platform TEXT,
    market_id TEXT,
    side TEXT,
    size REAL,
    limit_price REAL,
    status TEXT,
    submission_latency_ms INTEGER,
    created_at_utc TIMESTAMP
);

-- Order Events
CREATE TABLE order_events (
    event_id INTEGER PRIMARY KEY,
    signal_id TEXT,
    order_id TEXT,
    leg_index INTEGER,
    status TEXT,
    details TEXT,
    timestamp_utc TIMESTAMP
);

-- Signal Events
CREATE TABLE signal_events (
    event_id INTEGER PRIMARY KEY,
    signal_id TEXT,
    status TEXT,
    details TEXT,
    timestamp_utc TIMESTAMP
);

-- Positions
CREATE TABLE positions (
    position_id TEXT PRIMARY KEY,
    market_id TEXT,
    platform TEXT,
    side TEXT,
    quantity REAL,
    entry_price REAL,
    entry_timestamp REAL,
    current_price REAL,
    unrealized_pnl REAL,
    updated_at_utc TIMESTAMP
);

-- Markets
CREATE TABLE markets (
    market_id TEXT PRIMARY KEY,
    platform TEXT,
    name TEXT,
    description TEXT,
    created_at_utc TIMESTAMP
);

-- Market Pairs
CREATE TABLE market_pairs (
    pair_id INTEGER PRIMARY KEY,
    name TEXT,
    description TEXT,
    category TEXT,
    resolution_criteria TEXT,
    leg_a_platform TEXT,
    leg_a_market_id TEXT,
    leg_a_description TEXT,
    leg_b_platform TEXT,
    leg_b_market_id TEXT,
    leg_b_description TEXT,
    match_method TEXT,
    verified INTEGER,
    created_at TIMESTAMP
);

-- Market Prices
CREATE TABLE market_prices (
    price_id INTEGER PRIMARY KEY,
    market_id TEXT,
    platform TEXT,
    timestamp_utc TIMESTAMP,
    mid_price REAL,
    bid REAL,
    ask REAL
);
```

## Running the Services

### Start Redis
```bash
docker-compose up -d redis
```

### Run Execution Service
```bash
python -m execution.main
```

Configuration via environment:
```bash
REDIS_URL=redis://localhost:6379 \
SIGNAL_QUEUE_NAME=trading_signals \
DB_PATH=prediction_market.db \
python -m execution.main
```

### Run Core Service
```bash
python -m core.main
```

## Error Handling Strategy

### Idempotency
- Track processed signal_ids in memory and database
- Reject duplicate signals with warning log
- Prevents double-execution of same signal

### Retry Logic
- Exponential backoff on order submission failures
- 3 attempts maximum by default
- Logs each attempt with latency metrics

### Partial Fills
- If `abort_on_partial=true` and any leg fails:
  - Cancel all previously accepted orders
  - Log abort event to database
  - Signal owner notified via event bus

### Validation Failures
- Schema validation errors logged with full error message
- Parameter validation failures checked independently
- Invalid legs skipped, but signal continues if valid legs exist

## Production Considerations

### Performance
- Async I/O throughout: no blocking operations
- asyncio.gather for parallel order execution
- Configurable flush intervals for state persistence
- BRPOP timeout prevents busy-waiting on empty queue

### Monitoring
- Structured logging at all critical points
- Latency metrics recorded for every operation
- Event logging to database for audit trail
- Health checks via Redis ping

### Scalability
- Stateless execution service (can run multiple instances)
- Shared database for coordination
- Redis queue for distributing signals
- Position state can be partitioned by market

### Security
- Parameters validated independently (defense-in-depth)
- Gas price spike handling for Polygon
- HMAC-SHA256 signing for Kalshi API
- No hardcoded credentials in code

## Testing

Install dev dependencies:
```bash
pip install -r requirements-dev.txt
```

Run tests:
```bash
pytest tests/ -v --cov=execution --cov=scripts
```

Type checking:
```bash
mypy execution/ scripts/
```

Linting:
```bash
ruff check execution/ scripts/
black --check execution/ scripts/
```

## Summary

The execution service and scripts provide a complete, production-quality system for:
- Consuming trading signals from a Redis queue
- Validating signals against strict schemas
- Routing orders to Polymarket (web3.py) and Kalshi (REST API)
- Managing position state with database persistence
- Providing operational scripts for analytics and configuration

All code features full type annotations, comprehensive error handling, async/await patterns, and is ready for production deployment.
