"""
Qualification Pydantic schemas — request/response validation for AI proposals and review.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.schemas.base import BaseSchema, TimestampSchema, UUIDSchema


class LeadQualificationProposalResponse(UUIDSchema, TimestampSchema):
    tenant_id: uuid.UUID
    lead_id: uuid.UUID
    proposed_by: str
    status: str

    budget_min: int | None = None
    budget_max: int | None = None
    property_type: str | None = None
    bhk: int | None = None
    preferred_location: str | None = None
    purpose: str | None = None
    move_in_timeline: str | None = None

    confidence: dict = Field(default_factory=dict)
    llm_model: str | None = None
    llm_prompt_hash: str | None = None

    reviewed_by: uuid.UUID | None = None
    reviewed_at: datetime | None = None
    review_notes: str | None = None


class ProposalReviewRequest(BaseSchema):
    action: Literal["ACCEPT", "REJECT"]
    review_notes: str | None = Field(default=None, max_length=1000)


class GenerateProposalRequest(BaseSchema):
    messages: list[str] | None = Field(
        default=None,
        description="Explicit conversation transcripts or messages. If omitted, uses lead's raw_message_text.",
    )
