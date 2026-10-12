"""
Property ORM model — listing catalog for an agency tenant.
"""

import uuid

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.models.mixins import SoftDeleteMixin, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.matching.models import LeadPropertyMatch  # noqa: F401


class Property(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, SoftDeleteMixin):
    """
    Real estate property listing owned by an agency tenant.
    """

    __tablename__ = "properties"

    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    property_type: Mapped[str] = mapped_column(String(50), nullable=False)  # APARTMENT, VILLA, PLOT, COMMERCIAL
    bhk: Mapped[int | None] = mapped_column(Integer, nullable=True)
    price: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    location_text: Mapped[str] = mapped_column(String(255), nullable=False)
    latitude: Mapped[float | None] = mapped_column(Numeric(10, 7), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric(10, 7), nullable=True)
    area_sqft: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    amenities: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="AVAILABLE")  # AVAILABLE, SOLD, RENTED, UNAVAILABLE, DRAFT
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    photos: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)

    # Private owner contact information & verbatim source data (excluded from public responses)
    owner_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Relationships
    matches: Mapped[list["LeadPropertyMatch"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "LeadPropertyMatch", back_populates="property", lazy="raise"
    )

    __table_args__ = (
        Index("ix_properties_tenant_owner_phone", "tenant_id", "owner_phone"),
    )

    def __repr__(self) -> str:
        return f"<Property id={self.id} title={self.title!r} status={self.status!r}>"

