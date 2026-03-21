"""
Shared data models for the execution service.

Extracted to break circular imports between handler, router, and platform clients.
"""

from pydantic import BaseModel, Field, validator


class OrderLeg(BaseModel):
    """Schema for a single order leg."""

    market_id: str
    platform: str  # "polymarket" or "kalshi"
    side: str  # "BUY" or "SELL"
    size: float = Field(gt=0)
    limit_price: float | None = Field(None, ge=0, le=1)
    order_type: str = "LIMIT"  # "LIMIT" or "MARKET"

    @validator("platform")
    def validate_platform(cls, v: str) -> str:
        if v.lower() not in ("polymarket", "kalshi"):
            raise ValueError("platform must be 'polymarket' or 'kalshi'")
        return v.lower()

    @validator("side")
    def validate_side(cls, v: str) -> str:
        if v.upper() not in ("BUY", "SELL"):
            raise ValueError("side must be 'BUY' or 'SELL'")
        return v.upper()

    @validator("order_type")
    def validate_order_type(cls, v: str) -> str:
        if v.upper() not in ("LIMIT", "MARKET"):
            raise ValueError("order_type must be 'LIMIT' or 'MARKET'")
        return v.upper()


class TradingSignal(BaseModel):
    """Schema for trading signals from core."""

    signal_id: str
    schema_version: str
    generated_at_utc: str  # ISO format timestamp
    expires_at_utc: str  # ISO format timestamp
    legs: list[OrderLeg]
    execution_mode: str = "simultaneous"  # "simultaneous" or "sequential"
    abort_on_partial: bool = False
    expiry_s: int = Field(default=300, gt=0)  # Seconds to keep position open

    @validator("schema_version")
    def validate_schema_version(cls, v: str) -> str:
        if v != "1.0":
            raise ValueError("schema_version must be '1.0'")
        return v

    @validator("execution_mode")
    def validate_execution_mode(cls, v: str) -> str:
        if v.lower() not in ("simultaneous", "sequential"):
            raise ValueError("execution_mode must be 'simultaneous' or 'sequential'")
        return v.lower()
