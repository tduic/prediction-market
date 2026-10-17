"""
Shared data models for the execution service.

Extracted to break circular imports between handler, router, and platform clients.
"""

from pydantic import BaseModel, Field, field_validator

from execution.enums import Side


class OrderLeg(BaseModel):
    """Schema for a single order leg."""

    market_id: str
    platform: str  # "polymarket" or "kalshi"
    side: Side
    size: float = Field(gt=0)
    limit_price: float | None = Field(None, ge=0, le=1)
    order_type: str = "LIMIT"  # "LIMIT" or "MARKET"

    @field_validator("platform")
    @classmethod
    def validate_platform(cls, v: str) -> str:
        if v.lower() not in ("polymarket", "kalshi"):
            raise ValueError("platform must be 'polymarket' or 'kalshi'")
        return v.lower()

    @field_validator("side", mode="before")
    @classmethod
    def validate_side(cls, v) -> Side:
        if isinstance(v, Side):
            return v
        if isinstance(v, str) and v.upper() in ("BUY", "SELL"):
            return Side(v.upper())
        raise ValueError("side must be 'BUY' or 'SELL'")

    @field_validator("order_type")
    @classmethod
    def validate_order_type(cls, v: str) -> str:
        if v.upper() not in ("LIMIT", "MARKET"):
            raise ValueError("order_type must be 'LIMIT' or 'MARKET'")
        return v.upper()
