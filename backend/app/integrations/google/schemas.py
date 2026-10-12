"""
Pydantic schemas for Google OAuth and Sheets Integration.
Ensures zero credential leakage: tokens and secrets are never in response schemas.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class GoogleAuthorizeResponse(BaseModel):
    """Returned when initiating Google OAuth flow."""

    authorization_url: str = Field(description="Google OAuth 2.0 authorization URL to redirect the user to")
    state: str = Field(description="Cryptographically signed OAuth state token")


class GoogleCallbackResponse(BaseModel):
    """Returned upon successful OAuth callback completion."""

    status: str = "connected"
    google_email: str | None = None
    scopes: list[str] = Field(default_factory=list)
    spreadsheet_id: str | None = None


class GoogleStatusResponse(BaseModel):
    """Health/Status response for Google integration. Does not expose tokens or secrets."""

    model_config = ConfigDict(from_attributes=True)

    is_connected: bool
    connected_at: datetime | None = None
    connected_by: uuid.UUID | None = None
    google_email: str | None = None
    scopes: list[str] = Field(default_factory=list)
    spreadsheet_id: str | None = None
    properties_sheet_name: str = "Properties"
    incoming_leads_sheet_name: str = "Incoming Leads"
    qualified_leads_sheet_name: str = "Qualified Leads"
    column_mappings: dict[str, Any] = Field(default_factory=dict)
    sync_mode: str = "MERGE_VALIDATED"
    last_sync_at: datetime | None = None
    last_sync_status: str | None = None
    last_sync_error: str | None = None
    last_sync_stats: dict[str, Any] = Field(default_factory=dict)


class GoogleConfigUpdateRequest(BaseModel):
    """Tenant configuration for Google Sheets sync."""

    spreadsheet_id: str | None = None
    properties_sheet_name: str | None = None
    incoming_leads_sheet_name: str | None = None
    qualified_leads_sheet_name: str | None = None
    column_mappings: dict[str, Any] | None = None
    sync_mode: str | None = None


class InventorySyncResponse(BaseModel):
    """Result of property inventory synchronization."""

    status: str
    spreadsheet_id: str
    sheet_name: str
    total_rows: int
    created: int
    updated: int
    staged: int = 0
    duplicate_suspects: int = 0
    skipped: int
    error_count: int
    errors: list[dict[str, Any]] = Field(default_factory=list)


class InventoryStagingItemResponse(BaseModel):
    """Details of a single staged inventory row."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    integration_id: uuid.UUID | None = None
    spreadsheet_id: str | None = None
    sheet_name: str | None = None
    row_index: int | None = None
    external_id: str | None = None
    status: str
    validation_errors: list[Any] = Field(default_factory=list)
    raw_data: dict[str, Any] = Field(default_factory=dict)
    parsed_data: dict[str, Any] = Field(default_factory=dict)
    source_size: str | None = None
    source_area: str | None = None
    owner_name: str | None = None
    owner_phone: str | None = None
    duplicate_matched_property_id: uuid.UUID | None = None
    promoted_property_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class InventoryStagingListResponse(BaseModel):
    """List of staged inventory items."""

    data: list[InventoryStagingItemResponse]
    total: int
    page: int = 1
    page_size: int = 50


class InventoryStagingPromoteRequest(BaseModel):
    """Payload to review and promote a staged row into a canonical Property."""

    price: int = Field(gt=0, description="Price in smallest currency unit (INR)")
    property_type: str | None = Field(default=None, max_length=50)
    title: str | None = Field(default=None, max_length=255)
    location_text: str | None = Field(default=None, max_length=255)
    bhk: int | None = Field(default=None, ge=1, le=20)
    area_sqft: int | None = Field(default=None, ge=1)
    description: str | None = None
    status: str = Field(default="AVAILABLE", max_length=30)

