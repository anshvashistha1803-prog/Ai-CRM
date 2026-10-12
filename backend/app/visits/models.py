"""
SiteVisit ORM model — tracks in-person property site visits, scheduling, agent assignments,
cancellations, and conflict-checked booking states.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.models.mixins import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class SiteVisit(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    """
    Scheduled property visit by a prospective lead.
    Tracks state machine from SCHEDULED -> CONFIRMED -> COMPLETED / CANCELLED.
    """

    __tablename__ = "site_visits"

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    property_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("properties.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    assigned_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    scheduled_start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    scheduled_end_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # Status: SCHEDULED, CONFIRMED, COMPLETED, CANCELLED, RESCHEDULED, NO_SHOW
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="SCHEDULED", index=True
    )

    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    rescheduled_from_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("site_visits.id", ondelete="SET NULL"),
        nullable=True,
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    broker_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Relationships
    lead: Mapped["Lead"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "Lead", lazy="raise"
    )
    property: Mapped["Property"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "Property", lazy="raise"
    )
    assigned_agent: Mapped["User"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "User", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<SiteVisit id={self.id} lead={self.lead_id} status={self.status!r}>"

