# Deployment Guide: Constraint Engine & Market Matching

## Architecture Overview

```
┌─────────────────────────────────────────────┐
│         Market Data Sources                  │
│  (Polymarket, Kalshi, Manifold, Metaculus)  │
└────────────────────┬────────────────────────┘
                     │
                     ▼
         ┌───────────────────────┐
         │   Event Bus           │
         │  (MarketUpdated)      │
         └───────────┬───────────┘
                     │
        ┌────────────┴────────────┐
        │                         │
        ▼                         ▼
┌──────────────────┐      ┌──────────────────┐
│ Constraint       │      │ Matching Layer   │
│ Engine           │      │ (Pair Discovery) │
└─────────┬────────┘      └──────┬───────────┘
          │                      │
          ▼                      ▼
┌──────────────────────────────────────────┐
│         Database                         │
│  - market_pairs                          │
│  - pair_spread_history                   │
│  - violations                            │
└──────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────┐
│      ConstraintViolationDetected         │
│      Event → Trading Bot / Alerting      │
└──────────────────────────────────────────┘
```

## Prerequisites

- Python 3.8+
- PostgreSQL 12+ (or compatible database)
- Redis (for event bus)
- Optional: sentence-transformers for semantic matching

## Installation

### 1. Clone and Setup

```bash
cd /path/to/prediction-market
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

**requirements.txt:**
```
asyncpg>=0.28.0          # PostgreSQL async driver
aioredis>=2.0.0          # Redis async client
numpy>=1.21.0
sentence-transformers>=2.2.0  # Optional, for semantic matching
pytest>=7.0.0            # Testing
pytest-asyncio>=0.21.0   # Async test support
```

### 3. Database Setup

#### PostgreSQL Schema

```sql
-- Market pairs table
CREATE TABLE market_pairs (
    pair_id VARCHAR(255) PRIMARY KEY,
    market_id_a VARCHAR(255) NOT NULL,
    market_id_b VARCHAR(255) NOT NULL,
    pair_type VARCHAR(50) NOT NULL,
    relationship VARCHAR(50),
    match_method VARCHAR(50) NOT NULL,
    similarity_score FLOAT,
    is_active BOOLEAN DEFAULT TRUE,
    verified_by VARCHAR(255),
    verified_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    created_by VARCHAR(255),
    UNIQUE(market_id_a, market_id_b)
);

-- Spread history table
CREATE TABLE pair_spread_history (
    id BIGSERIAL PRIMARY KEY,
    pair_id VARCHAR(255) NOT NULL,
    market_a_price FLOAT NOT NULL,
    market_b_price FLOAT NOT NULL,
    raw_spread FLOAT NOT NULL,
    net_spread FLOAT,
    rule_violations JSONB,
    recorded_at TIMESTAMP NOT NULL,
    FOREIGN KEY (pair_id) REFERENCES market_pairs(pair_id),
    INDEX idx_pair_id (pair_id),
    INDEX idx_recorded_at (recorded_at)
);

-- Violations table
CREATE TABLE violations (
    violation_id VARCHAR(255) PRIMARY KEY,
    pair_id VARCHAR(255) NOT NULL,
    market_id_a VARCHAR(255) NOT NULL,
    market_id_b VARCHAR(255) NOT NULL,
    rule_type VARCHAR(100) NOT NULL,
    severity VARCHAR(50) NOT NULL,
    description TEXT,
    implied_arbitrage FLOAT,
    detected_at TIMESTAMP NOT NULL,
    is_new BOOLEAN DEFAULT TRUE,
    resolved_at TIMESTAMP,
    FOREIGN KEY (pair_id) REFERENCES market_pairs(pair_id),
    INDEX idx_detected_at (detected_at),
    INDEX idx_is_new (is_new)
);

-- Create indexes for performance
CREATE INDEX idx_pairs_active ON market_pairs(is_active);
CREATE INDEX idx_pairs_type ON market_pairs(pair_type);
CREATE INDEX idx_spread_pair_time ON pair_spread_history(pair_id, recorded_at DESC);
```

### 4. Environment Variables

Create `.env` file:

```bash
# Database
DATABASE_URL=postgresql://user:password@localhost:5432/prediction_market
DB_MIN_CONNECTIONS=5
DB_MAX_CONNECTIONS=20

# Redis
REDIS_URL=redis://localhost:6379/0

# Constraint Engine
MIN_NET_SPREAD_SINGLE_PLATFORM=0.02
MIN_NET_SPREAD_CROSS_PLATFORM=0.03
COMPLEMENTARITY_TOLERANCE=0.01

# Platform Fees
FEE_RATE_POLYMARKET=0.02
FEE_RATE_KALSHI=0.02
FEE_RATE_MANIFOLD=0.01
FEE_RATE_METACULUS=0.00

# Logging
LOG_LEVEL=INFO
LOG_FORMAT=json

# Embedder
EMBEDDER_MODEL=all-MiniLM-L6-v2
ENABLE_SEMANTIC_MATCHING=true

# Monitoring
METRICS_PORT=8000
SENTRY_DSN=https://...@sentry.io/...
```

## Running the System

### 1. Implement Database Adapter

Create `infrastructure/database.py`:

```python
import asyncpg
from typing import List, Optional
from core.constraints.engine import MarketPair, MarketData
from datetime import datetime

class Database:
    def __init__(self, connection_string: str):
        self.connection_string = connection_string
        self.pool = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(self.connection_string)

    async def disconnect(self):
        if self.pool:
            await self.pool.close()

    async def get_market(self, market_id: str) -> Optional[MarketData]:
        # Fetch from market_prices table or API
        pass

    async def get_pairs_for_market(self, market_id: str) -> List[MarketPair]:
        query = """
            SELECT * FROM market_pairs
            WHERE (market_id_a = $1 OR market_id_b = $1) AND is_active
        """
        rows = await self.pool.fetch(query, market_id)
        return [MarketPair(**dict(row)) for row in rows]

    async def insert_market_pair(self, **kwargs) -> str:
        # Implementation
        pass

    async def insert_violation(self, **kwargs):
        # Implementation
        pass

    async def insert_pair_spread_history(self, entry):
        # Implementation
        pass
```

### 2. Implement Event Bus

Create `infrastructure/event_bus.py`:

```python
import aioredis
from typing import Callable, Dict, List

class RedisEventBus:
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.redis = None
        self.subscribers: Dict[str, List[Callable]] = {}

    async def connect(self):
        self.redis = await aioredis.create_redis_pool(self.redis_url)

    async def disconnect(self):
        if self.redis:
            self.redis.close()
            await self.redis.wait_closed()

    async def publish(self, event_type: str, event_data):
        # Publish to Redis channel
        await self.redis.publish(event_type, event_data.json())

    def subscribe(self, event_type: str, handler: Callable):
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []
        self.subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Callable):
        if event_type in self.subscribers:
            self.subscribers[event_type].remove(handler)
```

### 3. Create Main Service

Create `services/constraint_service.py`:

```python
import asyncio
import logging
from datetime import datetime
from infrastructure.database import Database
from infrastructure.event_bus import RedisEventBus
from core.constraints import ConstraintEngine
from core.constraints.engine import ConstraintConfig
from core.constraints.fees import FeeConfig
from core.matching import MarketPairCurator, MarketEmbedder

logger = logging.getLogger(__name__)

class ConstraintService:
    def __init__(self, config_dict: dict):
        self.config_dict = config_dict
        self.db = None
        self.event_bus = None
        self.engine = None

    async def start(self):
        logger.info("Starting Constraint Service...")

        # Connect to database
        self.db = Database(self.config_dict["DATABASE_URL"])
        await self.db.connect()

        # Connect to event bus
        self.event_bus = RedisEventBus(self.config_dict["REDIS_URL"])
        await self.event_bus.connect()

        # Initialize embedder
        embedder = MarketEmbedder(
            model_name=self.config_dict.get(
                "EMBEDDER_MODEL",
                "all-MiniLM-L6-v2"
            )
        )

        # Create constraint engine
        fee_config = FeeConfig(
            polymarket=float(self.config_dict["FEE_RATE_POLYMARKET"]),
            kalshi=float(self.config_dict["FEE_RATE_KALSHI"]),
            manifold=float(self.config_dict["FEE_RATE_MANIFOLD"]),
            metaculus=float(self.config_dict["FEE_RATE_METACULUS"]),
        )

        engine_config = ConstraintConfig(
            min_net_spread_single_platform=float(
                self.config_dict["MIN_NET_SPREAD_SINGLE_PLATFORM"]
            ),
            min_net_spread_cross_platform=float(
                self.config_dict["MIN_NET_SPREAD_CROSS_PLATFORM"]
            ),
            complementarity_tolerance=float(
                self.config_dict["COMPLEMENTARITY_TOLERANCE"]
            ),
            fee_config=fee_config,
        )

        self.engine = ConstraintEngine(self.event_bus, self.db, engine_config)
        await self.engine.start()

        logger.info("Constraint Service started successfully")

    async def stop(self):
        logger.info("Stopping Constraint Service...")
        if self.engine:
            await self.engine.stop()
        if self.event_bus:
            await self.event_bus.disconnect()
        if self.db:
            await self.db.disconnect()
        logger.info("Constraint Service stopped")

async def main():
    import os
    from dotenv import load_dotenv

    load_dotenv()

    config = {
        "DATABASE_URL": os.getenv("DATABASE_URL"),
        "REDIS_URL": os.getenv("REDIS_URL"),
        "MIN_NET_SPREAD_SINGLE_PLATFORM": os.getenv("MIN_NET_SPREAD_SINGLE_PLATFORM"),
        "MIN_NET_SPREAD_CROSS_PLATFORM": os.getenv("MIN_NET_SPREAD_CROSS_PLATFORM"),
        "COMPLEMENTARITY_TOLERANCE": os.getenv("COMPLEMENTARITY_TOLERANCE"),
        "FEE_RATE_POLYMARKET": os.getenv("FEE_RATE_POLYMARKET"),
        "FEE_RATE_KALSHI": os.getenv("FEE_RATE_KALSHI"),
        "FEE_RATE_MANIFOLD": os.getenv("FEE_RATE_MANIFOLD"),
        "FEE_RATE_METACULUS": os.getenv("FEE_RATE_METACULUS"),
        "EMBEDDER_MODEL": os.getenv("EMBEDDER_MODEL"),
    }

    service = ConstraintService(config)
    await service.start()

    # Keep running
    try:
        await asyncio.sleep(3600)  # 1 hour
    except KeyboardInterrupt:
        await service.stop()

if __name__ == "__main__":
    asyncio.run(main())
```

### 4. Docker Deployment

Create `Dockerfile`:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Run service
CMD ["python", "services/constraint_service.py"]
```

Create `docker-compose.yml`:

```yaml
version: '3.8'

services:
  postgres:
    image: postgres:15
    environment:
      POSTGRES_USER: user
      POSTGRES_PASSWORD: password
      POSTGRES_DB: prediction_market
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:7
    ports:
      - "6379:6379"

  constraint-engine:
    build: .
    environment:
      DATABASE_URL: postgresql://user:password@postgres:5432/prediction_market
      REDIS_URL: redis://redis:6379/0
      LOG_LEVEL: INFO
    depends_on:
      - postgres
      - redis
    ports:
      - "8000:8000"

volumes:
  postgres_data:
```

Run with:
```bash
docker-compose up
```

## Monitoring & Observability

### Logging

```python
import logging.config

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        },
        "detailed": {
            "format": "%(asctime)s [%(levelname)s] %(name)s:%(funcName)s:%(lineno)d - %(message)s"
        },
    },
    "handlers": {
        "default": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "standard",
        },
        "file": {
            "level": "DEBUG",
            "class": "logging.handlers.RotatingFileHandler",
            "filename": "constraint_engine.log",
            "maxBytes": 10485760,  # 10MB
            "backupCount": 5,
            "formatter": "detailed",
        },
    },
    "loggers": {
        "": {
            "handlers": ["default", "file"],
            "level": "DEBUG",
            "propagate": True,
        },
    },
}

logging.config.dictConfig(LOGGING_CONFIG)
```

### Metrics

```python
from prometheus_client import Counter, Histogram, start_http_server

violation_counter = Counter(
    'constraint_violations_total',
    'Total constraint violations detected',
    ['rule_type', 'severity']
)

spread_histogram = Histogram(
    'pair_spread_distribution',
    'Distribution of pair spreads',
    ['pair_type']
)

# Use in code:
violation_counter.labels(
    rule_type=violation.rule_type,
    severity=violation.severity
).inc()

spread_histogram.labels(pair_type=pair.pair_type).observe(spread)
```

### Alerting

Set up Prometheus + AlertManager or use Sentry:

```python
import sentry_sdk

sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    traces_sample_rate=0.1
)
```

## Performance Tuning

### Database Optimization

```sql
-- Add partitioning for large tables
ALTER TABLE pair_spread_history
PARTITION BY RANGE (YEAR(recorded_at)) (
    PARTITION p2024 VALUES LESS THAN (2025),
    PARTITION p2025 VALUES LESS THAN (2026),
    PARTITION p_future VALUES LESS THAN MAXVALUE
);
```

### Connection Pooling

```python
# Set appropriate pool sizes based on load
db = Database(
    connection_string,
    min_connections=10,
    max_connections=50
)
```

### Caching

```python
from functools import lru_cache

@lru_cache(maxsize=1000)
async def get_market_pair_config(pair_id: str):
    # Cache frequently accessed pair configs
    pass
```

## Monitoring Checklist

- [ ] Set up logging to centralized system
- [ ] Configure Prometheus metrics scraping
- [ ] Set up AlertManager for critical violations
- [ ] Monitor database connection pool usage
- [ ] Track event bus lag/throughput
- [ ] Monitor embedder model inference time
- [ ] Set up health checks/liveness probes
- [ ] Configure log rotation and retention

## Production Runbook

### Startup Sequence

1. Verify database connectivity
2. Verify Redis connectivity
3. Load embedder model (warm-up)
4. Load active market pairs from database
5. Start event bus subscription
6. Begin constraint monitoring

### Common Issues

**High Memory Usage:**
- Reduce embedder batch size
- Implement cache eviction policies

**Slow Constraint Checks:**
- Profile with cProfile
- Optimize database queries
- Add appropriate indexes

**Missing Market Updates:**
- Verify event bus connectivity
- Check Redis memory/cpu
- Increase subscription timeout

## Scaling Considerations

- Horizontal: Multiple constraint engine instances behind load balancer
- Vertical: Increase CPU/RAM for embedder inference
- Database: Implement read replicas for queries
- Event Bus: Use Redis Cluster for high throughput

## Version Control & Deployment

```bash
# Tag release
git tag -a v1.0.0 -m "Initial release"

# Build and push Docker image
docker build -t constraint-engine:1.0.0 .
docker push registry.example.com/constraint-engine:1.0.0

# Deploy
kubectl apply -f kubernetes/deployment.yaml
```

## Maintenance

### Regular Tasks

- [ ] Review constraint violation patterns weekly
- [ ] Update market pair matching templates
- [ ] Audit fee rates monthly
- [ ] Backup database daily
- [ ] Update dependencies quarterly

### Health Checks

```python
async def health_check():
    checks = {
        "database": await db.health_check(),
        "event_bus": await event_bus.health_check(),
        "embedder": embedder.is_available(),
    }
    return all(checks.values()), checks
```
