-- Initial schema for prediction market trading system
-- All 16 tables with proper indexes and constraints

-- Markets table: core market information
CREATE TABLE IF NOT EXISTS markets (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    category TEXT,
    event_type TEXT,
    resolution_source TEXT,
    resolution_criteria TEXT,
    close_time TEXT,
    resolve_time TEXT,
    outcome TEXT,
    outcome_value REAL,
    status TEXT DEFAULT 'open',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(platform, platform_id)
);

CREATE INDEX IF NOT EXISTS idx_markets_platform ON markets(platform);
CREATE INDEX IF NOT EXISTS idx_markets_status ON markets(status);
CREATE INDEX IF NOT EXISTS idx_markets_created_at ON markets(created_at);
CREATE INDEX IF NOT EXISTS idx_markets_category ON markets(category);

-- Market prices: time-series pricing data
CREATE TABLE IF NOT EXISTS market_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    yes_price REAL,
    no_price REAL,
    spread REAL,
    volume_24h REAL,
    open_interest REAL,
    liquidity REAL,
    poll_latency_ms INTEGER,
    polled_at TEXT NOT NULL,
    FOREIGN KEY (market_id) REFERENCES markets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_market_prices_market_id ON market_prices(market_id);
CREATE INDEX IF NOT EXISTS idx_market_prices_polled_at ON market_prices(polled_at);
CREATE INDEX IF NOT EXISTS idx_market_prices_timestamp ON market_prices(market_id, polled_at);

-- Ingestor runs: tracking data collection
CREATE TABLE IF NOT EXISTS ingestor_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    markets_fetched INTEGER NOT NULL,
    markets_new INTEGER NOT NULL,
    markets_updated INTEGER NOT NULL,
    errors INTEGER DEFAULT 0,
    error_detail TEXT,
    duration_ms INTEGER,
    ran_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingestor_runs_platform ON ingestor_runs(platform);
CREATE INDEX IF NOT EXISTS idx_ingestor_runs_ran_at ON ingestor_runs(ran_at);

-- Market pairs: same-market pairings across platforms
CREATE TABLE IF NOT EXISTS market_pairs (
    id TEXT PRIMARY KEY,
    market_id_a TEXT NOT NULL,
    market_id_b TEXT NOT NULL,
    pair_type TEXT NOT NULL,
    relationship TEXT,
    similarity_score REAL,
    match_method TEXT,
    verified INTEGER DEFAULT 0,
    verified_by TEXT,
    verified_at TEXT,
    active INTEGER DEFAULT 1,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (market_id_a) REFERENCES markets(id) ON DELETE CASCADE,
    FOREIGN KEY (market_id_b) REFERENCES markets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_market_pairs_market_a ON market_pairs(market_id_a);
CREATE INDEX IF NOT EXISTS idx_market_pairs_market_b ON market_pairs(market_id_b);
CREATE INDEX IF NOT EXISTS idx_market_pairs_verified ON market_pairs(verified);
CREATE INDEX IF NOT EXISTS idx_market_pairs_active ON market_pairs(active);
CREATE INDEX IF NOT EXISTS idx_market_pairs_similarity ON market_pairs(similarity_score);

-- Pair spread history: tracking spread evolution
CREATE TABLE IF NOT EXISTS pair_spread_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id TEXT NOT NULL,
    price_a REAL NOT NULL,
    price_b REAL NOT NULL,
    raw_spread REAL NOT NULL,
    net_spread REAL NOT NULL,
    constraint_satisfied INTEGER,
    evaluated_at TEXT NOT NULL,
    FOREIGN KEY (pair_id) REFERENCES market_pairs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_pair_spread_history_pair_id ON pair_spread_history(pair_id);
CREATE INDEX IF NOT EXISTS idx_pair_spread_history_evaluated_at ON pair_spread_history(evaluated_at);
CREATE INDEX IF NOT EXISTS idx_pair_spread_history_net_spread ON pair_spread_history(net_spread);

-- Violations: arbitrage opportunities detected
CREATE TABLE IF NOT EXISTS violations (
    id TEXT PRIMARY KEY,
    pair_id TEXT NOT NULL,
    violation_type TEXT NOT NULL,
    price_a_at_detect REAL NOT NULL,
    price_b_at_detect REAL NOT NULL,
    raw_spread REAL NOT NULL,
    net_spread REAL NOT NULL,
    fee_estimate_a REAL,
    fee_estimate_b REAL,
    status TEXT DEFAULT 'detected',
    rejection_reason TEXT,
    detected_at TEXT NOT NULL,
    closed_at TEXT,
    duration_open_ms INTEGER,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (pair_id) REFERENCES market_pairs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_violations_pair_id ON violations(pair_id);
CREATE INDEX IF NOT EXISTS idx_violations_status ON violations(status);
CREATE INDEX IF NOT EXISTS idx_violations_detected_at ON violations(detected_at);
CREATE INDEX IF NOT EXISTS idx_violations_type ON violations(violation_type);

-- Signals: trading signals generated
CREATE TABLE IF NOT EXISTS signals (
    id TEXT PRIMARY KEY,
    violation_id TEXT,
    strategy TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    market_id_a TEXT NOT NULL,
    market_id_b TEXT,
    target_price_a REAL,
    target_price_b REAL,
    model_fair_value REAL,
    model_edge REAL NOT NULL,
    kelly_fraction REAL NOT NULL,
    position_size_a REAL NOT NULL,
    position_size_b REAL,
    total_capital_at_risk REAL NOT NULL,
    risk_check_passed INTEGER DEFAULT 1,
    daily_loss_limit_remaining REAL,
    portfolio_exposure_pct REAL,
    status TEXT DEFAULT 'queued',
    fired_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (violation_id) REFERENCES violations(id) ON DELETE SET NULL,
    FOREIGN KEY (market_id_a) REFERENCES markets(id) ON DELETE CASCADE,
    FOREIGN KEY (market_id_b) REFERENCES markets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status);
CREATE INDEX IF NOT EXISTS idx_signals_strategy ON signals(strategy);
CREATE INDEX IF NOT EXISTS idx_signals_fired_at ON signals(fired_at);
CREATE INDEX IF NOT EXISTS idx_signals_market_a ON signals(market_id_a);
CREATE INDEX IF NOT EXISTS idx_signals_violation ON signals(violation_id);

-- Risk check log: detailed risk validation records
CREATE TABLE IF NOT EXISTS risk_check_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    violation_id TEXT,
    check_type TEXT NOT NULL,
    passed INTEGER NOT NULL,
    check_value REAL,
    threshold REAL,
    detail TEXT,
    evaluated_at TEXT NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES signals(id) ON DELETE CASCADE,
    FOREIGN KEY (violation_id) REFERENCES violations(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_risk_check_signal_id ON risk_check_log(signal_id);
CREATE INDEX IF NOT EXISTS idx_risk_check_type ON risk_check_log(check_type);
CREATE INDEX IF NOT EXISTS idx_risk_check_evaluated_at ON risk_check_log(evaluated_at);

-- Orders: executed orders
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    platform_order_id TEXT,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    requested_price REAL,
    requested_size REAL NOT NULL,
    filled_price REAL,
    filled_size REAL,
    slippage REAL,
    fee_paid REAL,
    status TEXT DEFAULT 'pending',
    failure_reason TEXT,
    retry_count INTEGER DEFAULT 0,
    submitted_at TEXT NOT NULL,
    filled_at TEXT,
    cancelled_at TEXT,
    submission_latency_ms INTEGER,
    fill_latency_ms INTEGER,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES signals(id) ON DELETE CASCADE,
    FOREIGN KEY (market_id) REFERENCES markets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_orders_signal_id ON orders(signal_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_platform ON orders(platform);
CREATE INDEX IF NOT EXISTS idx_orders_submitted_at ON orders(submitted_at);
CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market_id);

-- Order events: detailed order lifecycle
CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    price REAL,
    size REAL,
    detail TEXT,
    occurred_at TEXT NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_order_events_order_id ON order_events(order_id);
CREATE INDEX IF NOT EXISTS idx_order_events_type ON order_events(event_type);
CREATE INDEX IF NOT EXISTS idx_order_events_occurred_at ON order_events(occurred_at);

-- Positions: open and closed positions
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    entry_price REAL NOT NULL,
    entry_size REAL NOT NULL,
    current_price REAL,
    unrealized_pnl REAL,
    exit_price REAL,
    exit_size REAL,
    realized_pnl REAL,
    fees_paid REAL DEFAULT 0,
    status TEXT DEFAULT 'open',
    resolution_outcome TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES signals(id) ON DELETE CASCADE,
    FOREIGN KEY (market_id) REFERENCES markets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_market ON positions(market_id);
CREATE INDEX IF NOT EXISTS idx_positions_signal ON positions(signal_id);
CREATE INDEX IF NOT EXISTS idx_positions_opened_at ON positions(opened_at);

-- PnL snapshots: portfolio snapshots
CREATE TABLE IF NOT EXISTS pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_type TEXT DEFAULT 'scheduled',
    total_capital REAL NOT NULL,
    cash REAL NOT NULL,
    open_positions_count INTEGER NOT NULL,
    open_notional REAL,
    unrealized_pnl REAL,
    realized_pnl_today REAL,
    realized_pnl_total REAL,
    fees_today REAL,
    fees_total REAL,
    pnl_constraint_arb REAL DEFAULT 0,
    pnl_event_model REAL DEFAULT 0,
    pnl_calibration REAL DEFAULT 0,
    pnl_liquidity REAL DEFAULT 0,
    pnl_latency REAL DEFAULT 0,
    capital_polymarket REAL,
    capital_kalshi REAL,
    snapshotted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pnl_snapshots_type ON pnl_snapshots(snapshot_type);
CREATE INDEX IF NOT EXISTS idx_pnl_snapshots_snapshotted_at ON pnl_snapshots(snapshotted_at);

-- Trade outcomes: post-trade analysis
CREATE TABLE IF NOT EXISTS trade_outcomes (
    id TEXT PRIMARY KEY,
    signal_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    violation_id TEXT,
    market_id_a TEXT NOT NULL,
    market_id_b TEXT,
    predicted_edge REAL,
    predicted_pnl REAL,
    actual_pnl REAL,
    fees_total REAL,
    edge_captured_pct REAL,
    signal_to_fill_ms INTEGER,
    holding_period_ms INTEGER,
    spread_at_signal REAL,
    volume_at_signal REAL,
    liquidity_at_signal REAL,
    resolved_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES signals(id) ON DELETE CASCADE,
    FOREIGN KEY (violation_id) REFERENCES violations(id) ON DELETE SET NULL,
    FOREIGN KEY (market_id_a) REFERENCES markets(id) ON DELETE CASCADE,
    FOREIGN KEY (market_id_b) REFERENCES markets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_trade_outcomes_signal_id ON trade_outcomes(signal_id);
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_strategy ON trade_outcomes(strategy);
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_resolved_at ON trade_outcomes(resolved_at);

-- Model predictions: ML model outputs
CREATE TABLE IF NOT EXISTS model_predictions (
    id TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    market_id TEXT NOT NULL,
    input_features TEXT,
    predicted_probability REAL NOT NULL,
    market_price_at_prediction REAL,
    edge REAL,
    predicted_at TEXT NOT NULL,
    actual_outcome TEXT,
    brier_score REAL,
    resolved_at TEXT,
    FOREIGN KEY (market_id) REFERENCES markets(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_model_predictions_market ON model_predictions(market_id);
CREATE INDEX IF NOT EXISTS idx_model_predictions_model ON model_predictions(model_name, model_version);
CREATE INDEX IF NOT EXISTS idx_model_predictions_predicted_at ON model_predictions(predicted_at);

-- Model versions: model training metadata
CREATE TABLE IF NOT EXISTS model_versions (
    id TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    version TEXT NOT NULL,
    trained_at TEXT NOT NULL,
    training_samples INTEGER,
    feature_names TEXT,
    hyperparameters TEXT,
    in_sample_brier REAL,
    out_of_sample_brier REAL,
    deployed_at TEXT,
    retired_at TEXT,
    notes TEXT,
    UNIQUE(model_name, version)
);

CREATE INDEX IF NOT EXISTS idx_model_versions_name ON model_versions(model_name);
CREATE INDEX IF NOT EXISTS idx_model_versions_deployed ON model_versions(deployed_at);

-- System events: logging important events
CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    severity TEXT DEFAULT 'info',
    component TEXT NOT NULL,
    detail TEXT NOT NULL,
    context TEXT,
    occurred_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_system_events_type ON system_events(event_type);
CREATE INDEX IF NOT EXISTS idx_system_events_severity ON system_events(severity);
CREATE INDEX IF NOT EXISTS idx_system_events_component ON system_events(component);
CREATE INDEX IF NOT EXISTS idx_system_events_occurred_at ON system_events(occurred_at);

-- Signal events: tracks signal lifecycle (validation, execution, rejection)
CREATE TABLE IF NOT EXISTS signal_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT NOT NULL,
    status TEXT NOT NULL,
    details TEXT,
    timestamp_utc TEXT NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES signals(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_signal_events_signal_id ON signal_events(signal_id);
CREATE INDEX IF NOT EXISTS idx_signal_events_status ON signal_events(status);
