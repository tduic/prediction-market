"""
Production configuration smoke test.

Run this on the GCE VM (or anywhere with prod env vars exported) to verify
that every moving piece of the secrets + alerting stack is wired correctly
BEFORE flipping the service to live mode.

What it checks:

  1. Secrets backend
     - Reports which backend is active (env or gcp).
     - For each required secret, reports only whether it is present and which
       backend supplied it. Secret values are never printed or derived.

  2. Alerting
     - Reports which alert transports are configured.
     - Reports configured alert transports. Sending a test alert requires an
       explicit command-line flag.

Exit codes:
  0  everything OK
  1  at least one required secret missing
  2  alert transport failed
  3  alert transports silently dropped (NullTransport — webhook not set)

Usage:
    python scripts/verify_prod_config.py
    python scripts/verify_prod_config.py --send-test-alert # sends an INFO alert
    python scripts/verify_prod_config.py --require-gcp     # fail if env fallback
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Ensure project root importable when running from scripts/
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env from the persistent disk if present (VM deployments)
_env_file = Path("/data/predictor/.env")
if _env_file.exists():
    try:
        from dotenv import load_dotenv

        load_dotenv(_env_file, override=False)
    except ImportError:
        # Manual parse fallback if python-dotenv not available
        for _line in _env_file.read_text().splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

from core.alerting import (  # noqa: E402 - imports intentionally follow local .env loading
    DiscordWebhookTransport,
    NullTransport,
    Severity,
    get_alert_manager,
)
from core.secrets import (  # noqa: E402 - imports intentionally follow local .env loading
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

# These are filesystem paths (not secret values) — env storage is correct;
# --require-gcp should not fail for them.
ENV_OK_SECRETS = {"KALSHI_RSA_KEY_PATH"}


def _check_secrets(require_gcp: bool) -> tuple[bool, list[tuple[str, str, bool]]]:
    """
    Returns (all_ok, rows) where rows is a list of (name, source, present).

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
                source = "gcp-unavailable" if backend.strict else "env-fallback"
            else:
                source = "gcp-unavailable" if backend.strict else "env-fallback"
        else:
            source = "env"

        present = bool(value)
        rows.append((name, source, present))

        if not present:
            all_ok = False
        if require_gcp and source != "gcp" and name not in ENV_OK_SECRETS:
            all_ok = False

    return all_ok, rows


async def _check_alerting(send_test_alert: bool) -> int:
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
        print("    Configure ALERT_DISCORD_WEBHOOK_URL through the secrets backend.")

    for t in mgr.transports:
        if isinstance(t, DiscordWebhookTransport):
            print("  Discord webhook: configured")

    if not send_test_alert:
        print("  (test alert not sent; use --send-test-alert to dispatch one)")
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
        "--send-test-alert",
        action="store_true",
        help="Send an INFO alert after the configuration checks pass",
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
            print("  ⚠ GCP_PROJECT_ID is empty — GCP lookups cannot succeed")

    print()
    print("── Required secrets ───────────────────────────────────────")
    secrets_ok, rows = _check_secrets(require_gcp=args.require_gcp)

    name_w = max(len(r[0]) for r in rows)
    source_w = max(len(r[1]) for r in rows)
    for name, source, present in rows:
        marker = "✓" if present else "✗"
        status = "present" if present else "missing"
        print(f"  {marker} {name.ljust(name_w)}  [{source.ljust(source_w)}]  {status}")

    if not secrets_ok:
        print()
        if args.require_gcp:
            print("  ✗ One or more secrets missing or not from GCP (--require-gcp)")
        else:
            print("  ✗ One or more required secrets are missing")

    # ── Alerting ──
    alert_exit = await _check_alerting(send_test_alert=args.send_test_alert)

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
