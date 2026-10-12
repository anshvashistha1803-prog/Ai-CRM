"""
Tenant ORM model.
This is the root entity — all other models have a FK to tenants.id.
"""


from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.models.mixins import SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Tenant(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """
    Represents one agency (customer) of the platform.
    Every piece of data belongs to exactly one Tenant.
    """

    __tablename__ = "tenants"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)

    # WhatsApp config (stored encrypted in application layer)
    whatsapp_phone_number_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    whatsapp_access_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    whatsapp_verify_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Email config (stored encrypted)
    email_from: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    smtp_creds_enc: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Locale
    timezone: Mapped[str] = mapped_column(String(50), nullable=False, default="UTC")
    currency_code: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")

    # Status & feature flags
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Relationships (back-populated from child models)
    users: Mapped[list["User"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "User", back_populates="tenant", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<Tenant id={self.id} slug={self.slug!r}>"

