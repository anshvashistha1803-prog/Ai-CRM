"""
LeadPropertyMatch ORM model — links qualified leads to prospective properties.
"""

import uuid

from sqlalchemy import ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.models.mixins import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class LeadPropertyMatch(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    """
    Scored match between a lead and a property with explanation reasons.
    """

    __tablename__ = "lead_property_matches"
    __table_args__ = (
        UniqueConstraint("lead_id", "property_id", name="uq_lead_property_match"),
    )

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
    match_score: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)  # 0 to 100
    match_reasons: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="SUGGESTED")  # SUGGESTED, SHORTLISTED, REJECTED, VISITED

    # Relationships
    lead: Mapped["Lead"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "Lead", lazy="raise"
    )
    property: Mapped["Property"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "Property", back_populates="matches", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<LeadPropertyMatch lead={self.lead_id} prop={self.property_id} score={self.match_score}>"

