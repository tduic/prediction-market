# Roadmap

Features and improvements not yet implemented, roughly ordered by priority.

## Near-Term

### Live Platform API Testing
The Polymarket (py-clob-client) and Kalshi (RSA-PSS) execution clients are built with correct authentication, but have not been tested against production APIs with real orders. Next steps: run read-only API calls (fetch markets, check balances) to verify auth, then place small test orders on Kalshi demo environment before production.

### Real-Time Price Feeds
Currently using polling-based ingestion (~30s intervals). Adding websocket connections to Polymarket's CLOB feed and Kalshi's streaming API would reduce latency to sub-second, which matters most for P5 (Information Latency) strategy.

### Model Training Pipeline
The prediction model framework (base class, registry, FOMC/CPI/calibration models) is built but the actual training pipeline is stubbed. Needs: feature engineering from historical market data, train/validate/test splits, automated refit scheduling, and Brier score tracking against the `model_predictions` table.

### Redis Signal Queue Hardening
The two-service architecture works and auto-reconnects on failure, but still needs: message deduplication, dead letter queue for signals that fail processing, and backpressure handling when execution is slow.

## Medium-Term

### Monitoring & Alerting
Structured JSON logging is in place, but there's no alerting layer. Planned: Prometheus metrics export, Grafana dashboards, Slack/PagerDuty integration for circuit breaker trips and execution failures.

### Docker Deployment
Dockerfile and docker-compose for running both services plus Redis in production. Health checks, graceful shutdown, volume mounts for the SQLite database, and log aggregation.

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
Replace the CLI dashboard with a web-based UI showing real-time portfolio state, strategy performance charts, and execution monitoring.
