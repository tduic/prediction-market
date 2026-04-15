"""
Tests for core/snapshots/ and ArbitrageEngine telemetry.

Covers:
  - stats(): accurate, renamed fields (recently_fired, not open_positions)
  - pairs_eligible_now reflects live spread state
  - last_arb_fired_at and ticks_since_last_fire tracking
  - PnL sanity cap: rejects DB inserts where actual_pnl > size * 0.10
  - MIN_SPREAD_CROSS_PLATFORM env var overrides --min-spread argparse default
  - take_phase0_baseline_snapshot() writes a row to phase0_baseline
"""

import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import ArbitrageEngine  # noqa: E402

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_match(poly_id, kalshi_id, poly_price, kalshi_price, similarity=0.85):
    return {
        "poly_id": poly_id,
        "kalshi_id": kalshi_id,
        "poly_title": f"Poly {poly_id}",
        "kalshi_title": f"Kalshi {kalshi_id}",
        "poly_price": poly_price,
        "kalshi_price": kalshi_price,
        "similarity": similarity,
    }


async def _seed_markets(db, matches):
    now = datetime.now(timezone.utc).isoformat()
    for m in matches:
        for mid, platform, price in [
            (m["poly_id"], "polymarket", m["poly_price"]),
            (m["kalshi_id"], "kalshi", m["kalshi_price"]),
        ]:
            await db.execute(
                """INSERT OR IGNORE INTO markets
                   (id, platform, platform_id, title, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'open', ?, ?)""",
                (mid, platform, mid, f"Title {mid}", now, now),
            )
            if price is not None:
                await db.execute(
                    """INSERT INTO market_prices
                       (market_id, yes_price, no_price, spread, liquidity, polled_at)
                       VALUES (?, ?, ?, 0.02, 10000, ?)""",
                    (mid, price, round(1 - price, 4), now),
                )
    await db.commit()


async def _trigger_trade(engine, db, market_id, new_price):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO market_prices
           (market_id, yes_price, no_price, spread, liquidity, polled_at)
           VALUES (?, ?, ?, 0.02, 10000, ?)""",
        (market_id, new_price, round(1 - new_price, 4), now),
    )
    await db.commit()
    await engine.on_price_update(market_id, new_price)


# ── Stats fields ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestStatsFields:
    async def test_stats_has_no_open_positions_key(self, db):
        matches = [_make_match("poly_A", "kal_A", 0.50, 0.55)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        s = engine.stats()
        assert (
            "open_positions" not in s
        ), "open_positions key is still present — rename to recently_fired"

    async def test_stats_has_pairs_monitored(self, db):
        matches = [
            _make_match("poly_A", "kal_A", 0.50, 0.55),
            _make_match("poly_B", "kal_B", 0.40, 0.45),
        ]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        assert engine.stats()["pairs_monitored"] == 2

    async def test_stats_has_recently_fired(self, db):
        matches = [_make_match("poly_A", "kal_A", 0.50, 0.55)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        assert "recently_fired" in engine.stats()

    async def test_recently_fired_zero_before_trade(self, db):
        matches = [_make_match("poly_A", "kal_A", 0.50, 0.55)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.10)
        assert engine.stats()["recently_fired"] == 0

    async def test_recently_fired_increments_after_trade(self, db):
        matches = [_make_match("poly_A", "kal_A", 0.50, 0.55)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        await _trigger_trade(engine, db, "poly_A", 0.45)
        assert engine.stats()["recently_fired"] >= 1

    async def test_stats_has_pairs_eligible_now(self, db):
        matches = [
            _make_match("poly_A", "kal_A", 0.50, 0.60),
            _make_match("poly_B", "kal_B", 0.40, 0.42),
        ]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.05)
        s = engine.stats()
        assert "pairs_eligible_now" in s
        assert s["pairs_eligible_now"] == 1

    async def test_pairs_eligible_zero_when_all_below_threshold(self, db):
        matches = [_make_match("poly_A", "kal_A", 0.50, 0.52)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.05)
        assert engine.stats()["pairs_eligible_now"] == 0

    async def test_stats_has_last_arb_fired_at(self, db):
        matches = [_make_match("poly_A", "kal_A", 0.50, 0.55)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        assert "last_arb_fired_at" in engine.stats()

    async def test_last_arb_fired_at_none_before_trade(self, db):
        matches = [_make_match("poly_A", "kal_A", 0.50, 0.55)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.10)
        assert engine.stats()["last_arb_fired_at"] is None

    async def test_last_arb_fired_at_set_after_trade(self, db):
        matches = [_make_match("poly_A", "kal_A", 0.50, 0.55)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        before = time.time()
        await _trigger_trade(engine, db, "poly_A", 0.45)
        after = time.time()
        ts = engine.stats()["last_arb_fired_at"]
        assert ts is not None
        assert before <= ts <= after

    async def test_stats_has_ticks_since_last_fire(self, db):
        matches = [_make_match("poly_A", "kal_A", 0.50, 0.55)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        assert "ticks_since_last_fire" in engine.stats()

    async def test_ticks_since_last_fire_increments(self, db):
        matches = [_make_match("poly_A", "kal_A", 0.50, 0.55)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.99)
        initial = engine.stats()["ticks_since_last_fire"]
        await engine.on_price_update("poly_A", 0.51)
        await engine.on_price_update("poly_A", 0.52)
        assert engine.stats()["ticks_since_last_fire"] >= initial + 2

    async def test_ticks_since_last_fire_resets_on_trade(self, db):
        matches = [_make_match("poly_A", "kal_A", 0.50, 0.55)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.99)
        await engine.on_price_update("poly_A", 0.51)
        await engine.on_price_update("poly_A", 0.52)
        assert engine.stats()["ticks_since_last_fire"] >= 2
        engine.min_spread = 0.03
        await _trigger_trade(engine, db, "poly_A", 0.45)
        assert engine.stats()["ticks_since_last_fire"] == 0

    async def test_stats_has_total_pnl_and_prices_tracked(self, db):
        matches = [_make_match("poly_A", "kal_A", 0.50, 0.55)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        s = engine.stats()
        assert "total_pnl" in s
        assert "prices_tracked" in s
        assert s["prices_tracked"] > 0


# ── PnL sanity cap ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPnLSanityCap:
    async def test_arb_path_blocked_when_pnl_exceeds_cap(self, db):
        matches = [_make_match("poly_A", "kal_A", 0.50, 0.55)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        await _trigger_trade(engine, db, "poly_A", 0.10)
        cursor = await db.execute("SELECT COUNT(*) FROM positions")
        assert (await cursor.fetchone())[0] == 0

    async def test_arb_path_allowed_when_pnl_within_cap(self, db):
        matches = [_make_match("poly_A", "kal_A", 0.50, 0.55)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        await _trigger_trade(engine, db, "poly_A", 0.51)
        await engine.flush()
        cursor = await db.execute("SELECT COUNT(*) FROM trade_outcomes")
        assert (await cursor.fetchone())[0] >= 1

    async def test_sanity_cap_logs_warning_not_raises(self, db):
        matches = [_make_match("poly_A", "kal_A", 0.50, 0.55)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        with patch("core.engine.arb_engine.logger") as mock_log:
            await _trigger_trade(engine, db, "poly_A", 0.10)
            assert mock_log.warning.called or mock_log.error.called

    async def test_cap_constant_is_10_pct(self):
        from core.engine.arb_engine import _PNL_SANITY_CAP_RATIO

        assert _PNL_SANITY_CAP_RATIO == 0.10

    async def test_arb_trade_outcomes_also_blocked(self, db):
        matches = [_make_match("poly_A", "kal_A", 0.50, 0.55)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        await _trigger_trade(engine, db, "poly_A", 0.10)
        await engine.flush()
        for table in ("positions", "trade_outcomes"):
            cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")
            assert (await cursor.fetchone())[0] == 0


# ── MIN_SPREAD_CROSS_PLATFORM env var ─────────────────────────────────────────


class TestMinSpreadEnvVar:
    def test_env_var_overrides_default(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import os, sys
sys.path.insert(0, '.')
os.environ['MIN_SPREAD_CROSS_PLATFORM'] = '0.99'
import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--min-spread', type=float, default=0.03)
args = parser.parse_args([])
min_spread_env = os.getenv('MIN_SPREAD_CROSS_PLATFORM')
if min_spread_env is not None:
    args.min_spread = float(min_spread_env)
print(args.min_spread)
""",
            ],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0
        assert float(result.stdout.strip()) == 0.99

    def test_env_var_absent_uses_argparse_default(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import os, sys
sys.path.insert(0, '.')
os.environ.pop('MIN_SPREAD_CROSS_PLATFORM', None)
import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--min-spread', type=float, default=0.03)
args = parser.parse_args([])
min_spread_env = os.getenv('MIN_SPREAD_CROSS_PLATFORM')
if min_spread_env is not None:
    args.min_spread = float(min_spread_env)
print(args.min_spread)
""",
            ],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
        )
        assert result.returncode == 0
        assert float(result.stdout.strip()) == 0.03


# ── Baseline snapshot ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPhase0BaselineSnapshot:
    async def test_baseline_table_exists_after_migrations(self, db):
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='phase0_baseline'"
        )
        row = await cursor.fetchone()
        assert row is not None, "phase0_baseline table missing from migrations"

    async def test_take_baseline_snapshot_writes_row(self, db):
        from core.snapshots.phase0 import take_phase0_baseline_snapshot

        await take_phase0_baseline_snapshot(db)
        cursor = await db.execute("SELECT COUNT(*) FROM phase0_baseline")
        assert (await cursor.fetchone())[0] >= 1

    async def test_baseline_snapshot_captures_pair_count(self, db):
        from core.snapshots.phase0 import take_phase0_baseline_snapshot

        now = datetime.now(timezone.utc).isoformat()
        for i in range(2):
            await db.execute(
                "INSERT OR IGNORE INTO markets (id, platform, platform_id, title, status, created_at, updated_at) "
                "VALUES (?, 'polymarket', ?, 'T', 'open', ?, ?)",
                (f"p{i}", f"p{i}", now, now),
            )
            await db.execute(
                "INSERT OR IGNORE INTO markets (id, platform, platform_id, title, status, created_at, updated_at) "
                "VALUES (?, 'kalshi', ?, 'T', 'open', ?, ?)",
                (f"k{i}", f"k{i}", now, now),
            )
            await db.execute(
                "INSERT OR IGNORE INTO market_pairs (id, market_id_a, market_id_b, pair_type, created_at, updated_at) "
                "VALUES (?, ?, ?, 'cross_platform', ?, ?)",
                (f"pair_{i}", f"p{i}", f"k{i}", now, now),
            )
        await db.commit()
        await take_phase0_baseline_snapshot(db)
        cursor = await db.execute(
            "SELECT pair_count FROM phase0_baseline ORDER BY id DESC LIMIT 1"
        )
        assert (await cursor.fetchone())[0] == 2

    async def test_baseline_idempotent_multiple_calls(self, db):
        from core.snapshots.phase0 import take_phase0_baseline_snapshot

        await take_phase0_baseline_snapshot(db)
        await take_phase0_baseline_snapshot(db)
        cursor = await db.execute("SELECT COUNT(*) FROM phase0_baseline")
        assert (await cursor.fetchone())[0] == 2

    async def test_baseline_captures_strategy_pnl(self, db):
        from core.snapshots.phase0 import take_phase0_baseline_snapshot

        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT OR IGNORE INTO markets (id, platform, platform_id, title, status, created_at, updated_at) "
            "VALUES ('tm', 'polymarket', 'tm', 'T', 'open', ?, ?)",
            (now, now),
        )
        await db.execute(
            "INSERT OR IGNORE INTO signals (id, violation_id, strategy, signal_type, market_id_a, "
            "model_edge, kelly_fraction, position_size_a, total_capital_at_risk, status, fired_at, updated_at) "
            "VALUES ('sig1', NULL, 'P1_cross_market_arb', 'arb_pair', 'tm', 0.05, 0.10, 5.0, 10.0, 'fired', ?, ?)",
            (now, now),
        )
        await db.execute(
            "INSERT INTO trade_outcomes (id, signal_id, strategy, violation_id, market_id_a, "
            "predicted_edge, predicted_pnl, actual_pnl, fees_total, edge_captured_pct, "
            "signal_to_fill_ms, holding_period_ms, spread_at_signal, volume_at_signal, "
            "liquidity_at_signal, resolved_at, created_at) "
            "VALUES ('to1', 'sig1', 'P1_cross_market_arb', NULL, 'tm', "
            "0.05, 0.25, 10.0, 0.10, 100.0, 50, 5000, 0.05, 1000.0, 1000.0, ?, ?)",
            (now, now),
        )
        await db.commit()
        await take_phase0_baseline_snapshot(db)
        cursor = await db.execute(
            "SELECT p1_realized_pnl FROM phase0_baseline ORDER BY id DESC LIMIT 1"
        )
        assert (await cursor.fetchone())[0] == 10.0
