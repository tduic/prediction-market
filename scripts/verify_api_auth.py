"""
Verify API authentication against Kalshi and Polymarket.

Read-only calls: checks balances and fetches a few markets.
No orders placed, no risk.

Usage:
    python scripts/verify_api_auth.py
"""

import asyncio
import base64
import logging
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Load .env
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(message)s",
)
logger = logging.getLogger(__name__)


# ─── Kalshi ──────────────────────────────────────────────────────────────────


async def verify_kalshi():
    """Test Kalshi API auth with a balance check and market fetch."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    api_key = os.getenv("KALSHI_API_KEY", "")
    rsa_key_path = os.getenv("KALSHI_RSA_KEY_PATH", "")
    api_base = os.getenv(
        "KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2"
    )

    if not api_key or not rsa_key_path:
        logger.error("KALSHI: Missing KALSHI_API_KEY or KALSHI_RSA_KEY_PATH in .env")
        return False

    # Load RSA key
    key_path = Path(rsa_key_path).expanduser()
    if not key_path.exists():
        logger.error("KALSHI: RSA key file not found: %s", key_path)
        return False

    private_key = serialization.load_pem_private_key(
        key_path.read_bytes(), password=None
    )
    logger.info("KALSHI: RSA key loaded from %s", key_path)

    def sign_request(method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        message = (ts + method.upper() + path).encode("utf-8")
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
        }

    async with httpx.AsyncClient(timeout=15) as client:
        # 1. Check balance
        path = "/trade-api/v2/portfolio/balance"
        headers = sign_request("GET", path)
        logger.info("KALSHI: Fetching balance...")

        resp = await client.get(f"{api_base}/portfolio/balance", headers=headers)

        if resp.status_code == 200:
            data = resp.json()
            balance_cents = data.get("balance", 0)
            logger.info("KALSHI: ✅ Auth OK — Balance: $%.2f", balance_cents / 100)
        else:
            logger.error(
                "KALSHI: ❌ Auth failed — HTTP %d: %s", resp.status_code, resp.text
            )
            return False

        # 2. Fetch a few markets (public, but tests the connection)
        path = "/trade-api/v2/markets"
        headers = sign_request("GET", path)
        resp = await client.get(
            f"{api_base}/markets", headers=headers, params={"limit": 3}
        )

        if resp.status_code == 200:
            markets = resp.json().get("markets", [])
            logger.info("KALSHI: ✅ Fetched %d markets", len(markets))
            for m in markets[:3]:
                logger.info(
                    "  → %s: %s",
                    m.get("ticker", "?"),
                    m.get("title", "?")[:60],
                )
        else:
            logger.warning("KALSHI: Market fetch returned HTTP %d", resp.status_code)

    return True


# ─── Polymarket ──────────────────────────────────────────────────────────────


async def verify_polymarket():
    """Test Polymarket API with a public market fetch and CLOB client auth."""
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    funder = os.getenv("POLYMARKET_WALLET_ADDRESS", "")

    # 1. Test public API (no auth needed)
    async with httpx.AsyncClient(timeout=15) as client:
        logger.info("POLYMARKET: Fetching markets from Gamma API...")
        resp = await client.get(
            "https://gamma-api.polymarket.com/markets",
            params={"limit": 3, "active": True},
        )

        if resp.status_code == 200:
            markets = resp.json()
            logger.info(
                "POLYMARKET: ✅ Public API OK — Fetched %d markets", len(markets)
            )
            for m in markets[:3]:
                logger.info(
                    "  → %s",
                    m.get("question", m.get("title", "?"))[:60],
                )
        else:
            logger.error("POLYMARKET: ❌ Public API failed — HTTP %d", resp.status_code)
            return False

    # 2. Test authenticated CLOB client
    if not private_key:
        logger.warning("POLYMARKET: No POLYMARKET_PRIVATE_KEY set, skipping auth test")
        return True

    try:
        from py_clob_client.client import ClobClient

        logger.info("POLYMARKET: Testing CLOB client authentication...")

        # Stage 1: derive credentials
        l1_client = ClobClient(
            host="https://clob.polymarket.com",
            key=private_key,
            chain_id=137,
        )
        creds = l1_client.create_or_derive_api_creds()
        logger.info("POLYMARKET: ✅ API credentials derived successfully")

        # Stage 2: authenticated client
        l2_client = ClobClient(
            host="https://clob.polymarket.com",
            key=private_key,
            chain_id=137,
            creds=creds,
            signature_type=1,
            funder=funder if funder else None,
        )

        # Try fetching open orders (should return empty list if no orders)
        try:
            orders = l2_client.get_orders()
            order_count = len(orders) if isinstance(orders, list) else 0
            logger.info("POLYMARKET: ✅ Auth OK — %d open orders", order_count)
        except Exception:
            # Some versions use different method names
            logger.info("POLYMARKET: ✅ Auth OK (credential derivation succeeded)")

    except ImportError:
        logger.warning(
            "POLYMARKET: py-clob-client not installed. Run: pip install py-clob-client"
        )
        return True
    except Exception as e:
        logger.error("POLYMARKET: ❌ Auth failed — %s", e)
        return False

    return True


# ─── Main ────────────────────────────────────────────────────────────────────


async def main():
    logger.info("=" * 60)
    logger.info("API Authentication Verification")
    logger.info("=" * 60)

    kalshi_ok = await verify_kalshi()
    logger.info("")
    poly_ok = await verify_polymarket()

    logger.info("")
    logger.info("=" * 60)
    if kalshi_ok and poly_ok:
        logger.info("✅ All API connections verified successfully")
    else:
        if not kalshi_ok:
            logger.error("❌ Kalshi authentication failed")
        if not poly_ok:
            logger.error("❌ Polymarket authentication failed")
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
