"""
OutboxService — manages transactional outbox records, idempotency checks,
safe retries with exponential backoff, and dead-letter queues.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.messaging.models import OutboxMessage

logger = structlog.get_logger()


class OutboxService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def enqueue_message(
        self,
        tenant_id: uuid.UUID,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str | uuid.UUID,
        payload: dict,
        idempotency_key: str | None = None,
        scheduled_at: datetime | None = None,
        max_retries: int = 3,
    ) -> OutboxMessage:
        """
        Durable task / message publication.
        Atomically staged in the database with caller's transaction.
        Deduplicates when idempotency_key is provided.
        """
        agg_id_str = str(aggregate_id)

        if idempotency_key:
            stmt = select(OutboxMessage).where(
                OutboxMessage.tenant_id == tenant_id,
                OutboxMessage.idempotency_key == idempotency_key,
            )
            res = await self.db.execute(stmt)
            existing = res.scalars().first()
            if existing:
                logger.info(
                    "outbox_message_deduplicated",
                    idempotency_key=idempotency_key,
                    event_type=event_type,
                    existing_id=str(existing.id),
                )
                return existing

        now = datetime.now(UTC)
        msg = OutboxMessage(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=agg_id_str,
            payload=payload,
            status="PENDING",
            retry_count=0,
            max_retries=max_retries,
            scheduled_at=scheduled_at or now,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
        self.db.add(msg)
        await self.db.flush()

        logger.info(
            "outbox_message_enqueued",
            message_id=str(msg.id),
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=agg_id_str,
        )
        return msg

    async def fetch_pending_messages(
        self,
        batch_size: int = 50,
        now: datetime | None = None,
        lock_rows: bool = True,
    ) -> Sequence[OutboxMessage]:
        """Fetch pending messages due for processing with skip-locked row claiming."""
        current_time = now or datetime.now(UTC)
        stmt = (
            select(OutboxMessage)
            .where(
                OutboxMessage.status == "PENDING",
                OutboxMessage.scheduled_at <= current_time,
            )
            .order_by(OutboxMessage.scheduled_at.asc())
            .limit(batch_size)
        )
        if lock_rows:
            stmt = stmt.with_for_update(skip_locked=True)

        res = await self.db.execute(stmt)
        return res.scalars().all()

    async def reclaim_stale_processing_messages(
        self,
        lease_timeout_minutes: int = 15,
        now: datetime | None = None,
    ) -> int:
        """
        Reclaim messages stuck in PROCESSING status due to crashed or timed-out workers.
        Transitions them back to PENDING if retries remain, or DEAD_LETTER if exhausted.
        """
        current_time = now or datetime.now(UTC)
        cutoff = current_time - timedelta(minutes=lease_timeout_minutes)

        stmt = select(OutboxMessage).where(
            OutboxMessage.status == "PROCESSING",
            OutboxMessage.updated_at < cutoff,
        ).with_for_update(skip_locked=True)
        res = await self.db.execute(stmt)
        stale_messages = res.scalars().all()

        reclaimed_count = 0
        for msg in stale_messages:
            msg.retry_count += 1
            msg.last_error = f"Worker lease expired (processing stuck for >{lease_timeout_minutes}m)"
            msg.updated_at = current_time
            # Invalidate claim token so stale worker cannot finalize
            updated_payload = dict(msg.payload) if msg.payload else {}
            updated_payload["_claim_token"] = None
            msg.payload = updated_payload
            if msg.retry_count >= msg.max_retries:
                msg.status = "DEAD_LETTER"
                msg.processed_at = current_time
            else:
                msg.status = "PENDING"
                msg.scheduled_at = current_time
            reclaimed_count += 1

        if reclaimed_count > 0:
            await self.db.flush()
            logger.warning("stale_outbox_messages_reclaimed", count=reclaimed_count)

        return reclaimed_count

    async def claim_pending_messages(
        self,
        batch_size: int = 50,
        now: datetime | None = None,
    ) -> Sequence[OutboxMessage]:
        """
        Atomically fetch pending messages due for processing and transition them to PROCESSING.
        Uses FOR UPDATE SKIP LOCKED to prevent duplicate claiming across workers.
        Assigns a unique UUID claim token for reliable stale-worker rejection without migrations.
        """
        current_time = now or datetime.now(UTC)
        messages = await self.fetch_pending_messages(
            batch_size=batch_size,
            now=current_time,
            lock_rows=True,
        )
        for msg in messages:
            msg.status = "PROCESSING"
            msg.updated_at = current_time
            updated_payload = dict(msg.payload) if msg.payload else {}
            updated_payload["_claim_token"] = str(uuid.uuid4())
            msg.payload = updated_payload
        if messages:
            await self.db.flush()
        return messages

    async def claim_message(
        self,
        message_id: uuid.UUID,
        now: datetime | None = None,
    ) -> OutboxMessage | None:
        """
        Atomically claim a specific pending message for processing.
        Uses FOR UPDATE SKIP LOCKED. Returns None if already claimed or processed.
        Assigns a unique UUID claim token for reliable stale-worker rejection without migrations.
        """
        current_time = now or datetime.now(UTC)
        stmt = (
            select(OutboxMessage)
            .where(
                OutboxMessage.id == message_id,
                OutboxMessage.status == "PENDING",
            )
            .with_for_update(skip_locked=True)
        )
        res = await self.db.execute(stmt)
        msg = res.scalars().first()
        if not msg:
            return None

        msg.status = "PROCESSING"
        msg.updated_at = current_time
        updated_payload = dict(msg.payload) if msg.payload else {}
        updated_payload["_claim_token"] = str(uuid.uuid4())
        msg.payload = updated_payload
        await self.db.flush()
        return msg

    async def get_message(self, message_id: uuid.UUID) -> OutboxMessage:
        """Retrieve an outbox message by ID."""
        res = await self.db.execute(
            select(OutboxMessage).where(OutboxMessage.id == message_id)
        )
        msg = res.scalars().first()
        if not msg:
            raise NotFoundError("Outbox message not found.")
        return msg

    async def mark_processing(self, message_id: uuid.UUID) -> OutboxMessage:
        """Transition status from PENDING to PROCESSING."""
        msg = await self.get_message(message_id)
        msg.status = "PROCESSING"
        msg.updated_at = datetime.now(UTC)
        await self.db.flush()
        return msg

    async def mark_sent(
        self,
        message_id: uuid.UUID,
        provider_message_id: str | None = None,
        claimed_at: datetime | None = None,
        claim_token: str | None = None,
    ) -> OutboxMessage:
        """
        Mark outbox message as successfully delivered / processed.
        Guards against stale workers via unique claim_token and/or claimed_at timestamp.
        """
        now = datetime.now(UTC)
        msg = await self.get_message(message_id)
        if msg.status != "PROCESSING":
            logger.warning(
                "outbox_status_not_processing",
                message_id=str(msg.id),
                current_status=msg.status,
                action="mark_sent_ignored",
            )
            return msg

        # Primary OCC verification: unique UUID claim token
        if claim_token is not None:
            current_token = (msg.payload or {}).get("_claim_token")
            if current_token != claim_token:
                logger.warning(
                    "outbox_claim_token_mismatch",
                    message_id=str(msg.id),
                    expected_token=claim_token,
                    current_token=current_token,
                    action="mark_sent_ignored",
                )
                return msg
        elif claimed_at is not None and msg.updated_at is not None:
            # Fallback timestamp comparison if claim_token is not provided
            if abs(msg.updated_at.timestamp() - claimed_at.timestamp()) > 1.0:
                logger.warning(
                    "outbox_claim_expired_or_overwritten",
                    message_id=str(msg.id),
                    claimed_at=claimed_at.isoformat(),
                    current_updated_at=msg.updated_at.isoformat(),
                    action="mark_sent_ignored",
                )
                return msg

        msg.status = "SENT"
        msg.processed_at = now
        msg.updated_at = now
        updated_payload = dict(msg.payload) if msg.payload else {}
        if provider_message_id:
            updated_payload["provider_message_id"] = provider_message_id
        # Clear claim token on completion
        updated_payload.pop("_claim_token", None)
        msg.payload = updated_payload

        await self.db.flush()

        logger.info(
            "outbox_message_sent",
            message_id=str(msg.id),
            event_type=msg.event_type,
            aggregate_id=msg.aggregate_id,
            provider_message_id=provider_message_id,
        )
        return msg

    async def mark_failed(
        self,
        message_id: uuid.UUID,
        error_message: str,
        claimed_at: datetime | None = None,
        claim_token: str | None = None,
        base_backoff_seconds: int = 30,
    ) -> OutboxMessage:
        """
        Record failure, increment retry count, and schedule next retry with exponential backoff.
        Moves to DEAD_LETTER if max_retries is reached.
        Guards against stale workers via unique claim_token and/or claimed_at timestamp.
        """
        now = datetime.now(UTC)
        msg = await self.get_message(message_id)
        if msg.status != "PROCESSING":
            logger.warning(
                "outbox_status_not_processing",
                message_id=str(msg.id),
                current_status=msg.status,
                action="mark_failed_ignored",
            )
            return msg

        # Primary OCC verification: unique UUID claim token
        if claim_token is not None:
            current_token = (msg.payload or {}).get("_claim_token")
            if current_token != claim_token:
                logger.warning(
                    "outbox_claim_token_mismatch",
                    message_id=str(msg.id),
                    expected_token=claim_token,
                    current_token=current_token,
                    action="mark_failed_ignored",
                )
                return msg
        elif claimed_at is not None and msg.updated_at is not None:
            # Fallback timestamp comparison if claim_token is not provided
            if abs(msg.updated_at.timestamp() - claimed_at.timestamp()) > 1.0:
                logger.warning(
                    "outbox_claim_expired_or_overwritten",
                    message_id=str(msg.id),
                    claimed_at=claimed_at.isoformat(),
                    current_updated_at=msg.updated_at.isoformat(),
                    action="mark_failed_ignored",
                )
                return msg

        msg.retry_count += 1
        msg.last_error = error_message
        msg.updated_at = now
        updated_payload = dict(msg.payload) if msg.payload else {}
        updated_payload.pop("_claim_token", None)
        msg.payload = updated_payload

        if msg.retry_count >= msg.max_retries:
            msg.status = "DEAD_LETTER"
            msg.processed_at = now
            logger.error(
                "outbox_message_dead_lettered",
                message_id=str(msg.id),
                event_type=msg.event_type,
                aggregate_id=msg.aggregate_id,
                retries=msg.retry_count,
                error=error_message,
            )
        else:
            # Exponential backoff: base * 2^(retry_count - 1)
            backoff = base_backoff_seconds * (2 ** (msg.retry_count - 1))
            msg.status = "PENDING"
            msg.scheduled_at = now + timedelta(seconds=backoff)
            logger.warning(
                "outbox_message_retry_scheduled",
                message_id=str(msg.id),
                event_type=msg.event_type,
                retry_count=msg.retry_count,
                next_attempt_at=msg.scheduled_at.isoformat(),
                error=error_message,
            )

        await self.db.flush()
        return msg
