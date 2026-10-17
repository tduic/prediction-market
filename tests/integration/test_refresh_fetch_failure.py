"""A failed market fetch must not crash the trading session at startup.

fetch_* raise MarketFetchError rather than return a truncated list. If
refresh_markets_and_matches let that escape, a session started with an empty
pair cache while gamma is down would exit; systemd would restart it into the
same failure until StartLimitBurst left the unit permanently failed. It must
log, alert and return no matches so the session idles as it always has.
"""

from types import SimpleNamespace

import pytest

from core import alerting
from core.alerting import AlertManager, Severity
from core.ingestor import store
from core.ingestor.store import MarketFetchError


class _Capture:
    def __init__(self):
        self.alerts = []

    async def publish(self, alert):
        self.alerts.append(alert)
        return True


@pytest.fixture
def captured_alerts():
    cap = _Capture()
    alerting.set_alert_manager(AlertManager([cap]))
    yield cap.alerts
    alerting.set_alert_manager(None)


CFG = SimpleNamespace(
    platform_credentials=SimpleNamespace(
        kalshi_api_key="k", kalshi_rsa_key_path="/x", kalshi_api_base="https://x"
    )
)


async def test_fetch_failure_returns_no_matches_and_alerts(
    db, monkeypatch, captured_alerts, caplog
):
    from scripts import trading_session

    async def poly_down(*a, **k):
        raise MarketFetchError("Polymarket keyset page 3: HTTP 503")

    async def kalshi_ok(*a, **k):
        return []

    monkeypatch.setattr(store, "fetch_polymarket_markets", poly_down)
    monkeypatch.setattr(store, "fetch_kalshi_markets", kalshi_ok)

    with caplog.at_level("ERROR"):
        matches = await trading_session.refresh_markets_and_matches(db, CFG)

    assert matches == []
    assert any("Polymarket keyset page 3" in r.getMessage() for r in caplog.records)
    assert captured_alerts, "no alert sent"
    assert captured_alerts[0].severity in (Severity.CRITICAL, Severity.WARNING)
    assert "fetch" in captured_alerts[0].title.lower()
