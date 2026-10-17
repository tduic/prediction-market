"""
Shared pytest fixtures for the whole suite.

Database fixtures with the real migration schema live in
tests/integration/conftest.py.
"""

import pytest


@pytest.fixture(autouse=True)
def _offline_market_data():
    """Keep every test offline: typical venue fees (Kalshi 0.07 quadratic,
    Polymarket r=0.03) and unlimited depth, unless a test installs its own
    provider with ``set_market_data``."""
    from core.engine.fees import kalshi_params, polymarket_params
    from execution.market_data import StaticMarketData, set_market_data

    set_market_data(
        StaticMarketData(
            fees={
                "kalshi": kalshi_params(1.0),
                "polymarket": polymarket_params(0.03, 1.0),
            }
        )
    )
    yield
    set_market_data(None)
