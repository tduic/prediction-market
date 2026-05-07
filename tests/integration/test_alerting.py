"""
Tests for core.alerting.

Covers:
  - NullTransport default (no webhook configured)
  - DiscordWebhookTransport posts correct JSON shape
  - Severity → colour mapping
  - Dedup suppresses duplicates within the window
  - Multi-transport fanout
  - send_nowait schedules on running loop without awaiting
  - Webhook failure does not raise
  - Context fields truncated safely
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # noqa: E402

from core.alerting import (  # noqa: E402
    Alert,
    AlertManager,
    DiscordWebhookTransport,
    NullTransport,
    Severity,
    get_alert_manager,
    set_alert_manager,
)


@pytest.fixture(autouse=True)
def reset_manager():
    set_alert_manager(None)
    yield
    set_alert_manager(None)


class CollectingTransport:
    """Transport that records alerts instead of sending them."""

    name = "collecting"

    def __init__(self, return_value: bool = True) -> None:
        self.alerts: list[Alert] = []
        self.return_value = return_value

    async def publish(self, alert: Alert) -> bool:
        self.alerts.append(alert)
        return self.return_value


@pytest.mark.asyncio
async def test_null_transport_returns_true():
    t = NullTransport()
    alert = Alert(title="t", message="m")
    assert await t.publish(alert) is True


@pytest.mark.asyncio
async def test_manager_default_uses_null_when_no_webhook(monkeypatch):
    monkeypatch.delenv("ALERT_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_SLACK_WEBHOOK_URL", raising=False)
    mgr = get_alert_manager()
    assert len(mgr.transports) == 1
    assert mgr.transports[0].name == "null"


@pytest.mark.asyncio
async def test_manager_picks_up_discord_webhook(monkeypatch):
    monkeypatch.setenv(
        "ALERT_DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123/abc"
    )
    monkeypatch.delenv("ALERT_SLACK_WEBHOOK_URL", raising=False)
    set_alert_manager(None)
    mgr = get_alert_manager()
    assert any(t.name == "discord" for t in mgr.transports)


@pytest.mark.asyncio
async def test_send_dispatches_to_all_transports():
    t1 = CollectingTransport()
    t2 = CollectingTransport()
    mgr = AlertManager(transports=[t1, t2], dedup_window_s=60)

    ok = await mgr.send(
        title="Test alert",
        message="Something happened",
        severity=Severity.WARNING,
        context={"foo": "bar"},
        component="unit-test",
    )
    assert ok is True
    assert len(t1.alerts) == 1
    assert len(t2.alerts) == 1
    assert t1.alerts[0].title == "Test alert"
    assert t1.alerts[0].severity == Severity.WARNING
    assert t1.alerts[0].context == {"foo": "bar"}
    assert t1.alerts[0].component == "unit-test"


@pytest.mark.asyncio
async def test_dedup_suppresses_repeat():
    t = CollectingTransport()
    mgr = AlertManager(transports=[t], dedup_window_s=60)

    await mgr.send(title="same", message="first", severity=Severity.INFO)
    await mgr.send(title="same", message="second", severity=Severity.INFO)
    await mgr.send(title="same", message="third", severity=Severity.INFO)

    assert len(t.alerts) == 1
    assert t.alerts[0].message == "first"


@pytest.mark.asyncio
async def test_dedup_allows_different_titles():
    t = CollectingTransport()
    mgr = AlertManager(transports=[t], dedup_window_s=60)

    await mgr.send(title="a", message="m", severity=Severity.INFO)
    await mgr.send(title="b", message="m", severity=Severity.INFO)

    assert len(t.alerts) == 2


@pytest.mark.asyncio
async def test_dedup_allows_different_severities():
    """Same title at different severity should NOT be deduped."""
    t = CollectingTransport()
    mgr = AlertManager(transports=[t], dedup_window_s=60)

    await mgr.send(title="same", message="m", severity=Severity.INFO)
    await mgr.send(title="same", message="m", severity=Severity.CRITICAL)

    assert len(t.alerts) == 2


@pytest.mark.asyncio
async def test_dedup_window_expires():
    import time as _t

    t = CollectingTransport()
    mgr = AlertManager(transports=[t], dedup_window_s=0.01)

    await mgr.send(title="same", message="1", severity=Severity.INFO)
    _t.sleep(0.02)
    await mgr.send(title="same", message="2", severity=Severity.INFO)

    assert len(t.alerts) == 2


@pytest.mark.asyncio
async def test_failed_transport_does_not_crash():
    class FailingTransport:
        name = "fail"

        async def publish(self, alert):
            raise RuntimeError("boom")

    good = CollectingTransport()
    mgr = AlertManager(transports=[FailingTransport(), good], dedup_window_s=60)

    ok = await mgr.send(title="t", message="m", severity=Severity.INFO)
    # Good transport still delivered
    assert ok is True
    assert len(good.alerts) == 1


@pytest.mark.asyncio
async def test_all_transports_failing_returns_false():
    class FailingTransport:
        name = "fail"

        async def publish(self, alert):
            return False

    mgr = AlertManager(
        transports=[FailingTransport(), FailingTransport()], dedup_window_s=60
    )
    ok = await mgr.send(title="t", message="m", severity=Severity.CRITICAL)
    assert ok is False


@pytest.mark.asyncio
async def test_send_nowait_schedules_task():
    t = CollectingTransport()
    mgr = AlertManager(transports=[t], dedup_window_s=60)

    mgr.send_nowait(title="bg", message="async", severity=Severity.INFO)
    # Yield control so the scheduled task can run
    import asyncio as _a

    await _a.sleep(0)
    await _a.sleep(0)
    assert len(t.alerts) == 1


@pytest.mark.asyncio
async def test_send_nowait_retains_strong_task_reference():
    """Without a strong reference, asyncio only weakly holds scheduled tasks
    and the GC can cancel them mid-flight. AlertManager must keep them in
    _pending_tasks until they complete, then drop them."""
    import asyncio as _a

    t = CollectingTransport()
    mgr = AlertManager(transports=[t], dedup_window_s=60)

    mgr.send_nowait(title="ref", message="m", severity=Severity.INFO)

    # While the task is pending it must be retained.
    assert len(mgr._pending_tasks) == 1

    # Explicitly await the scheduled task to completion, then yield once more
    # so the done_callback (which .discard()s from _pending_tasks) runs.
    pending = list(mgr._pending_tasks)
    await _a.gather(*pending)
    await _a.sleep(0)

    assert len(t.alerts) == 1
    assert len(mgr._pending_tasks) == 0  # cleaned up after done


def test_send_nowait_outside_loop_is_safe():
    """send_nowait called with no running loop should not raise."""
    t = CollectingTransport()
    mgr = AlertManager(transports=[t], dedup_window_s=60)
    mgr.send_nowait(title="no-loop", message="m")  # should just log and return
    assert len(t.alerts) == 0


@pytest.mark.asyncio
async def test_discord_format_builds_embed():
    transport = DiscordWebhookTransport(webhook_url="https://example.invalid/webhook")
    alert = Alert(
        title="Test",
        message="Message body",
        severity=Severity.CRITICAL,
        context={"a": 1, "b": 2},
        component="unit",
    )
    payload = transport._format(alert)

    assert "embeds" in payload
    embed = payload["embeds"][0]
    assert "Test" in embed["title"]
    assert embed["description"] == "Message body"
    assert embed["color"] == 0xE74C3C  # critical = red
    assert {"name": "a", "value": "`1`", "inline": True} in embed["fields"]
    assert payload.get("content", "").startswith("@here ")  # critical pings
    assert payload["username"] == "prediction-market-bot"


@pytest.mark.asyncio
async def test_discord_info_severity_no_ping():
    transport = DiscordWebhookTransport(webhook_url="https://example.invalid/webhook")
    alert = Alert(title="Info", message="m", severity=Severity.INFO)
    payload = transport._format(alert)
    assert "content" in payload  # always set for Slack compat
    assert not payload["content"].startswith("@here")  # no ping for non-critical
    assert payload["embeds"][0]["color"] == 0x3498DB  # blue


@pytest.mark.asyncio
async def test_discord_context_truncation():
    transport = DiscordWebhookTransport(webhook_url="https://example.invalid/webhook")
    big_context = {f"key_{i}": f"val_{i}" for i in range(50)}
    alert = Alert(title="t", message="m", context=big_context)
    payload = transport._format(alert)
    # Capped at 10 fields
    assert len(payload["embeds"][0]["fields"]) == 10


@pytest.mark.asyncio
async def test_discord_webhook_http_failure_returns_false(monkeypatch):
    """A webhook HTTP failure should return False, not raise."""
    transport = DiscordWebhookTransport(webhook_url="https://example.invalid/webhook")

    class FakeResponse:
        status_code = 500
        text = "internal server error"

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            return FakeResponse()

    monkeypatch.setattr("core.alerting.httpx.AsyncClient", FakeClient)

    alert = Alert(title="t", message="m")
    ok = await transport.publish(alert)
    assert ok is False


@pytest.mark.asyncio
async def test_discord_webhook_success(monkeypatch):
    transport = DiscordWebhookTransport(webhook_url="https://example.invalid/webhook")

    posted = {}

    class FakeResponse:
        status_code = 204
        text = ""

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            posted["url"] = url
            posted["json"] = json
            return FakeResponse()

    monkeypatch.setattr("core.alerting.httpx.AsyncClient", FakeClient)

    alert = Alert(title="ok", message="ok", severity=Severity.WARNING, component="test")
    ok = await transport.publish(alert)
    assert ok is True
    assert posted["url"] == "https://example.invalid/webhook"
    assert "embeds" in posted["json"]


@pytest.mark.asyncio
async def test_send_accepts_string_severity():
    t = CollectingTransport()
    mgr = AlertManager(transports=[t], dedup_window_s=60)
    await mgr.send(title="t", message="m", severity="critical")
    assert t.alerts[0].severity == Severity.CRITICAL


@pytest.mark.asyncio
async def test_integration_via_asyncmock():
    """Verify the transport interface works with unittest AsyncMock too."""
    mock_transport = AsyncMock()
    mock_transport.name = "mock"
    mock_transport.publish.return_value = True

    mgr = AlertManager(transports=[mock_transport], dedup_window_s=60)
    ok = await mgr.send(title="t", message="m", severity=Severity.INFO)
    assert ok is True
    mock_transport.publish.assert_awaited_once()
