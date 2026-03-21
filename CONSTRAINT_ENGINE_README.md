# Constraint Engine & Market Matching System

Production-quality constraint engine and matching layer for prediction market arbitrage detection.

## Architecture Overview

### Constraint Engine (`core/constraints/`)

The constraint engine monitors prediction markets for pricing violations and arbitrage opportunities. It runs constraint checks on market pairs and emits violation events when exploitable spreads are detected.

**Components:**

1. **ConstraintEngine** (`engine.py`)
   - Main event-driven engine subscribing to MarketUpdated events
   - Loads related market pairs from database
   - Runs constraint checkers on each pair
   - Emits ViolationDetected events
   - Records spread history and violations to database

2. **Fee Estimator** (`fees.py`)
   - Platform-specific fee calculation (Polymarket, Kalshi, Manifold, Metaculus)
   - Net spread calculation after fees
   - Supports different fee rates per platform

3. **Constraint Rules** (`rules/`)
   - Pure functions for easy testing
   - No side effects - all data passed as arguments
   - Return ViolationInfo on constraint violation

### Matching Layer (`core/matching/`)

Discovers and manages market pairs using rule-based templates and semantic embeddings.

**Components:**

1. **Rule-Based Matcher** (`rules.py`)
   - Templates for known event types (FOMC, CPI, elections, sports)
   - Pattern matching with configurable confidence
   - Fast, deterministic matching

2. **Semantic Embedder** (`embedder.py`)
   - Sentence-transformer integration
   - Graceful fallback if library not installed
   - Batch embedding and similarity computation

3. **Pair Curator** (`curator.py`)
   - CRUD operations for market_pairs table
   - Verification workflow for human review
   - Statistics and filtering queries

## Constraint Rules

### 1. Subset/Superset Rule
**File:** `core/constraints/rules/subset_superset.py`

Enforces logical consistency: if A is a subset of B, then P(A) ≤ P(B).

Example: "Candidate wins Q1 2026" is subset of "Candidate wins 2026"

```python
from core.constraints.rules import subset_superset

violation = subset_superset.check(
    market_a_price=0.35,      # Q1 probability
    market_b_price=0.30,      # Annual probability
    relationship="subset"      # market_a is subset of market_b
)
# Returns violation if Q1 > Annual (illogical pricing)
```

### 2. Mutual Exclusivity Rule
**File:** `core/constraints/rules/mutual_exclusivity.py`

Enforces: P(A) + P(B) + P(C) ≤ 100% for exhaustive outcomes.

Example: "Trump wins", "Harris wins", "Someone else wins"

```python
from core.constraints.rules import mutual_exclusivity

violation = mutual_exclusivity.check(
    prices=[0.45, 0.50, 0.10]  # Three candidates' probabilities
)
# Returns violation if sum > 1.0
```

### 3. Complementarity Rule
**File:** `core/constraints/rules/complementarity.py`

Enforces: P(YES) + P(NO) = 100% on binary markets.

Example: "Will Bitcoin exceed $100k?" with separate YES and NO markets

```python
from core.constraints.rules import complementarity

violation = complementarity.check(
    yes_price=0.58,
    no_price=0.40,  # Should sum to 1.0
    tolerance=0.01  # Allow 1% for fees
)
# Returns violation if |sum - 1.0| > tolerance
```

### 4. Cross-Platform Rule
**File:** `core/constraints/rules/cross_platform.py`

Detects spreads too tight for profitable arbitrage across platforms.

Example: Same event on Polymarket (0.45) and Kalshi (0.46)

```python
from core.constraints.rules import cross_platform

violation = cross_platform.check(
    price_a=0.45,
    price_b=0.46,
    platform_a="polymarket",
    platform_b="kalshi",
    min_net_spread_threshold=0.03,  # 3% minimum
    fee_config=config  # Platform fee rates
)
# Returns violation if net spread < 3% after fees
```

## Usage Examples

### Basic Constraint Engine Usage

```python
from core.constraints import ConstraintEngine
from core.constraints.fees import FeeConfig

# Create engine
config = ConstraintConfig(
    min_net_spread_single_platform=0.02,
    min_net_spread_cross_platform=0.03,
    complementarity_tolerance=0.01
)

engine = ConstraintEngine(
    event_bus=event_bus,
    db=database,
    config=config
)

# Start monitoring
await engine.start()

# Engine subscribes to MarketUpdated events
# When a market updates, it loads related pairs and checks constraints
# Violations are emitted as ConstraintViolationDetected events
# And recorded in violations and pair_spread_history tables

# Stop when done
await engine.stop()
```

### Market Matching

```python
from core.matching import MarketPairCurator, MarketEmbedder
from core.matching.rules import match_by_rules

# Rule-based matching (fast, deterministic)
result = match_by_rules(
    market_a_title="FOMC Rate Decision March 2026 75bps",
    market_b_title="Will Fed Raise Rates 75bp in March?",
    category="economic"
)
# Returns: MatchResult(pair_type="complement", relationship=None, confidence=0.9)

# Semantic matching (flexible, requires sentence-transformers)
embedder = MarketEmbedder(model_name="all-MiniLM-L6-v2")

matches = embedder.find_matches(
    new_market_title="Federal Reserve Interest Rate Decision Q1",
    existing_titles=[...],
    threshold=0.75
)
# Returns: [(index1, 0.92), (index2, 0.81), ...]

# Manage pairs
curator = MarketPairCurator(db)

pair_id = await curator.add_pair(
    market_id_a="poly_001",
    market_id_b="kalshi_002",
    pair_type="cross_platform",
    match_method="rules",
    created_by="system"
)

# Verify pair (human review)
await curator.verify_pair(pair_id, verified_by="analyst_1")

# Get active pairs
pairs = await curator.get_active_pairs()

# Get statistics
stats = await curator.get_pair_stats()
```

## Configuration

### ConstraintConfig

```python
@dataclass
class ConstraintConfig:
    min_net_spread_single_platform: float = 0.02  # 2%
    min_net_spread_cross_platform: float = 0.03   # 3%
    complementarity_tolerance: float = 0.01       # 1%
    fee_config: Optional[FeeConfig] = None
    enable_logging: bool = True
    max_violation_age_seconds: int = 3600
```

### FeeConfig

```python
@dataclass
class FeeConfig:
    polymarket: float = 0.02  # 2% fee
    kalshi: float = 0.02
    manifold: float = 0.01
    metaculus: float = 0.00
```

## Database Schema

### market_pairs table
```sql
CREATE TABLE market_pairs (
    pair_id VARCHAR(255) PRIMARY KEY,
    market_id_a VARCHAR(255) NOT NULL,
    market_id_b VARCHAR(255) NOT NULL,
    pair_type VARCHAR(50) NOT NULL,  -- "complement", "subset", "cross_platform", etc.
    relationship VARCHAR(50),         -- "subset", "superset", etc.
    match_method VARCHAR(50) NOT NULL, -- "rules" or "embedding"
    similarity_score FLOAT,
    is_active BOOLEAN DEFAULT TRUE,
    verified_by VARCHAR(255),
    verified_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    created_by VARCHAR(255)
);
```

### pair_spread_history table
```sql
CREATE TABLE pair_spread_history (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    pair_id VARCHAR(255) NOT NULL,
    market_a_price FLOAT NOT NULL,
    market_b_price FLOAT NOT NULL,
    raw_spread FLOAT NOT NULL,
    net_spread FLOAT,
    rule_violations JSON,
    recorded_at TIMESTAMP NOT NULL,
    FOREIGN KEY (pair_id) REFERENCES market_pairs(pair_id)
);
```

### violations table
```sql
CREATE TABLE violations (
    violation_id VARCHAR(255) PRIMARY KEY,
    pair_id VARCHAR(255) NOT NULL,
    market_id_a VARCHAR(255) NOT NULL,
    market_id_b VARCHAR(255) NOT NULL,
    rule_type VARCHAR(100) NOT NULL,
    severity VARCHAR(50) NOT NULL,  -- "critical" or "warning"
    description TEXT,
    implied_arbitrage FLOAT,
    detected_at TIMESTAMP NOT NULL,
    is_new BOOLEAN DEFAULT TRUE,
    resolved_at TIMESTAMP,
    FOREIGN KEY (pair_id) REFERENCES market_pairs(pair_id)
);
```

## Events

### MarketUpdated (Input)
Published when a market price updates.

```python
{
    "market_id": "poly_001",
    "platform": "polymarket",
    "title": "Will Trump win 2024?",
    "current_price": 0.65,
    "last_updated": datetime.utcnow()
}
```

### ConstraintViolationDetected (Output)
Published when a constraint violation is detected.

```python
ViolationEvent(
    violation_id="pair_001_cross_platform_1234567890",
    pair_id="pair_001",
    market_id_a="poly_001",
    market_id_b="kalshi_001",
    rule_type="cross_platform",
    severity="critical",
    description="Cross-platform spread too tight...",
    implied_arbitrage=0.5,  # 0.5%
    detected_at=datetime.utcnow(),
    is_new=True
)
```

## Testing

All constraint rules are pure functions with no side effects:

```python
import pytest
from core.constraints.rules import complementarity

def test_complementary_binary_market():
    # Valid complementary prices
    violation = complementarity.check(0.60, 0.40, tolerance=0.01)
    assert violation is None

def test_complementary_violation():
    # Invalid: prices don't sum to 1.0
    violation = complementarity.check(0.60, 0.30, tolerance=0.01)
    assert violation is not None
    assert violation.rule_type == "complementarity"
    assert violation.severity == "critical"
```

## Performance Considerations

1. **Embedder**: Lazy loads sentence-transformers model on first use
2. **Batch Operations**: MarketEmbedder supports batch_embed() and batch_similarity()
3. **Caching**: Cache embeddings of market titles to avoid redundant computation
4. **Async**: All database operations are async
5. **Violation Tracking**: In-memory cache to avoid duplicate events

## Error Handling

- All modules log errors with context
- Database errors are caught and logged, not propagated
- Embedder gracefully falls back if sentence-transformers not installed
- Invalid inputs raise ValueError with descriptive messages

## Production Checklist

- [ ] Configure proper fee rates for each platform
- [ ] Set appropriate spread thresholds for your strategy
- [ ] Implement database schema
- [ ] Test with historical market data
- [ ] Set up violation alerting/logging
- [ ] Run embedder in warm-up phase to load model
- [ ] Monitor constraint engine performance metrics
- [ ] Implement pair verification workflow
- [ ] Set up metrics/monitoring for violations detected
