"""
Outbox ORM model — durable database-backed task and message outbox.
Ensures at-least-once delivery, safe retries, and transactional guarantees.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.models.mixins import TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class OutboxMessage(Base, UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin):
    """
    Transactional Outbox record.
    Created in the same database transaction as business events to prevent dual-write inconsistencies.
    Background workers poll or receive signals to process pending records.
    """

    __tablename__ = "outbox_messages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_outbox_tenant_idempotency"),
    )

    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(50), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(100), nullable=False)

    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # Status: PENDING, PROCESSING, SENT, FAILED, DEAD_LETTER
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="PENDING", index=True
    )

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        index=True,
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )

    def __repr__(self) -> str:
        return (
            f"<OutboxMessage id={self.id} event={self.event_type!r} "
            f"status={self.status!r} retries={self.retry_count}>"
        )
