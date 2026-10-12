"""
Matching Pydantic schemas — request/response validation for lead-property matches.
"""

import uuid
from typing import Literal

from pydantic import Field

from app.core.schemas.base import BaseSchema, TimestampSchema, UUIDSchema


class PropertyPreview(UUIDSchema):
    title: str
    property_type: str
    bhk: int | None = None
    price: int
    currency_code: str
    location_text: str
    status: str
    photos: list[str] = Field(default_factory=list)


class LeadPropertyMatchResponse(UUIDSchema, TimestampSchema):
    tenant_id: uuid.UUID
    lead_id: uuid.UUID
    property_id: uuid.UUID
    match_score: float
    match_reasons: dict = Field(default_factory=dict)
    status: str
    property: PropertyPreview | None = None


class MatchStatusUpdateRequest(BaseSchema):
    status: Literal["SUGGESTED", "SHORTLISTED", "REJECTED", "VISITED"]
