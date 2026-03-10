# Prediction Market Trading System - Test Suite

Comprehensive pytest-based test suite for the prediction market arbitrage trading system.

## Overview

This test suite provides complete coverage across the prediction market trading pipeline:

- **Unit Tests**: Individual component testing with isolated dependencies
- **Integration Tests**: End-to-end flow testing with realistic data
- **Fixtures**: Shared test data, database schemas, and configuration

## Structure

```
tests/
├── __init__.py
├── conftest.py                 # Shared fixtures and configuration
├── fixtures/
│   ├── markets.json           # 20 realistic markets (10 Polymarket, 10 Kalshi)
│   └── violations.json        # 5 constraint violation examples
├── unit/
│   ├── __init__.py
│   ├── test_constraints.py    # Constraint detection (7 test classes, 30+ tests)
│   ├── test_risk.py           # Risk management (6 test classes, 35+ tests)
│   ├── test_sizing.py         # Kelly sizing (8 test classes, 25+ tests)
│   ├── test_matching.py       # Market pair matching (5 test classes, 20+ tests)
│   └── test_models.py         # Prediction models (5 test classes, 20+ tests)
└── integration/
    ├── __init__.py
    ├── test_ingestor.py       # Data ingestion (3 test classes, 12+ tests)
    ├── test_signal_flow.py    # Signal pipeline (4 test classes, 10+ tests)
    └── test_execution.py      # Order execution (5 test classes, 15+ tests)
```

## Test Categories

### Unit Tests

#### test_constraints.py
Tests constraint violation detection:
- **TestSubsetSupersetConstraints**: Subset/superset relationship validation
- **TestMutualExclusivityConstraints**: Mutually exclusive market validation
- **TestComplementarityConstraints**: YES/NO price sum validation
- **TestCrossPlatformConstraints**: Cross-platform spread analysis
- **TestConstraintEngineIntegration**: Full engine with event bus

Key tests:
- Violation detection with realistic price data
- Fee calculation and net spread computation
- Event emission and event bus integration

#### test_risk.py
Tests risk management and position sizing:
- **TestPositionLimitChecks**: Position size limit enforcement
- **TestDailyLossLimitChecks**: Daily loss limit enforcement
- **TestConcentrationLimitChecks**: Portfolio concentration limits
- **TestKellySizing**: Kelly criterion sizing
- **TestDuplicateSignalSuppression**: Duplicate signal detection
- **TestAllRiskChecks**: Combined risk checking

Key tests:
- Position limit validation (hard caps)
- Daily loss tracking and enforcement
- Kelly fraction capping and fractional Kelly application
- Signal deduplication with time windows

#### test_sizing.py
Tests position sizing calculations:
- **TestKellySizingBasics**: Basic Kelly calculations
- **TestKellyCapping**: Kelly fraction capping logic
- **TestEdgeCases**: Edge/zero/negative inputs
- **TestSizingWithCapital**: Capital-aware sizing
- **TestEdgeComputation**: Edge derivation from spreads
- **TestSizingFromSpreadBasis**: Direct sizing from basis points

Key tests:
- Quarter Kelly sizing with known inputs
- Kelly ceiling enforcement (0.5 max)
- Fractional Kelly multiplier application
- Edge-to-size conversion

#### test_matching.py
Tests market pair matching:
- **TestRuleBasedMatching**: FOMC, CPI, unemployment matching
- **TestEmbeddingSimilarityMatching**: Text similarity matching
- **TestMarketPairCurator**: Pair lifecycle management
- Verification, activation, deactivation workflows

Key tests:
- FOMC market matching by date
- CPI market matching
- Text similarity computation (Jaccard)
- Pair CRUD operations
- Pair status transitions

#### test_models.py
Tests prediction models:
- **TestFOCMModel**: FOMC rate prediction model
- **TestCalibrationModel**: Market price calibration
- **TestModelRegistry**: Model lifecycle management
- **TestWalkForwardValidation**: Data leakage prevention

Key tests:
- Insufficient data rejection
- Model training with sufficient samples
- Prediction generation with features
- Walk-forward validation with proper train/test splits
- Data leakage detection

### Integration Tests

#### test_ingestor.py
Tests market data ingestion:
- **TestIngestorDataFlow**: Market insertion and updates
- **TestIngestorRateLimiting**: Rate limit handling
- **TestIngestorMultipleCycles**: Multiple ingest runs

Key tests:
- Markets inserted on first poll
- Prices appended (append-only history)
- Rate limit (429) handling with backoff
- Ingestor run tracking
- Update on second poll

#### test_signal_flow.py
Tests signal generation pipeline:
- **TestViolationToSignalFlow**: Violation → Signal → Queue
- **TestSignalRiskChecks**: Risk filtering
- **TestSignalMultipleCycles**: Multiple signal handling

Key tests:
- Complete flow: violation detection → signal generation → queuing
- Paper trading mode (log only, no queue)
- Signal expiration checking
- Risk check filtering
- Paper trading vs live trading modes

#### test_execution.py
Tests order execution service:
- **TestOrderSubmissionAndFills**: Order submission and fill tracking
- **TestConcurrentExecution**: Parallel leg submission
- **TestExecutionErrorHandling**: Error scenarios

Key tests:
- Order submission to platforms
- Fill confirmation with database updates
- Partial fill detection
- Concurrent order submission (both legs)
- Sequential order submission (A then B)
- Unknown platform rejection
- Fill timeout handling

## Running Tests

### Install Dependencies

```bash
pip install pytest pytest-asyncio
```

### Run All Tests

```bash
pytest tests/
```

### Run Specific Test Categories

```bash
# Unit tests only
pytest tests/unit/

# Integration tests only
pytest tests/integration/

# Specific test file
pytest tests/unit/test_constraints.py

# Specific test class
pytest tests/unit/test_constraints.py::TestSubsetSupersetConstraints

# Specific test
pytest tests/unit/test_constraints.py::TestSubsetSupersetConstraints::test_subset_superset_detects_violation
```

### Verbose Output

```bash
pytest tests/ -v
```

### Show Print Statements

```bash
pytest tests/ -s
```

### Run with Coverage

```bash
pytest tests/ --cov=prediction_market --cov-report=html
```

### Run Only Async Tests

```bash
pytest tests/ -m asyncio
```

### Run Excluding Slow Tests

```bash
pytest tests/ -m "not slow"
```

## Test Data

### Fixtures

#### markets.json
10 Polymarket and 10 Kalshi markets covering:
- **Macro**: FOMC rate cuts, CPI, unemployment, PCE inflation
- **Crypto**: Bitcoin ($50k), Ethereum ($3k), Solana ($200)
- **Equities**: NVIDIA, S&P 500
- **Commodities**: Gold ($2,100), Oil ($90)
- **Currency**: US Dollar Index ($105)

Each market includes realistic pricing, categories, and event types.

#### violations.json
5 constraint violation examples:
1. **Subset/Superset**: Subset exceeds superset (violation)
2. **Mutual Exclusivity**: Sum of YES prices exceeds 1.0 (violation)
3. **Complementarity**: YES + NO = 1.0 within tolerance (no violation)
4. **Cross-Platform**: Small spread below profitability threshold (no violation)
5. **Cross-Platform Large**: Large spread but still unprofitable after fees (no violation)

### Shared Fixtures (conftest.py)

- **in_memory_db**: SQLite database with full schema
- **sample_config**: Test configuration with paper trading enabled
- **event_bus**: In-memory event bus for testing
- **sample_markets**: Loaded market data by platform
- **sample_violations**: Loaded violation data

## Mock Implementations

The test suite includes complete mock implementations for:

- **ConstraintEngine**: Constraint detection with all rules
- **RiskManager**: Risk checking and Kelly sizing
- **PositionSizer**: Kelly criterion calculations
- **MarketMatcher**: Rule-based and embedding similarity matching
- **MarketPairCurator**: Pair lifecycle management
- **FOCMModel**: FOMC prediction model
- **CalibrationModel**: Market calibration
- **ModelRegistry**: Model versioning and lifecycle
- **MarketIngestor**: Data ingestion pipeline
- **SignalGenerator**: Signal creation from violations
- **ExecutionService**: Order submission and execution
- **PlatformClientMock**: Mock platform APIs

## Key Features

1. **Comprehensive Coverage**
   - 120+ test cases
   - Unit and integration tests
   - Realistic test data

2. **Async Support**
   - pytest-asyncio for async test functions
   - Async fixtures and utilities
   - Concurrent execution testing

3. **Database Testing**
   - In-memory SQLite for fast tests
   - Full schema with constraints
   - Transaction management

4. **Event Bus Testing**
   - Event emission and subscription
   - Event filtering and inspection
   - Integration with services

5. **Mock Platform Clients**
   - Configurable response delays
   - Rate limit simulation
   - Order fill simulation

6. **Risk Management Testing**
   - Position limit enforcement
   - Daily loss tracking
   - Portfolio concentration
   - Kelly fraction capping

7. **Constraint Detection Testing**
   - All constraint types
   - Fee calculations
   - Spread analysis
   - Violation detection

## Test Patterns

### Unit Test Pattern

```python
class TestFeatureName:
    """Test feature description."""

    def test_specific_behavior(self, sample_config):
        """Test docstring describing behavior."""
        # Arrange
        engine = ConstraintEngine(sample_config)

        # Act
        result = engine.check_constraint(price_a=0.72, price_b=0.78)

        # Assert
        assert result is True
```

### Integration Test Pattern

```python
@pytest.mark.asyncio
async def test_full_flow(self, in_memory_db, event_bus, sample_config):
    """Test end-to-end flow."""
    # Setup
    ingestor = MarketIngestor(in_memory_db)

    # Execute
    result = await ingestor.run_ingest_cycle()

    # Verify
    assert result["success"] is True
    markets = in_memory_db.execute("SELECT * FROM markets")
    assert len(markets) > 0
```

## Expected Test Results

Running all tests should produce output similar to:

```
tests/unit/test_constraints.py::TestSubsetSupersetConstraints::test_subset_superset_detects_violation PASSED
tests/unit/test_constraints.py::TestSubsetSupersetConstraints::test_subset_superset_no_violation PASSED
...
tests/integration/test_execution.py::TestExecutionErrorHandling::test_fill_timeout_returns_error PASSED

========================== 120+ passed in 15.23s ==========================
```

## Debugging Tests

### Print Debug Information

```bash
pytest tests/unit/test_constraints.py -v -s
```

### Stop on First Failure

```bash
pytest tests/ -x
```

### Show Local Variables on Failure

```bash
pytest tests/ -l
```

### Use PDB on Failure

```bash
pytest tests/ --pdb
```

## CI/CD Integration

Example GitHub Actions workflow:

```yaml
name: Tests
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v2
      - uses: actions/setup-python@v2
        with:
          python-version: '3.9'
      - run: pip install pytest pytest-asyncio
      - run: pytest tests/ -v
```

## Performance

- Unit tests: ~10 seconds for all unit tests
- Integration tests: ~10 seconds for all integration tests
- Total: ~15-20 seconds for full suite

## Notes

- All tests use in-memory SQLite for speed
- Async tests use pytest-asyncio with auto mode
- Mock objects simulate realistic behavior
- No external API calls required
- All tests are deterministic and repeatable
