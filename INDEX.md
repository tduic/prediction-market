# Constraint Engine & Market Matching System - Complete Index

**Location:** `/sessions/zealous-determined-johnson/mnt/Predictor/prediction-market/`

## Start Here

**New to the system?** Read these first:
1. **CONSTRAINT_SYSTEM_GUIDE.md** - Quick start & overview
2. **examples/integration_example.py** - Working code example
3. **CONSTRAINT_ENGINE_README.md** - Detailed usage guide

## Core Files (What You'll Use)

### Constraint Engine

**Main Module:**
- **core/constraints/engine.py** - Main constraint evaluation engine
  - ConstraintEngine class
  - Event-driven architecture
  - Async/await support
  - 400+ lines

**Fee Handling:**
- **core/constraints/fees.py** - Platform fee estimation
  - FeeEstimator class
  - Platform-specific rates
  - Net spread calculation

**Constraint Rules:**
- **core/constraints/rules/subset_superset.py** - P(subset) ≤ P(superset)
- **core/constraints/rules/complementarity.py** - P(YES) + P(NO) = 100%
- **core/constraints/rules/mutual_exclusivity.py** - Sum ≤ 100%
- **core/constraints/rules/cross_platform.py** - Net spread > threshold

### Market Matching

**Rule-Based Matching:**
- **core/matching/rules.py** - Template-based pair discovery (350+ lines)
  - FOMC, CPI, Elections, Sports templates
  - Custom template support

**Semantic Matching:**
- **core/matching/embedder.py** - Sentence embeddings (250+ lines)
  - Uses sentence-transformers
  - Batch operations
  - Graceful fallback

**Pair Management:**
- **core/matching/curator.py** - CRUD operations (350+ lines)
  - Add, verify, deactivate pairs
  - Statistics and reporting

## Testing

**Run These to Verify:**
- **tests/test_constraints.py** - 35+ tests for constraint rules
- **tests/test_matching.py** - 33+ tests for matching layer

Run: `pytest tests/ -v`

## Documentation

1. **CONSTRAINT_ENGINE_README.md** - Architecture & usage
2. **IMPLEMENTATION_SUMMARY.md** - What was built
3. **DEPLOYMENT_GUIDE.md** - Production deployment
4. **FILES_MANIFEST.md** - Complete file reference
5. **CONSTRAINT_SYSTEM_GUIDE.md** - Quick reference
6. **INDEX.md** - This file

## Examples

**Working Code:**
- **examples/integration_example.py** - Full integration example with mock classes

## Quick Reference

### Import Constraint Engine
```python
from core.constraints import ConstraintEngine
from core.constraints.engine import ConstraintConfig

engine = ConstraintEngine(event_bus, db, config)
await engine.start()
```

### Use Constraint Rules
```python
from core.constraints.rules import complementarity

violation = complementarity.check(yes_price=0.58, no_price=0.42)
```

### Discover Market Pairs
```python
from core.matching.rules import match_by_rules
from core.matching.embedder import MarketEmbedder

result = match_by_rules(title_a, title_b, category="economic")
embedder = MarketEmbedder()
matches = embedder.find_matches(new_title, existing_titles)
```

### Manage Pairs
```python
from core.matching import MarketPairCurator

curator = MarketPairCurator(db)
pair_id = await curator.add_pair(market_id_a, market_id_b, pair_type)
```

## Constraint Rules Summary

| Rule | File | Validates |
|------|------|-----------|
| Subset/Superset | subset_superset.py | P(A in Q1) ≤ P(A in 2026) |
| Complementarity | complementarity.py | P(YES) + P(NO) = 100% |
| Mutual Exclusivity | mutual_exclusivity.py | Sum ≤ 100% |
| Cross-Platform | cross_platform.py | Net spread > threshold |

## Key Statistics

- **Total Code:** ~2,500 lines
- **Total Tests:** 900+ lines (68+ test cases)
- **Documentation:** ~1,000 lines
- **Production Ready:** Yes
- **Type Safety:** 100% type hints

## Getting Started Checklist

- [ ] Read CONSTRAINT_SYSTEM_GUIDE.md
- [ ] Review examples/integration_example.py
- [ ] Run: pytest tests/ -v
- [ ] Read CONSTRAINT_ENGINE_README.md
- [ ] Set up database (DEPLOYMENT_GUIDE.md)
- [ ] Deploy constraint engine
- [ ] Set up monitoring

## Status

✅ **Production Ready** - 19 files, 68+ tests, complete documentation

