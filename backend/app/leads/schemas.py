"""
Pydantic schemas for the leads module: request/response validation,
filtering, pagination, and state machine transitions.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import EmailStr, Field

from app.core.schemas.base import BaseSchema, PaginatedResponse
from app.leads.state_machine import LeadStage


class LeadCreateRequest(BaseSchema):
    """Payload for creating a new lead from webform, WhatsApp, or manual entry."""

    full_name: str = Field(min_length=1, max_length=255)
    phone: str = Field(min_length=7, max_length=50, description="Raw phone number to be normalized")
    email: EmailStr | None = None
    preferred_contact: str = Field(default="WHATSAPP", max_length=20)
    source: str = Field(default="MANUAL", max_length=50)

    # Qualification criteria
    budget_min: int | None = Field(default=None, ge=0)
    budget_max: int | None = Field(default=None, ge=0)
    currency_code: str = Field(default="INR", max_length=3)
    property_type: str | None = Field(default=None, max_length=50)
    bhk: int | None = Field(default=None, ge=1, le=10)
    preferred_location: str | None = Field(default=None, max_length=255)
    purpose: str | None = Field(default=None, max_length=50)  # BUY, RENT, INVEST
    move_in_timeline: str | None = Field(default=None, max_length=50)

    notes: str | None = None
    assigned_agent_id: uuid.UUID | None = None

    # DPDP / GDPR consent tracking
    consent_granted: bool = True
    consent_channel: str = Field(default="FORM", max_length=50)
    consent_ip: str | None = None
    consent_user_agent: str | None = None


class LeadUpdateRequest(BaseSchema):
    """Payload for updating an existing lead's profile."""

    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    phone: str | None = Field(default=None, min_length=7, max_length=50)
    email: EmailStr | None = None
    preferred_contact: str | None = Field(default=None, max_length=20)

    budget_min: int | None = Field(default=None, ge=0)
    budget_max: int | None = Field(default=None, ge=0)
    currency_code: str | None = Field(default=None, max_length=3)
    property_type: str | None = Field(default=None, max_length=50)
    bhk: int | None = Field(default=None, ge=1, le=10)
    preferred_location: str | None = Field(default=None, max_length=255)
    purpose: str | None = Field(default=None, max_length=50)
    move_in_timeline: str | None = Field(default=None, max_length=50)

    notes: str | None = None
    assigned_agent_id: uuid.UUID | None = None
    do_not_contact: bool | None = None


class LeadStageTransitionRequest(BaseSchema):
    """Payload for requesting an explicit state machine transition."""

    target_stage: LeadStage
    reason: str | None = Field(default=None, max_length=500)


class LeadAssignRequest(BaseSchema):
    """Payload for explicitly assigning or reassigning a lead to an agent."""

    assigned_agent_id: uuid.UUID | None = None


class LeadResponse(BaseSchema):
    """Public representation of a Lead with decrypted PII for authorized agents."""

    id: uuid.UUID
    tenant_id: uuid.UUID
    external_id: str | None = None
    source: str
    full_name: str
    phone: str | None = None  # Decrypted E.164 phone
    email: str | None = None  # Decrypted email
    preferred_contact: str

    budget_min: int | None = None
    budget_max: int | None = None
    currency_code: str
    property_type: str | None = None
    bhk: int | None = None
    preferred_location: str | None = None
    purpose: str | None = None
    move_in_timeline: str | None = None

    stage: str
    lead_health: str
    intent_score: int | None = None
    automation_status: str
    assigned_agent_id: uuid.UUID | None = None
    do_not_contact: bool
    is_duplicate_of: uuid.UUID | None = None

    last_activity_at: datetime | None = None
    score_breakdown: dict[str, Any] | None = None
    notes: str | None = None

    created_at: datetime
    updated_at: datetime


class LeadActivityResponse(BaseSchema):
    """Activity timeline event."""

    id: uuid.UUID
    lead_id: uuid.UUID
    actor_id: uuid.UUID | None = None
    actor_type: str
    activity_type: str
    description: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class LeadListResponse(PaginatedResponse[LeadResponse]):
    """Paginated lead list response."""
    pass
