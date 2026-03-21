# Roadmap

Features and improvements not yet implemented, roughly ordered by priority.

## Near-Term

### Live Platform Integration
The Polymarket and Kalshi execution clients have the interface and DB integration built, but have not been tested against production APIs. Remaining work: end-to-end authentication flow testing, rate limit handling, websocket price feeds (currently polling), and order book depth integration.

### Redis Signal Queue
The two-service architecture (Core → Redis → Execution) is designed but the Redis pub/sub integration needs hardening: reconnection logic, message deduplication, dead letter queue for failed signals, and backpressure handling when execution is slow.

### Real-Time Price Feeds
Currently using polling-based ingestion. Adding websocket connections to Polymarket's CLOB feed and Kalshi's streaming API would reduce latency from ~30s to sub-second, which matters most for P5 (Information Latency) strategy.

### Model Training Pipeline
The prediction model framework (base class, registry, FOMC/CPI/calibration models) is built but the actual training pipeline is stubbed. Needs: feature engineering from historical market data, train/validate/test splits, automated refit scheduling, and Brier score tracking against `model_predictions` table.

## Medium-Term

### Monitoring & Alerting
System events are logged to the `system_events` table but there's no alerting layer. Planned: Prometheus metrics export, Grafana dashboards, PagerDuty/Slack integration for circuit breaker trips and execution failures.

### Docker Deployment
Dockerfile and docker-compose for running both services plus Redis. Health checks, graceful shutdown, volume mounts for the SQLite database, and log aggregation.

### Backtesting Framework
Use historical `market_prices` and `pair_spread_history` data to replay constraint violations through the signal pipeline. Compare simulated PnL against actual outcomes to validate strategy parameters before deploying changes.

### Position Reconciliation
Periodic reconciliation between in-memory position state, the database, and actual platform balances. Flag discrepancies for manual review.

### Multi-Outcome Markets
Current constraint rules assume binary (yes/no) markets. Extending to multi-outcome markets (e.g., "Who wins the election?" with 5+ candidates) requires generalized probability simplex constraints.

## Longer-Term

### Additional Platforms
Extend ingestors and execution clients to support Metaculus, PredictIt, or other prediction market platforms. The platform-agnostic design (market pairs, constraint engine) should make this straightforward.

### ML Feature Store
Centralized feature computation and caching for prediction models. Market-level features (price momentum, volume patterns, spread history) and event-level features (economic indicators, sentiment) stored for efficient model training.

### Portfolio Optimization
Replace per-signal Kelly sizing with portfolio-level optimization that considers correlation between open positions, joint probability of outcomes, and capital allocation across strategies.

### Web Dashboard
Replace the CLI dashboard with a web-based UI showing real-time portfolio state, strategy performance charts, and execution monitoring. Could be a simple Flask/FastAPI app reading from the same SQLite database.
