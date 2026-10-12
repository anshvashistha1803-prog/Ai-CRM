"""
LeadQualificationProposal ORM model — tracks AI-generated field extraction proposals
requiring human agent review before mutating lead records.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.models.mixins import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class LeadQualificationProposal(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    """
    AI-proposed lead qualification criteria requiring agent review.
    Raw LLM outputs are captured for auditing, but never exposed to end-users.
    """

    __tablename__ = "lead_qualification_proposals"

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    proposed_by: Mapped[str] = mapped_column(String(50), nullable=False, default="AI")
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="PENDING_REVIEW"
    )  # PENDING_REVIEW, ACCEPTED, REJECTED, SUPERSEDED

    # Proposed field values
    budget_min: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    budget_max: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    property_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    bhk: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    preferred_location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(50), nullable=True)
    move_in_timeline: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Confidence scores (0.0 - 1.0 per extracted field)
    confidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Auditing
    llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    llm_prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_llm_output: Mapped[str | None] = mapped_column(Text, nullable=True)

    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    lead: Mapped["Lead"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "Lead", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<Proposal id={self.id} lead={self.lead_id} status={self.status!r}>"

