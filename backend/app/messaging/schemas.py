"""
Pydantic schemas for Outbox and Operations endpoints.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DeadLetterMessageSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    event_type: str
    aggregate_type: str
    aggregate_id: str
    status: str
    retry_count: int
    max_retries: int
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    payload_preview: dict[str, Any] = {}


class DeadLetterListResponse(BaseModel):
    items: list[DeadLetterMessageSummary]
    total: int
    limit: int
    offset: int


class DeadLetterRetryResponse(BaseModel):
    message_id: uuid.UUID
    tenant_id: uuid.UUID
    status: str
    retried_by: uuid.UUID | None = None
    detail: str


class DeadLetterRetryRequest(BaseModel):
    reconciliation_outcome: str | None = None  # e.g. "CONFIRMED_UNDELIVERED"
    allow_duplicate_risk_override: bool = False
    override_reason: str | None = None


class DeadLetterReconcileRequest(BaseModel):
    reconciliation_outcome: str  # "CONFIRMED_UNDELIVERED" or "CONFIRMED_DELIVERED"
    provider_message_id: str | None = None
    notes: str | None = None


class DeadLetterReconcileResponse(BaseModel):
    message_id: uuid.UUID
    tenant_id: uuid.UUID
    status: str
    reconciliation_outcome: str
    reconciled_by: uuid.UUID | None = None
    detail: str

