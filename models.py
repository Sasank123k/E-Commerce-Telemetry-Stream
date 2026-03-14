"""
models.py — Pydantic data models for the E-Commerce Telemetry Stream.

Defines the schema for e-commerce transaction events that flow
through the async queue and get aggregated by consumer workers.
"""

from pydantic import BaseModel, Field
from uuid import UUID, uuid4
from datetime import datetime


class TransactionEvent(BaseModel):
    """A single e-commerce transaction event."""

    event_id: UUID = Field(default_factory=uuid4, description="Unique event identifier")
    timestamp: float = Field(
        default_factory=lambda: datetime.now().timestamp(),
        description="Unix epoch timestamp of the event",
    )
    user_id: str = Field(..., description="ID of the user who triggered the event")
    product_id: str = Field(..., description="ID of the purchased product")
    category: str = Field(..., description="Product category (e.g. Electronics, Clothing)")
    region: str = Field(..., description="Geographic region of the transaction")
    price: float = Field(..., gt=0, description="Unit price of the product")
    quantity: int = Field(..., gt=0, description="Number of units purchased")

    class Config:
        json_schema_extra = {
            "example": {
                "event_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "timestamp": 1710441588.123,
                "user_id": "user_4821",
                "product_id": "prod_1037",
                "category": "Electronics",
                "region": "North America",
                "price": 29.99,
                "quantity": 2,
            }
        }
