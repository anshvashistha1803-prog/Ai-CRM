"""
Pydantic schemas for Properties API.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.core.schemas.base import BaseSchema, PaginatedResponse


class PropertyCreateRequest(BaseSchema):
    """Payload to create a new property listing."""

    title: str = Field(min_length=3, max_length=255)
    property_type: str = Field(default="APARTMENT", max_length=50)  # APARTMENT, VILLA, PLOT, COMMERCIAL
    bhk: int | None = Field(default=None, ge=1, le=20)
    price: int = Field(gt=0, description="Price in smallest currency unit")
    currency_code: str = Field(default="INR", max_length=3)
    location_text: str = Field(min_length=2, max_length=255)
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    area_sqft: int | None = Field(default=None, ge=1)
    description: str | None = None
    amenities: list[str] = Field(default_factory=list)
    status: str = Field(default="AVAILABLE", max_length=30)  # AVAILABLE, SOLD, RENTED, UNAVAILABLE, DRAFT
    photos: list[str] = Field(default_factory=list)


class PropertyUpdateRequest(BaseSchema):
    """Payload to update an existing property listing."""

    title: str | None = Field(default=None, min_length=3, max_length=255)
    property_type: str | None = Field(default=None, max_length=50)
    bhk: int | None = Field(default=None, ge=1, le=20)
    price: int | None = Field(default=None, gt=0)
    currency_code: str | None = Field(default=None, max_length=3)
    location_text: str | None = Field(default=None, min_length=2, max_length=255)
    latitude: float | None = Field(default=None, ge=-90.0, le=90.0)
    longitude: float | None = Field(default=None, ge=-180.0, le=180.0)
    area_sqft: int | None = Field(default=None, ge=1)
    description: str | None = None
    amenities: list[str] | None = None
    status: str | None = Field(default=None, max_length=30)
    photos: list[str] | None = None


class PropertyResponse(BaseSchema):
    """Public property listing details."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    title: str
    property_type: str
    bhk: int | None = None
    price: int
    currency_code: str
    location_text: str
    latitude: float | None = None
    longitude: float | None = None
    area_sqft: int | None = None
    description: str | None = None
    amenities: list[Any] = Field(default_factory=list)
    status: str
    added_by: uuid.UUID | None = None
    photos: list[Any] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PropertyListResponse(PaginatedResponse[PropertyResponse]):
    """Paginated properties list."""
    pass
