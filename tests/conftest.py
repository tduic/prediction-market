"""
Shared pytest fixtures for prediction market trading system tests.
"""

import json
import sqlite3
from pathlib import Path
from typing import Generator, Dict, Any, List
from dataclasses import dataclass
from datetime import datetime

import pytest
import pytest_asyncio


# ============================================================================
# Configuration and Data Models
# ============================================================================


@dataclass
class Config:
    """Test configuration object with sensible defaults."""

    PAPER_TRADING: bool = True
    MAX_POSITION_SIZE_USD: float = 10000.0
    MAX_DAILY_LOSS_USD: float = 5000.0
    MAX_PORTFOLIO_EXPOSURE_PCT: float = 0.75
    KELLY_FRACTION: float = 0.25
    DUPLICATE_SIGNAL_WINDOW_S: int = 300
    MIN_SPREAD_BPS: int = 50
    POLYMARKET_FEE_PCT: float = 0.2
    KALSHI_FEE_PCT: float = 0.2
    API_RATE_LIMIT: int = 100
    INGEST_INTERVAL_S: int = 60
    MIN_TRAINING_SAMPLES: int = 30


@dataclass
class Market:
    """Market data model."""

    id: str
    platform: str
    platform_id: str
    title: str
    description: str
    category: str
    event_type: str
    yes_price: float
    no_price: float
    status: str


@dataclass
class Violation:
    """Constraint violation data model."""

    violation_id: str
    pair_id: str
    violation_type: str
    price_a: float
    price_b: float
    raw_spread: float
    net_spread: float
    arbitrage_available: bool
    expected_result: str


class EventBus:
    """Simple in-memory event bus for testing."""

    def __init__(self):
        self.events: List[Dict[str, Any]] = []
        self.subscribers: Dict[str, List[callable]] = {}

    def subscribe(self, event_type: str, handler: callable) -> None:
        """Subscribe handler to event type."""
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []
        self.subscribers[event_type].append(handler)

    def emit(self, event_type: str, data: Dict[str, Any]) -> None:
        """Emit event and notify subscribers."""
        event = {
            "type": event_type,
            "data": data,
            "timestamp": datetime.utcnow().isoformat(),
        }
        self.events.append(event)

        if event_type in self.subscribers:
            for handler in self.subscribers[event_type]:
                handler(event)

    def get_events(self, event_type: str = None) -> List[Dict[str, Any]]:
        """Retrieve events, optionally filtered by type."""
        if event_type is None:
            return self.events
        return [e for e in self.events if e["type"] == event_type]

    def clear(self) -> None:
        """Clear all events."""
        self.events = []


# ============================================================================
# Database Fixtures
# ============================================================================


@pytest.fixture
def in_memory_db() -> Generator[sqlite3.Connection, None, None]:
    """
    Create an in-memory SQLite database with full schema.

    Yields:
        sqlite3.Connection: Connected database with schema initialized.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # Enable foreign keys
    conn.execute("PRAGMA foreign_keys = ON")

    # Create schema
    schema = """
    CREATE TABLE markets (
        id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        platform_id TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        category TEXT,
        event_type TEXT,
        yes_price REAL NOT NULL,
        no_price REAL NOT NULL,
        status TEXT DEFAULT 'open',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(platform, platform_id)
    );

    CREATE TABLE market_prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id TEXT NOT NULL,
        yes_price REAL NOT NULL,
        no_price REAL NOT NULL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (market_id) REFERENCES markets (id)
    );

    CREATE TABLE market_pairs (
        id TEXT PRIMARY KEY,
        market_a_id TEXT NOT NULL,
        market_b_id TEXT NOT NULL,
        pair_type TEXT,
        status TEXT DEFAULT 'active',
        verified BOOLEAN DEFAULT 0,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (market_a_id) REFERENCES markets (id),
        FOREIGN KEY (market_b_id) REFERENCES markets (id),
        UNIQUE(market_a_id, market_b_id)
    );

    CREATE TABLE violations (
        id TEXT PRIMARY KEY,
        pair_id TEXT NOT NULL,
        violation_type TEXT NOT NULL,
        price_a REAL NOT NULL,
        price_b REAL NOT NULL,
        raw_spread REAL NOT NULL,
        net_spread REAL NOT NULL,
        detected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        severity TEXT DEFAULT 'medium',
        FOREIGN KEY (pair_id) REFERENCES market_pairs (id)
    );

    CREATE TABLE signals (
        id TEXT PRIMARY KEY,
        violation_id TEXT NOT NULL,
        signal_type TEXT NOT NULL,
        size_usd REAL NOT NULL,
        kelly_fraction REAL,
        expected_edge REAL,
        ttl_s INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        expires_at DATETIME,
        status TEXT DEFAULT 'pending',
        FOREIGN KEY (violation_id) REFERENCES violations (id)
    );

    CREATE TABLE orders (
        id TEXT PRIMARY KEY,
        signal_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        platform_order_id TEXT,
        leg_type TEXT NOT NULL,
        side TEXT NOT NULL,
        quantity REAL NOT NULL,
        price REAL NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        submitted_at DATETIME,
        filled_at DATETIME,
        fill_price REAL,
        status TEXT DEFAULT 'pending',
        FOREIGN KEY (signal_id) REFERENCES signals (id)
    );

    CREATE TABLE order_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        details TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (order_id) REFERENCES orders (id)
    );

    CREATE TABLE ingestor_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at DATETIME NOT NULL,
        completed_at DATETIME,
        status TEXT DEFAULT 'running',
        markets_fetched INTEGER,
        markets_inserted INTEGER,
        markets_updated INTEGER,
        errors TEXT
    );

    CREATE TABLE portfolio_positions (
        id TEXT PRIMARY KEY,
        signal_id TEXT NOT NULL,
        market_a_id TEXT NOT NULL,
        market_b_id TEXT NOT NULL,
        position_type TEXT,
        entry_date DATETIME NOT NULL,
        exit_date DATETIME,
        pnl_usd REAL,
        status TEXT DEFAULT 'open',
        FOREIGN KEY (signal_id) REFERENCES signals (id),
        FOREIGN KEY (market_a_id) REFERENCES markets (id),
        FOREIGN KEY (market_b_id) REFERENCES markets (id)
    );

    CREATE TABLE daily_loss_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date DATE NOT NULL,
        cumulative_loss REAL NOT NULL,
        num_signals INTEGER,
        num_trades INTEGER,
        UNIQUE(date)
    );

    CREATE INDEX idx_market_prices_market_id ON market_prices(market_id, timestamp);
    CREATE INDEX idx_market_prices_timestamp ON market_prices(timestamp);
    CREATE INDEX idx_violations_pair_id ON violations(pair_id);
    CREATE INDEX idx_violations_created ON violations(detected_at);
    CREATE INDEX idx_signals_status ON signals(status);
    CREATE INDEX idx_signals_expires ON signals(expires_at);
    CREATE INDEX idx_orders_signal_id ON orders(signal_id);
    CREATE INDEX idx_orders_status ON orders(status);
    """

    conn.executescript(schema)
    conn.commit()

    yield conn

    conn.close()


# ============================================================================
# Configuration Fixtures
# ============================================================================


@pytest.fixture
def sample_config() -> Config:
    """
    Provide a test configuration with paper trading enabled.

    Returns:
        Config: Configuration object with test defaults.
    """
    return Config(
        PAPER_TRADING=True,
        MAX_POSITION_SIZE_USD=10000.0,
        MAX_DAILY_LOSS_USD=5000.0,
        MAX_PORTFOLIO_EXPOSURE_PCT=0.75,
        KELLY_FRACTION=0.25,
        DUPLICATE_SIGNAL_WINDOW_S=300,
        MIN_SPREAD_BPS=50,
    )


# ============================================================================
# Event Bus Fixtures
# ============================================================================


@pytest.fixture
def event_bus() -> EventBus:
    """
    Create a fresh EventBus instance for testing.

    Returns:
        EventBus: In-memory event bus.
    """
    return EventBus()


# ============================================================================
# Market Data Fixtures
# ============================================================================


@pytest.fixture
def sample_markets() -> Dict[str, List[Market]]:
    """
    Load sample market data from fixtures/markets.json.

    Returns:
        Dict[str, List[Market]]: Markets organized by platform.
    """
    fixture_path = Path(__file__).parent / "fixtures" / "markets.json"

    with open(fixture_path, "r") as f:
        data = json.load(f)

    result = {}
    for platform, markets_list in data.items():
        result[platform] = [
            Market(
                id=m["id"],
                platform=m["platform"],
                platform_id=m["platform_id"],
                title=m["title"],
                description=m["description"],
                category=m["category"],
                event_type=m["event_type"],
                yes_price=m["yes_price"],
                no_price=m["no_price"],
                status=m["status"],
            )
            for m in markets_list
        ]

    return result


@pytest.fixture
def sample_violations() -> List[Violation]:
    """
    Load sample violation data from fixtures/violations.json.

    Returns:
        List[Violation]: List of violation data objects.
    """
    fixture_path = Path(__file__).parent / "fixtures" / "violations.json"

    with open(fixture_path, "r") as f:
        data = json.load(f)

    violations = []
    for v in data["violations"]:
        violations.append(
            Violation(
                violation_id=v["violation_id"],
                pair_id=v["pair_id"],
                violation_type=v["violation_type"],
                price_a=v["price_a"],
                price_b=v["price_b"],
                raw_spread=v["raw_spread"],
                net_spread=v["net_spread"],
                arbitrage_available=v["arbitrage_available"],
                expected_result=v["expected_result"],
            )
        )

    return violations


# ============================================================================
# Async Fixtures
# ============================================================================


@pytest_asyncio.fixture
async def async_event_bus() -> EventBus:
    """
    Async fixture that provides an EventBus instance.

    Yields:
        EventBus: In-memory event bus for async tests.
    """
    bus = EventBus()
    yield bus


@pytest_asyncio.fixture
async def async_in_memory_db() -> Generator[sqlite3.Connection, None, None]:
    """
    Async fixture that provides an in-memory database connection.

    Yields:
        sqlite3.Connection: Connected database with schema initialized.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    schema = """
    CREATE TABLE markets (
        id TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        platform_id TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        category TEXT,
        event_type TEXT,
        yes_price REAL NOT NULL,
        no_price REAL NOT NULL,
        status TEXT DEFAULT 'open',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(platform, platform_id)
    );

    CREATE TABLE market_prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        market_id TEXT NOT NULL,
        yes_price REAL NOT NULL,
        no_price REAL NOT NULL,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (market_id) REFERENCES markets (id)
    );

    CREATE TABLE signals (
        id TEXT PRIMARY KEY,
        violation_id TEXT NOT NULL,
        signal_type TEXT NOT NULL,
        size_usd REAL NOT NULL,
        kelly_fraction REAL,
        expected_edge REAL,
        ttl_s INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        expires_at DATETIME,
        status TEXT DEFAULT 'pending'
    );

    CREATE TABLE orders (
        id TEXT PRIMARY KEY,
        signal_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        platform_order_id TEXT,
        leg_type TEXT NOT NULL,
        side TEXT NOT NULL,
        quantity REAL NOT NULL,
        price REAL NOT NULL,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        submitted_at DATETIME,
        filled_at DATETIME,
        fill_price REAL,
        status TEXT DEFAULT 'pending'
    );
    """

    conn.executescript(schema)
    conn.commit()

    yield conn
    conn.close()
