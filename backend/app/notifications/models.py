"""
Notification ORM model — tracks broker and client multi-channel notifications.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.models.mixins import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Notification(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    """
    In-app and outgoing notification delivery record.
    """

    __tablename__ = "notifications"

    recipient_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    recipient_contact: Mapped[str] = mapped_column(String(255), nullable=False)
    channel: Mapped[str] = mapped_column(String(50), nullable=False)  # WHATSAPP, EMAIL, IN_APP
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    recipient_user: Mapped["User"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "User", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<Notification id={self.id} channel={self.channel!r} to={self.recipient_contact!r}>"

