"""
Shared Pydantic schemas: pagination, error responses, and common types.
"""

import uuid
from datetime import datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class APIResponse(BaseModel, Generic[T]):
    """Standard success envelope for all API responses."""

    success: bool = True
    data: T


class PaginatedResponse(BaseModel, Generic[T]):
    """Standard paginated list response."""

    success: bool = True
    data: list[T]
    total: int
    page: int
    page_size: int
    total_pages: int


class PaginationParams(BaseModel):
    """Standard pagination query parameters."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=100)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class ErrorResponse(BaseModel):
    """Standard error response body."""

    success: bool = False
    error: str
    message: str
    detail: str | None = None


class BaseSchema(BaseModel):
    """Base schema with shared config for all Pydantic models."""

    model_config = ConfigDict(
        from_attributes=True,   # enable ORM mode
        populate_by_name=True,  # allow field aliases
        str_strip_whitespace=True,
    )


class TimestampSchema(BaseSchema):
    """Adds read-only timestamp fields to response schemas."""

    created_at: datetime
    updated_at: datetime


class UUIDSchema(BaseSchema):
    """Adds read-only UUID id field."""

    id: uuid.UUID
