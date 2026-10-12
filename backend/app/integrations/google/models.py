"""
Google Integration ORM model.
Stores tenant-scoped OAuth credentials (encrypted at rest), scopes, connection status,
and Google Sheets operational configuration.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.models.mixins import SoftDeleteMixin, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class TenantGoogleIntegration(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """
    Tenant-scoped Google OAuth 2.0 integration and Sheets configuration.
    Strictly isolated per agency tenant. Refresh tokens and access tokens
    are stored encrypted using application AES-256-GCM.
    """

    __tablename__ = "tenant_google_integrations"

    # One integration record per tenant
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    connected_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Connected account identity
    google_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    google_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Secure credentials (encrypted with AES-256-GCM)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # Status tracking
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Google Sheets configuration
    spreadsheet_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    properties_sheet_name: Mapped[str] = mapped_column(String(100), nullable=False, default="Properties")
    incoming_leads_sheet_name: Mapped[str] = mapped_column(String(100), nullable=False, default="Incoming Leads")
    qualified_leads_sheet_name: Mapped[str] = mapped_column(String(100), nullable=False, default="Qualified Leads")
    column_mappings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sync_mode: Mapped[str] = mapped_column(String(50), nullable=False, default="MERGE_VALIDATED")

    # Sync audit & status
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_status: Mapped[str | None] = mapped_column(String(50), nullable=True)  # SUCCESS, FAILED, REVOKED
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sync_stats: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Relationships
    tenant: Mapped["Tenant"] = relationship("Tenant", lazy="raise")  # type: ignore[name-defined]  # noqa: F821
    user: Mapped["User"] = relationship("User", lazy="raise")  # type: ignore[name-defined]  # noqa: F821

    __table_args__ = (
        Index("ix_google_integrations_tenant_active", "tenant_id", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<TenantGoogleIntegration tenant_id={self.tenant_id} is_active={self.is_active} email={self.google_email!r}>"


class InventoryStagingItem(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """
    Inventory staging record for property listings imported from Google Sheets
    that cannot yet become canonical CRM properties (missing prices, ambiguous values,
    potential duplicates, or needing review).
    """

    __tablename__ = "inventory_staging_items"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenant_google_integrations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    spreadsheet_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sheet_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    row_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Status: PENDING_REVIEW, READY, PROMOTED, REJECTED, DUPLICATE_SUSPECT
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="PENDING_REVIEW")

    # Validation errors & reasons why staging was required
    validation_errors: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # Raw row dictionary verbatim as read from the sheet
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Candidate attributes extracted
    parsed_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Explicit preservation of source size vs area
    source_size: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_area: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Staged owner contact details
    owner_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Links to existing properties for duplicate review or successful promotion
    duplicate_matched_property_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("properties.id", ondelete="SET NULL"),
        nullable=True,
    )
    promoted_property_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("properties.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Relationships
    tenant: Mapped["Tenant"] = relationship("Tenant", lazy="raise")  # type: ignore[name-defined]  # noqa: F821
    duplicate_property: Mapped["Property | None"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "Property",
        foreign_keys=[duplicate_matched_property_id],
        lazy="raise",
    )
    promoted_property: Mapped["Property | None"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "Property",
        foreign_keys=[promoted_property_id],
        lazy="raise",
    )

    __table_args__ = (
        Index("ix_inventory_staging_tenant_status", "tenant_id", "status"),
    )

    def __repr__(self) -> str:
        return f"<InventoryStagingItem id={self.id} tenant_id={self.tenant_id} status={self.status!r}>"

