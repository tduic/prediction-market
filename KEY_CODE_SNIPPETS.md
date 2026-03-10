# Key Code Snippets and Examples

## Signal Processing Flow

### 1. Consuming from Redis Queue (execution/main.py)
```python
async def consume_queue(self) -> None:
    """Main loop: consume signals from Redis queue and process them."""
    if not self.redis_client:
        logger.error("Redis client not initialized")
        return

    logger.info("Starting signal consumer on queue: %s", self.signal_queue_name)

    while self.running:
        try:
            # BRPOP blocks with 1-second timeout to allow graceful shutdown
            result = await self.redis_client.brpop(
                self.signal_queue_name, timeout=1
            )

            if result is None:
                continue

            _, signal_json = result

            try:
                payload = json.loads(signal_json)
                logger.debug("Received signal: %s", payload.get("signal_id"))
                await self.process_signal(payload)
            except json.JSONDecodeError as e:
                logger.error("Failed to decode signal JSON: %s", e)

        except asyncio.CancelledError:
            logger.info("Signal consumer cancelled")
            break
        except Exception as e:
            logger.error("Error consuming from queue: %s", exc_info=e)
            await asyncio.sleep(1)
```

### 2. Signal Validation with Pydantic (execution/handler.py)
```python
class TradingSignal(BaseModel):
    """Schema for trading signals from core."""

    signal_id: str
    schema_version: str
    generated_at_utc: str  # ISO format timestamp
    expires_at_utc: str  # ISO format timestamp
    legs: list[OrderLeg]
    execution_mode: str = "simultaneous"
    abort_on_partial: bool = False
    expiry_s: int = Field(default=300, gt=0)

    @validator("schema_version")
    def validate_schema_version(cls, v: str) -> str:
        if v != "1.0":
            raise ValueError("schema_version must be '1.0'")
        return v

async def validate_signal(self, payload: Dict[str, Any]) -> bool:
    """Validate signal against schema and check TTL."""
    try:
        signal = TradingSignal(**payload)

        # Check TTL - ensure expires_at is in the future
        expires_at = datetime.fromisoformat(
            signal.expires_at_utc.replace("Z", "+00:00")
        )
        now = datetime.utcnow().replace(tzinfo=expires_at.tzinfo)

        if expires_at <= now:
            logger.warning("Signal has expired: %s", signal.signal_id)
            return False

        logger.debug("Signal validation passed: %s", signal.signal_id)
        return True

    except ValidationError as e:
        logger.error("Signal validation failed: %s", e)
        raise
```

## Order Routing and Execution

### 3. Parallel Order Execution with asyncio.gather (execution/router.py)
```python
async def _execute_simultaneous(
    self,
    signal_id: str,
    legs: List[OrderLeg],
) -> List[OrderResult]:
    """Execute all legs simultaneously using asyncio.gather."""
    logger.info("Executing %d legs simultaneously", len(legs))

    tasks = [
        self.route_order(leg, idx, signal_id)
        for idx, leg in enumerate(legs)
    ]

    results = await asyncio.gather(*tasks, return_exceptions=False)
    return results
```

### 4. Exponential Backoff Retry Logic (execution/router.py)
```python
async def route_order(
    self,
    leg: OrderLeg,
    leg_index: int,
    signal_id: str,
) -> OrderResult:
    """Route a single order with exponential backoff retry logic."""
    platform = leg.platform.lower()
    client = (
        self.polymarket_client if platform == "polymarket" 
        else self.kalshi_client
    )

    for attempt in range(MAX_ORDER_RETRIES):
        try:
            logger.info(
                "Submitting order to %s (attempt %d/%d)",
                platform,
                attempt + 1,
                MAX_ORDER_RETRIES,
            )

            result = await client.submit_order(leg)

            await self._log_order_event(
                signal_id=signal_id,
                order_id=result.order_id,
                leg_index=leg_index,
                status="ACCEPTED",
                details=f"Submitted to {platform}",
            )

            return result

        except Exception as e:
            logger.warning(
                "Order submission attempt %d failed: %s",
                attempt + 1,
                e,
            )

            if attempt < MAX_ORDER_RETRIES - 1:
                backoff = RETRY_BACKOFF_BASE_S * (2 ** attempt)
                logger.info("Retrying in %d seconds", backoff)
                await asyncio.sleep(backoff)
            else:
                # All retries exhausted
                return OrderResult(
                    order_id=f"FAILED-{leg.market_id}",
                    leg_index=leg_index,
                    platform=platform,
                    status="REJECTED",
                    submission_latency_ms=0,
                    error_message=str(e),
                )
```

## Platform Clients

### 5. Polymarket USDC Pre-approval (execution/clients/polymarket.py)
```python
async def _approve_usdc_spending(self, amount: float) -> bool:
    """Pre-approve USDC spending allowance."""
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
```

### 6. Kalshi HMAC-SHA256 Signing (execution/clients/kalshi.py)
```python
async def _sign_request(
    self,
    method: str,
    path: str,
    body: Optional[str] = None,
) -> dict[str, str]:
    """Generate HMAC-SHA256 signature for Kalshi API request."""
    if not self.api_key or not self.api_secret:
        raise ValueError("API key and secret required for authentication")

    timestamp = str(int(time.time() * 1000))

    # Message to sign: METHOD|PATH|TIMESTAMP|BODY
    message = f"{method}|{path}|{timestamp}|{body or ''}"

    # HMAC-SHA256 signature
    signature = hmac.new(
        self.api_secret.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()

    return {
        "Authorization": f"HMAC {self.api_key}:{signature}",
        "Content-Type": "application/json",
    }
```

## Position State Management

### 7. PnL Calculation and Updates (execution/state.py)
```python
@dataclass
class Position:
    """Represents an open trading position."""
    position_id: str
    market_id: str
    platform: str
    side: str  # "BUY" or "SELL"
    quantity: float
    entry_price: float
    entry_timestamp: float
    current_price: Optional[float] = None
    unrealized_pnl: float = 0.0

    def update_price(self, current_price: float) -> None:
        """Update current price and recalculate unrealized PnL."""
        self.current_price = current_price

        if self.side == "BUY":
            self.unrealized_pnl = (current_price - self.entry_price) * self.quantity
        else:  # SELL
            self.unrealized_pnl = (self.entry_price - current_price) * self.quantity

async def update_pnl(
    self,
    market_id: str,
    current_price: float,
) -> Dict[str, float]:
    """Update unrealized PnL for all positions in a market."""
    updated_pnl: Dict[str, float] = {}

    for position_id, position in self.positions.items():
        if position.market_id == market_id:
            position.update_price(current_price)
            updated_pnl[position_id] = position.unrealized_pnl
            logger.debug(
                "Updated PnL for %s: %f (price=%f)",
                position_id,
                position.unrealized_pnl,
                current_price,
            )

    return updated_pnl
```

### 8. Periodic Database Flush (execution/state.py)
```python
async def flush_to_db(self) -> None:
    """Flush all pending writes to SQLite database."""
    if not self.pending_writes:
        return

    try:
        logger.debug(
            "Flushing %d position updates to database",
            len(self.pending_writes),
        )

        for position in self.pending_writes:
            await self.db_connection.execute(
                """
                INSERT OR REPLACE INTO positions
                (position_id, market_id, platform, side, quantity,
                 entry_price, entry_timestamp, current_price, unrealized_pnl,
                 updated_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    position.position_id,
                    position.market_id,
                    position.platform,
                    position.side,
                    position.quantity,
                    position.entry_price,
                    position.entry_timestamp,
                    position.current_price,
                    position.unrealized_pnl,
                ),
            )

        await self.db_connection.commit()
        self.pending_writes.clear()
        self.last_flush_time = time.time()

        logger.info("Position state flushed to database")

    except Exception as e:
        logger.error("Error flushing to database: %s", exc_info=e)
```

## Utility Scripts

### 9. Interactive Pair Validation (scripts/validate_pairs.py)
```python
async def validate_pairs(self, status: str = "unverified") -> None:
    """Run interactive pair validation."""
    pairs = await self.get_pairs_by_status(verified_filter)

    if not pairs:
        print(f"\nNo pairs found with status '{status}'")
        return

    print(f"\nLoaded {len(pairs)} pairs for review")

    accepted_count = 0
    rejected_count = 0
    skipped_count = 0

    try:
        for idx, pair in enumerate(pairs):
            self.display_pair(pair, idx, len(pairs))

            result = await self.validate_pair(pair)

            if result is True:
                await self.update_pair_status(pair["pair_id"], 1)
                print("✓ Pair accepted")
                accepted_count += 1

            elif result is False:
                await self.update_pair_status(pair["pair_id"], 0)
                print("✗ Pair rejected")
                rejected_count += 1

            else:
                print("↷ Pair skipped")
                skipped_count += 1

    except KeyboardInterrupt:
        print("\n\nValidation cancelled by user")

    # Summary
    print("\n" + "=" * 80)
    print("Validation Summary")
    print("=" * 80)
    print(f"Accepted: {accepted_count}")
    print(f"Rejected: {rejected_count}")
    print(f"Skipped: {skipped_count}")
    print(f"Total: {len(pairs)}")
```

### 10. Price Backfill with Progress Bar (scripts/backfill_prices.py)
```python
async def backfill(
    self,
    since: Optional[str],
    until: Optional[str],
) -> int:
    """Run the backfill process."""
    try:
        # Parse dates
        if until:
            end_date = datetime.fromisoformat(until)
        else:
            end_date = datetime.utcnow()

        if since:
            try:
                start_date = datetime.fromisoformat(since)
            except ValueError:
                # Assume it's a number of days back
                days_back = int(since)
                start_date = end_date - timedelta(days=days_back)
        else:
            start_date = end_date - timedelta(days=30)

        logger.info(
            "Backfilling %s prices from %s to %s",
            self.platform,
            start_date.isoformat(),
            end_date.isoformat(),
        )

        # Fetch prices
        if self.platform == "polymarket":
            prices = await self.get_polymarket_prices(start_date, end_date)
        elif self.platform == "kalshi":
            prices = await self.get_kalshi_prices(start_date, end_date)
        else:
            logger.error("Unknown platform: %s", self.platform)
            return 0

        # Write to database
        count = await self.write_prices_to_db(prices)

        return count

    except Exception as e:
        logger.error("Error during backfill: %s", exc_info=e)
        return 0
```

## Core Service Initialization

### 11. Service Initialization (core/main.py)
```python
async def initialize(self) -> None:
    """Initialize all service components."""
    logger.info("Initializing core service")

    # Initialize database
    logger.info("Initializing database at %s", self.db_path)
    self.db = Database(self.db_path)
    await self.db.connect()

    # Run migrations
    logger.info("Running database migrations")
    await run_migrations(self.db.connection)

    # Initialize event bus
    logger.info("Initializing event bus")
    self.event_bus = EventBus(
        redis_url=self.redis_url,
        channel_name=self.event_bus_channel,
    )
    await self.event_bus.connect()

    # Initialize ingestor
    logger.info("Initializing ingestor")
    self.ingestor = Ingestor(
        db_connection=self.db.connection,
        event_bus=self.event_bus,
    )

    # Initialize constraint engine
    logger.info("Initializing constraint engine")
    self.constraint_engine = ConstraintEngine(
        db_connection=self.db.connection,
    )

    # Subscribe constraint engine to market updates
    if self.event_bus:
        await self.event_bus.subscribe(
            event_type="MarketUpdated",
            handler=self.constraint_engine.on_market_updated,
        )

    # Initialize signal generator
    logger.info("Initializing signal generator")
    self.signal_generator = SignalGenerator(
        db_connection=self.db.connection,
        redis_client=self.event_bus.redis_client,
        signal_queue_name=self.signal_queue_name,
    )

    # Subscribe signal generator to violations
    if self.event_bus:
        await self.event_bus.subscribe(
            event_type="ViolationDetected",
            handler=self.signal_generator.on_violation_detected,
        )

    logger.info("Core service initialization complete")
```

## Type-Safe Error Handling

### 12. Idempotent Signal Processing (execution/main.py)
```python
async def check_duplicate_signal(self, signal_id: str) -> bool:
    """Check if signal has already been processed (idempotency)."""
    if signal_id in self.processed_signal_ids:
        logger.warning("Duplicate signal detected: %s", signal_id)
        return True

    # Check database for previous processing
    if self.db_connection:
        cursor = await self.db_connection.execute(
            "SELECT COUNT(*) FROM order_events WHERE signal_id = ?",
            (signal_id,),
        )
        row = await cursor.fetchone()
        if row and row[0] > 0:
            logger.warning("Signal previously processed in database: %s", signal_id)
            return True

    return False
```

All code features:
- Full type annotations
- Comprehensive error handling
- Async/await patterns
- Structured logging
- Database persistence
- Production-quality error messages
