"""
Lead ORM models: Lead, LeadActivity, ConsentRecord.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.models.mixins import SoftDeleteMixin, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Lead(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """
    Central entity representing a prospective buyer/renter.
    PII fields (phone_e164_enc, email_enc) are stored encrypted via AES-256-GCM.
    Decryption happens in the service layer, never in the model.
    """

    __tablename__ = "leads"

    # Inbound channel
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="MANUAL")

    # Contact info — ENCRYPTED at rest
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_e164_enc: Mapped[str | None] = mapped_column(Text, nullable=True)   # encrypted
    email_enc: Mapped[str | None] = mapped_column(Text, nullable=True)         # encrypted
    phone_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # blind index for dedup
    email_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # blind index for dedup
    preferred_contact: Mapped[str] = mapped_column(String(20), nullable=False, default="WHATSAPP")

    # Qualification (may be AI-proposed or manually entered)
    budget_min: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    budget_max: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    property_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    bhk: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    preferred_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(50), nullable=True)
    move_in_timeline: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # State machine fields (kept separate by design)
    stage: Mapped[str] = mapped_column(String(50), nullable=False, default="NEW")
    lead_health: Mapped[str] = mapped_column(String(20), nullable=False, default="UNKNOWN")
    intent_score: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    automation_status: Mapped[str] = mapped_column(String(30), nullable=False, default="ACTIVE")

    # Assignment
    assigned_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # Flags
    do_not_contact: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_duplicate_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Metadata
    last_activity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    score_breakdown: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw_message_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    activities: Mapped[list["LeadActivity"]] = relationship(
        "LeadActivity", back_populates="lead", lazy="raise"
    )
    consent_records: Mapped[list["ConsentRecord"]] = relationship(
        "ConsentRecord", back_populates="lead", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<Lead id={self.id} stage={self.stage!r} source={self.source!r}>"


class LeadActivity(Base, UUIDPrimaryKeyMixin, TenantMixin):
    """
    Append-only activity log for a lead.
    Every meaningful event (stage change, note, message, AI action) is recorded here.
    """

    __tablename__ = "lead_activities"

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False, default="USER")
    activity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    lead: Mapped[Lead] = relationship("Lead", back_populates="activities", lazy="raise")

    def __repr__(self) -> str:
        return f"<LeadActivity lead={self.lead_id} type={self.activity_type!r}>"


class ConsentRecord(Base, UUIDPrimaryKeyMixin, TenantMixin):
    """
    Immutable record of consent granted or revoked.
    Required for GDPR / DPDP-India compliance.
    Never updated — append new records for changes.
    """

    __tablename__ = "consent_records"

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    consent_type: Mapped[str] = mapped_column(String(50), nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    channel: Mapped[str] = mapped_column(String(50), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)  # IPv6 max = 39
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    consent_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    lead: Mapped[Lead] = relationship("Lead", back_populates="consent_records", lazy="raise")

