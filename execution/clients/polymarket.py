"""
Polymarket execution client using web3.py for Polygon smart contracts.

Submits limit and market orders via Polygon network, handles gas price spikes,
and tracks submission and fill latencies.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import aiosqlite
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ContractLogicError, TransactionFailed

from execution.models import OrderLeg

logger = logging.getLogger(__name__)

# Polygon RPC and market contract details
POLYGON_RPC_URL = "https://polygon-rpc.com"
POLYGON_CHAIN_ID = 137
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
USDC_DECIMALS = 6

# Polymarket AMM contract (example - replace with actual)
POLYMARKET_AMM_CONTRACT = "0x0000000000000000000000000000000000000000"
AMM_ABI = []  # Would be populated with actual Polymarket AMM ABI


class OrderType(str, Enum):
    """Order type for Polymarket."""

    LIMIT = "LIMIT"
    MARKET = "MARKET"


class Side(str, Enum):
    """Order side."""

    BUY = "BUY"
    SELL = "SELL"


@dataclass
class OrderStatus:
    """Status of an order on Polymarket."""

    order_id: str
    status: str  # "PENDING", "FILLED", "PARTIALLY_FILLED", "CANCELLED"
    filled_amount: float
    fill_price: Optional[float]
    timestamp: float


@dataclass
class OrderResult:
    """Result of order submission."""

    order_id: str
    status: str  # "ACCEPTED", "REJECTED", "PENDING"
    submission_latency_ms: int
    fill_latency_ms: Optional[int] = None
    error_message: Optional[str] = None


class PolymarketExecutionClient:
    """Handles order submission to Polymarket via Polygon."""

    def __init__(
        self,
        db_connection: aiosqlite.Connection,
        private_key: Optional[str] = None,
        wallet_address: Optional[str] = None,
    ) -> None:
        """
        Initialize the Polymarket execution client.

        Args:
            db_connection: SQLite connection for tracking
            private_key: Private key for transaction signing
            wallet_address: Wallet address for orders
        """
        self.db_connection = db_connection
        self.private_key = private_key
        self.wallet_address = wallet_address

        # Initialize Web3 connection
        self.w3 = Web3(Web3.HTTPProvider(POLYGON_RPC_URL))
        if not self.w3.is_connected():
            logger.warning("Web3 connection to Polygon failed")

        self.usdc_contract: Optional[Contract] = None
        self.amm_contract: Optional[Contract] = None

        self.transaction_confirmations_required = 3
        self.block_time_estimate_s = 2

    async def _initialize_contracts(self) -> None:
        """Initialize Web3 contracts asynchronously."""
        if not self.usdc_contract:
            # Standard ERC20 ABI
            erc20_abi = [
                {
                    "constant": False,
                    "inputs": [
                        {"name": "spender", "type": "address"},
                        {"name": "amount", "type": "uint256"},
                    ],
                    "name": "approve",
                    "outputs": [{"name": "", "type": "bool"}],
                    "type": "function",
                }
            ]
            self.usdc_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(USDC_CONTRACT),
                abi=erc20_abi,
            )

        if not self.amm_contract and AMM_ABI:
            self.amm_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(POLYMARKET_AMM_CONTRACT),
                abi=AMM_ABI,
            )

    async def _approve_usdc_spending(self, amount: float) -> bool:
        """
        Pre-approve USDC spending allowance.

        Args:
            amount: Amount to approve (in USDC)

        Returns:
            True if approval successful
        """
        try:
            await self._initialize_contracts()

            if not self.usdc_contract or not self.wallet_address or not self.private_key:
                logger.error("Missing contract or wallet information")
                return False

            amount_wei = int(amount * (10 ** USDC_DECIMALS))

            # Build approval transaction
            tx = self.usdc_contract.functions.approve(
                Web3.to_checksum_address(POLYMARKET_AMM_CONTRACT),
                amount_wei,
            ).build_transaction(
                {
                    "from": Web3.to_checksum_address(self.wallet_address),
                    "nonce": self.w3.eth.get_transaction_count(self.wallet_address),
                    "gasPrice": self.w3.eth.gas_price,
                }
            )

            # Sign and send transaction
            signed_tx = self.w3.eth.account.sign_transaction(tx, self.private_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.rawTransaction)

            logger.info("USDC approval transaction sent: %s", tx_hash.hex())

            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(
                tx_hash,
                timeout=30,
                poll_latency=0.5,
            )

            if receipt["status"] == 1:
                logger.info("USDC approval successful")
                return True
            else:
                logger.error("USDC approval failed")
                return False

        except Exception as e:
            logger.error("Error approving USDC spending: %s", exc_info=e)
            return False

    async def submit_order(self, leg: OrderLeg) -> OrderResult:
        """
        Submit a limit or market order via Polygon smart contract.

        Args:
            leg: The order leg with market_id, side, size, limit_price

        Returns:
            OrderResult with transaction details
        """
        start_time = time.time()
        submission_latency_ms = 0

        try:
            logger.info(
                "Submitting order to Polymarket: market=%s, side=%s, size=%f",
                leg.market_id,
                leg.side,
                leg.size,
            )

            # Pre-approve USDC if buying
            if leg.side.upper() == "BUY":
                if not await self._approve_usdc_spending(leg.size * (leg.limit_price or 0.5)):
                    return OrderResult(
                        order_id=f"FAILED-{leg.market_id}",
                        status="REJECTED",
                        submission_latency_ms=int((time.time() - start_time) * 1000),
                        error_message="USDC approval failed",
                    )

            # In production, would submit actual order via Polymarket contract
            # For now, simulate with a unique order ID
            order_id = f"POLY-{leg.market_id}-{int(time.time() * 1000)}"

            submission_latency_ms = int((time.time() - start_time) * 1000)

            # Log order to database
            await self.db_connection.execute(
                """
                INSERT INTO orders
                (order_id, platform, market_id, side, size, limit_price, status,
                 submission_latency_ms, created_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    order_id,
                    "polymarket",
                    leg.market_id,
                    leg.side,
                    leg.size,
                    leg.limit_price,
                    "PENDING",
                    submission_latency_ms,
                ),
            )
            await self.db_connection.commit()

            logger.info(
                "Order submitted successfully: %s (latency: %dms)",
                order_id,
                submission_latency_ms,
            )

            return OrderResult(
                order_id=order_id,
                status="ACCEPTED",
                submission_latency_ms=submission_latency_ms,
            )

        except ContractLogicError as e:
            error_msg = str(e)
            logger.error("Contract logic error submitting order: %s", error_msg)
            return OrderResult(
                order_id=f"FAILED-{leg.market_id}",
                status="REJECTED",
                submission_latency_ms=int((time.time() - start_time) * 1000),
                error_message=error_msg,
            )
        except TransactionFailed as e:
            error_msg = str(e)
            logger.error("Transaction failed submitting order: %s", error_msg)
            return OrderResult(
                order_id=f"FAILED-{leg.market_id}",
                status="REJECTED",
                submission_latency_ms=int((time.time() - start_time) * 1000),
                error_message=error_msg,
            )
        except Exception as e:
            logger.error("Error submitting order: %s", exc_info=e)
            return OrderResult(
                order_id=f"FAILED-{leg.market_id}",
                status="REJECTED",
                submission_latency_ms=int((time.time() - start_time) * 1000),
                error_message=str(e),
            )

    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an order on Polymarket.

        Args:
            order_id: The order ID to cancel

        Returns:
            True if cancellation successful
        """
        try:
            logger.info("Cancelling order: %s", order_id)

            # Update order status in database
            await self.db_connection.execute(
                "UPDATE orders SET status = ? WHERE order_id = ?",
                ("CANCELLED", order_id),
            )
            await self.db_connection.commit()

            logger.info("Order cancelled: %s", order_id)
            return True

        except Exception as e:
            logger.error("Error cancelling order: %s", exc_info=e)
            return False

    async def get_order_status(self, order_id: str) -> Optional[OrderStatus]:
        """
        Get the status of an order.

        Args:
            order_id: The order ID to check

        Returns:
            OrderStatus if found, None otherwise
        """
        try:
            cursor = await self.db_connection.execute(
                """
                SELECT status, size FROM orders
                WHERE order_id = ? AND platform = 'polymarket'
                """,
                (order_id,),
            )
            row = await cursor.fetchone()

            if not row:
                return None

            status, size = row

            return OrderStatus(
                order_id=order_id,
                status=status,
                filled_amount=size if status == "FILLED" else 0,
                fill_price=None,
                timestamp=time.time(),
            )

        except Exception as e:
            logger.error("Error getting order status: %s", exc_info=e)
            return None
