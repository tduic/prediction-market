"""
Pluggable alerting for critical system events.

Provides a tiny ``AlertManager`` that publishes messages to one or more
transports. The primary transport is Discord webhooks (shape-compatible with
Slack webhooks, so swapping platforms is one env var), with a no-op
``NullTransport`` used by default so tests and local dev stay silent.

Design goals:
  - No-op by default. If ``ALERT_DISCORD_WEBHOOK_URL`` is unset, alerts are
    dropped silently. Production can enable by setting the env var; tests
    and CI never need to know about it.
  - Non-blocking. Alerts are fire-and-forget — a failing webhook must never
    crash the caller (circuit breaker, reconciliation, etc.) or slow down
    the hot path. All network calls are wrapped in try/except with short
    timeouts and run in background tasks when called from async code.
  - Rate limited + deduped. A repeated alert within ``dedup_window_s`` is
    suppressed so a flapping component can't spam the channel.
  - Severity levels. ``info`` / ``warning`` / ``critical`` map to coloured
    Discord embeds so ops can triage at a glance.

Usage::

    from core.alerting import get_alert_manager, Severity

    mgr = get_alert_manager()
    await mgr.send(
        title="Circuit breaker tripped",
        message="Daily loss $215 >= limit $200",
        severity=Severity.CRITICAL,
        context={"daily_loss": 215.0, "limit": 200.0},
    )
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


# Discord embed colours (decimal integers)
_COLOURS = {
    Severity.INFO: 0x3498DB,  # blue
    Severity.WARNING: 0xF39C12,  # orange
    Severity.CRITICAL: 0xE74C3C,  # red
}


@dataclass
class Alert:
    title: str
    message: str
    severity: Severity = Severity.INFO
    context: dict | None = None
    component: str = "system"
    timestamp: float = field(default_factory=time.time)


class AlertTransport(Protocol):
    name: str

    async def publish(self, alert: Alert) -> bool: ...


class NullTransport:
    """Drops alerts silently. Default when no webhook is configured."""

    name = "null"

    async def publish(self, alert: Alert) -> bool:
        logger.debug(
            "NullTransport dropping alert [%s] %s", alert.severity.value, alert.title
        )
        return True


class DiscordWebhookTransport:
    """
    Post alerts to a Discord webhook.

    Discord webhook JSON shape:
        {
          "content": "optional plaintext",
          "embeds": [{"title": ..., "description": ..., "color": int, "fields": [...]}]
        }
    """

    name = "discord"

    def __init__(
        self,
        webhook_url: str,
        username: str = "prediction-market-bot",
        timeout_s: float = 5.0,
    ) -> None:
        self.webhook_url = webhook_url
        self.username = username
        self.timeout_s = timeout_s

    async def publish(self, alert: Alert) -> bool:
        payload = self._format(alert)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(self.webhook_url, json=payload)
                if 200 <= resp.status_code < 300:
                    return True
                logger.warning(
                    "Discord webhook returned %d: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
        except httpx.TimeoutException:
            logger.warning("Discord webhook timed out after %ss", self.timeout_s)
            return False
        except Exception as e:
            logger.warning("Discord webhook failed: %s", e)
            return False

    def _format(self, alert: Alert) -> dict:
        colour = _COLOURS.get(alert.severity, _COLOURS[Severity.INFO])

        fields = []
        if alert.context:
            for k, v in list(alert.context.items())[
                :10
            ]:  # Discord cap: 25, we cap at 10
                fields.append(
                    {
                        "name": str(k)[:256],
                        "value": f"`{str(v)[:1000]}`",
                        "inline": True,
                    }
                )

        # Severity gets a leading emoji so the channel is scannable.
        emoji = {
            Severity.INFO: "ℹ️",
            Severity.WARNING: "⚠️",
            Severity.CRITICAL: "🚨",
        }.get(alert.severity, "")

        embed = {
            "title": f"{emoji} {alert.title}"[:256],
            "description": alert.message[:2000],
            "color": colour,
            "fields": fields,
            "footer": {"text": f"{alert.component} • {alert.severity.value}"},
            "timestamp": _iso(alert.timestamp),
        }

        content = "@here" if alert.severity == Severity.CRITICAL else None
        payload: dict = {"username": self.username, "embeds": [embed]}
        if content:
            payload["content"] = content
        return payload


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class AlertManager:
    """
    Central alert dispatcher with dedup and multi-transport fanout.

    All methods are safe to call from both sync and async contexts — synchronous
    callers should use ``send_nowait`` which schedules the publish on a
    background task (returns immediately).
    """

    def __init__(
        self,
        transports: list[AlertTransport],
        dedup_window_s: float = 300.0,
    ) -> None:
        self.transports = transports
        self.dedup_window_s = dedup_window_s
        self._recent: dict[str, float] = {}

    def _dedup_key(self, alert: Alert) -> str:
        # Hash of title+severity+component — body can vary (e.g. timestamps)
        # without re-alerting, but a new component/title gets through.
        return hashlib.sha1(
            f"{alert.component}|{alert.severity.value}|{alert.title}".encode()
        ).hexdigest()

    def _is_duplicate(self, alert: Alert) -> bool:
        now = time.time()
        key = self._dedup_key(alert)

        # Opportunistic cleanup of old entries
        cutoff = now - self.dedup_window_s
        self._recent = {k: t for k, t in self._recent.items() if t > cutoff}

        last = self._recent.get(key)
        if last is not None and (now - last) < self.dedup_window_s:
            return True
        self._recent[key] = now
        return False

    async def send(
        self,
        title: str,
        message: str,
        severity: Severity | str = Severity.INFO,
        context: dict | None = None,
        component: str = "system",
    ) -> bool:
        """Publish an alert to all configured transports. Awaits completion."""
        if isinstance(severity, str):
            severity = Severity(severity)

        alert = Alert(
            title=title,
            message=message,
            severity=severity,
            context=context,
            component=component,
        )

        if self._is_duplicate(alert):
            logger.debug(
                "Dedup suppressed alert [%s] %s", alert.severity.value, alert.title
            )
            return True

        logger.info(
            "ALERT [%s] %s: %s",
            alert.severity.value.upper(),
            alert.title,
            alert.message,
        )

        results = await asyncio.gather(
            *[t.publish(alert) for t in self.transports],
            return_exceptions=True,
        )

        # Success if at least one transport delivered. NullTransport always
        # returns True, so the default no-op configuration is always "success".
        ok = any(r is True for r in results)
        if not ok:
            logger.warning("All alert transports failed for: %s", alert.title)
        return ok

    def send_nowait(
        self,
        title: str,
        message: str,
        severity: Severity | str = Severity.INFO,
        context: dict | None = None,
        component: str = "system",
    ) -> None:
        """
        Fire-and-forget variant — schedules the send on the running loop and
        returns immediately. Safe to call from synchronous code paths that
        can't await (e.g. signal handlers, risk check rejections).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop running — just log and drop. Callers that need guaranteed
            # delivery should use await send() instead.
            logger.debug("send_nowait called outside running loop: %s", title)
            return

        loop.create_task(
            self.send(
                title=title,
                message=message,
                severity=severity,
                context=context,
                component=component,
            )
        )


# ── Module-level factory ──────────────────────────────────────────────

_manager: AlertManager | None = None


def get_alert_manager() -> AlertManager:
    """Return the process-wide AlertManager, creating it on first use."""
    global _manager
    if _manager is None:
        _manager = _build_default_manager()
    return _manager


def set_alert_manager(mgr: AlertManager | None) -> None:
    """Override the global manager — primarily for tests."""
    global _manager
    _manager = mgr


def _build_default_manager() -> AlertManager:
    transports: list[AlertTransport] = []

    discord_url = os.getenv("ALERT_DISCORD_WEBHOOK_URL", "").strip()
    if discord_url:
        transports.append(
            DiscordWebhookTransport(
                webhook_url=discord_url,
                username=os.getenv("ALERT_BOT_USERNAME", "prediction-market-bot"),
                timeout_s=float(os.getenv("ALERT_TIMEOUT_S", "5")),
            )
        )
        logger.info("Alerting enabled: Discord webhook")

    # Slack uses the same webhook shape — Discord payloads render fine there
    # as long as you use the "content" field. Enable both if you want cross-post.
    slack_url = os.getenv("ALERT_SLACK_WEBHOOK_URL", "").strip()
    if slack_url:
        transports.append(
            DiscordWebhookTransport(  # same shape
                webhook_url=slack_url,
                username=os.getenv("ALERT_BOT_USERNAME", "prediction-market-bot"),
                timeout_s=float(os.getenv("ALERT_TIMEOUT_S", "5")),
            )
        )
        logger.info("Alerting enabled: Slack webhook")

    if not transports:
        transports.append(NullTransport())
        logger.info("Alerting disabled (no webhook configured, using NullTransport)")

    dedup_window = float(os.getenv("ALERT_DEDUP_WINDOW_S", "300"))
    return AlertManager(transports=transports, dedup_window_s=dedup_window)
