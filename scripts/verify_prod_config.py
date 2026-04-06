"""
Production configuration smoke test.

Run this on the GCE VM (or anywhere with prod env vars exported) to verify
that every moving piece of the secrets + alerting stack is wired correctly
BEFORE flipping the service to live mode.

What it checks:

  1. Secrets backend
     - Reports which backend is active (env or gcp).
     - For each required secret, fetches the value and reports whether it
       came from GCP Secret Manager or fell through to an environment
       variable. Actual values are masked to the first and last 4 chars so
       this is safe to paste into a terminal log.

  2. Alerting
     - Reports which alert transports are configured.
     - Sends one INFO-severity test alert through the AlertManager so you
       can visually confirm the Discord (or Slack) channel receives it.

Exit codes:
  0  everything OK
  1  at least one required secret missing
  2  alert transport failed
  3  alert transports silently dropped (NullTransport — webhook not set)

Usage:
    python scripts/verify_prod_config.py
    python scripts/verify_prod_config.py --skip-alert      # config only
    python scripts/verify_prod_config.py --require-gcp     # fail if env fallback
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Ensure project root importable when running from scripts/
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.alerting import (  # noqa: E402
    DiscordWebhookTransport,
    NullTransport,
    Severity,
    get_alert_manager,
)
from core.secrets import (  # noqa: E402
    GCPSecretManagerBackend,
    get_backend,
    get_secret,
)

# All secrets the system expects in a live-mode deployment.
REQUIRED_SECRETS = [
    "POLYMARKET_PRIVATE_KEY",
    "POLYMARKET_WALLET_ADDRESS",
    "KALSHI_API_KEY",
    "KALSHI_RSA_KEY_PATH",
]


def _mask(value: str | None) -> str:
    if not value:
        return "<MISSING>"
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:4]}…{value[-4:]} ({len(value)} chars)"


def _check_secrets(require_gcp: bool) -> tuple[bool, list[tuple[str, str, str]]]:
    """
    Returns (all_ok, rows) where rows is a list of (name, source, masked).

    Source is determined by asking the GCP backend's cache whether the value
    came from its internal store or fell through to os.getenv.
    """
    backend = get_backend()
    rows: list[tuple[str, str, str]] = []
    all_ok = True

    for name in REQUIRED_SECRETS:
        value = get_secret(name)

        # Determine source
        if isinstance(backend, GCPSecretManagerBackend):
            if name in backend._cache:
                source = "gcp"
            elif name in backend._unavailable:
                source = "env-fallback"
            else:
                # Wasn't looked up via GCP at all (no project id, no client)
                source = "env-fallback"
        else:
            source = "env"

        rows.append((name, source, _mask(value)))

        if not value:
            all_ok = False
        if require_gcp and source != "gcp":
            all_ok = False

    return all_ok, rows


async def _check_alerting(skip_alert: bool) -> int:
    """Report transport config and optionally send a test alert."""
    mgr = get_alert_manager()

    print()
    print("── Alerting ───────────────────────────────────────────────")
    transport_names = [t.name for t in mgr.transports]
    print(f"  Transports: {', '.join(transport_names) or '(none)'}")
    print(f"  Dedup window: {mgr.dedup_window_s}s")

    # Warn if only NullTransport is configured — the service will run but
    # will never notify anyone of a trip or halt.
    only_null = all(isinstance(t, NullTransport) for t in mgr.transports)
    if only_null:
        print("  ⚠ Only NullTransport configured — alerts will be dropped.")
        print("    Set ALERT_DISCORD_WEBHOOK_URL in the environment.")

    for t in mgr.transports:
        if isinstance(t, DiscordWebhookTransport):
            # Don't print the full URL — the token portion is sensitive.
            url = t.webhook_url
            masked = f"{url[:32]}…{url[-6:]}" if len(url) > 40 else url
            print(f"  Discord webhook: {masked}")
            print(f"  Username: {t.username}")
            print(f"  Timeout: {t.timeout_s}s")

    if skip_alert:
        print("  (skipping test alert — --skip-alert)")
        return 3 if only_null else 0

    print()
    print("  → Sending test alert…")

    # Use a unique title so dedup doesn't swallow repeat runs of this script.
    import time

    title = f"Prod config smoke test [{int(time.time())}]"
    ok = await mgr.send(
        title=title,
        message=(
            "This is a test message from verify_prod_config.py. "
            "If you're seeing this in your alert channel, the wiring works."
        ),
        severity=Severity.INFO,
        context={
            "script": "verify_prod_config.py",
            "component": "smoke_test",
        },
        component="smoke_test",
    )

    if ok:
        if only_null:
            print("  ⚠ AlertManager returned OK but only NullTransport is wired.")
            print("    No alert was actually sent. Check your channel.")
            return 3
        print("  ✓ Test alert dispatched. Check your Discord channel.")
        return 0
    else:
        print("  ✗ Test alert FAILED. Webhook rejected the request.")
        return 2


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-alert",
        action="store_true",
        help="Check config only, don't send a test alert",
    )
    parser.add_argument(
        "--require-gcp",
        action="store_true",
        help="Fail if any secret fell through to env vars (strict prod mode)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("══════════════════════════════════════════════════════════")
    print("  Production Configuration Smoke Test")
    print("══════════════════════════════════════════════════════════")

    # ── Secrets ──
    print()
    print("── Secrets backend ────────────────────────────────────────")
    backend = get_backend()
    print(f"  Active backend: {backend.name}")
    if isinstance(backend, GCPSecretManagerBackend):
        print(f"  GCP project: {backend.project_id or '(not set)'}")
        if not backend.project_id:
            print("  ⚠ GCP_PROJECT_ID is empty — all lookups will fall through to env")

    print()
    print("── Required secrets ───────────────────────────────────────")
    secrets_ok, rows = _check_secrets(require_gcp=args.require_gcp)

    name_w = max(len(r[0]) for r in rows)
    source_w = max(len(r[1]) for r in rows)
    for name, source, masked in rows:
        marker = "✓" if "<MISSING>" not in masked else "✗"
        print(f"  {marker} {name.ljust(name_w)}  [{source.ljust(source_w)}]  {masked}")

    if not secrets_ok:
        print()
        if args.require_gcp:
            print("  ✗ One or more secrets missing or not from GCP (--require-gcp)")
        else:
            print("  ✗ One or more required secrets are missing")

    # ── Alerting ──
    alert_exit = await _check_alerting(skip_alert=args.skip_alert)

    # ── Summary ──
    print()
    print("══════════════════════════════════════════════════════════")

    if not secrets_ok:
        print("  RESULT: FAIL (secrets)")
        print("══════════════════════════════════════════════════════════")
        return 1

    if alert_exit != 0:
        print(f"  RESULT: FAIL (alerting, exit={alert_exit})")
        print("══════════════════════════════════════════════════════════")
        return alert_exit

    print("  RESULT: OK — config looks good for production")
    print("══════════════════════════════════════════════════════════")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
