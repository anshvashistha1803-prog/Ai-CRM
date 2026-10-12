"""
SiteVisit Pydantic schemas — request/response validation for booking, rescheduling, and status transitions.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.core.schemas.base import BaseSchema, TimestampSchema, UUIDSchema


class SiteVisitCreateRequest(BaseSchema):
    lead_id: uuid.UUID
    property_id: uuid.UUID
    assigned_agent_id: uuid.UUID | None = None
    scheduled_start_at: datetime
    scheduled_end_at: datetime
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("scheduled_end_at")
    @classmethod
    def validate_duration(cls, end: datetime, info) -> datetime:
        start = info.data.get("scheduled_start_at")
        if start and end <= start:
            raise ValueError("scheduled_end_at must be strictly after scheduled_start_at")
        return end


class SiteVisitRescheduleRequest(BaseSchema):
    new_start_at: datetime
    new_end_at: datetime
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("new_end_at")
    @classmethod
    def validate_duration(cls, end: datetime, info) -> datetime:
        start = info.data.get("new_start_at")
        if start and end <= start:
            raise ValueError("new_end_at must be strictly after new_start_at")
        return end


class SiteVisitCancelRequest(BaseSchema):
    cancellation_reason: str = Field(min_length=2, max_length=500)


class SiteVisitResponse(UUIDSchema, TimestampSchema):
    tenant_id: uuid.UUID
    lead_id: uuid.UUID
    property_id: uuid.UUID
    assigned_agent_id: uuid.UUID | None = None
    scheduled_start_at: datetime
    scheduled_end_at: datetime
    status: Literal["SCHEDULED", "CONFIRMED", "COMPLETED", "CANCELLED", "RESCHEDULED", "NO_SHOW"]
    cancellation_reason: str | None = None
    rescheduled_from_id: uuid.UUID | None = None
    notes: str | None = None
    client_confirmed: bool = False
    broker_confirmed: bool = False
