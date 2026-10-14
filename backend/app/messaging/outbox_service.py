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

from app.core.exceptions import ConflictError, NotFoundError
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
        is_ambiguous: bool = False,
    ) -> OutboxMessage:
        """
        Record failure, increment retry count, and schedule next retry with exponential backoff.
        Moves to DEAD_LETTER if max_retries is reached or if delivery state is AMBIGUOUS
        (to prevent blind duplicate outbound WhatsApp sends).
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
        if is_ambiguous:
            updated_payload["delivery_state"] = "AMBIGUOUS"
            updated_payload["ambiguous_delivery"] = True
            updated_payload["ambiguous_at"] = now.isoformat()
        msg.payload = updated_payload

        if is_ambiguous:
            # Ambiguous delivery (socket timeout after send): do not blindly retry immediately.
            # Move to DEAD_LETTER with reconciliation flag so operator can review before resending.
            msg.status = "DEAD_LETTER"
            msg.processed_at = now
            msg.last_error = f"AMBIGUOUS_DELIVERY_REQUIRES_RECONCILIATION: {error_message}"
            logger.error(
                "outbox_message_ambiguous_delivery_dead_lettered",
                message_id=str(msg.id),
                event_type=msg.event_type,
                aggregate_id=msg.aggregate_id,
                error=msg.last_error,
            )
        elif msg.retry_count >= msg.max_retries:
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

    async def list_dead_letters(
        self,
        tenant_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[OutboxMessage]:
        """
        List dead-lettered messages for an authorized tenant.
        Strictly tenant-isolated and ordered by most recently updated.
        """
        stmt = (
            select(OutboxMessage)
            .where(
                OutboxMessage.tenant_id == tenant_id,
                OutboxMessage.status == "DEAD_LETTER",
            )
            .order_by(OutboxMessage.updated_at.desc())
            .offset(offset)
            .limit(limit)
        )
        res = await self.db.execute(stmt)
        return res.scalars().all()

    async def retry_dead_letter(
        self,
        message_id: uuid.UUID,
        tenant_id: uuid.UUID,
        operator_id: uuid.UUID | None = None,
        *,
        reconciliation_outcome: str | None = None,
        allow_duplicate_risk_override: bool = False,
        override_reason: str | None = None,
    ) -> OutboxMessage:
        """
        Operator-initiated recovery for an exhausted dead-lettered outbox event.
        Guarded by SELECT FOR UPDATE to prevent concurrent operator race conditions.
        Enforces tenant isolation, idempotency checks against already-succeeded business actions,
        protects ambiguous delivery from blind resending without reconciliation or audited override,
        resets status to PENDING with retry_count=0, and records an auditable LeadActivity.
        """
        stmt = (
            select(OutboxMessage)
            .where(
                OutboxMessage.id == message_id,
                OutboxMessage.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        res = await self.db.execute(stmt)
        msg = res.scalars().first()
        if not msg:
            raise NotFoundError("Dead-letter outbox message not found.")

        if msg.status != "DEAD_LETTER":
            raise ConflictError(
                f"Cannot retry outbox message in status '{msg.status}'. Only DEAD_LETTER messages can be retried."
            )

        # Idempotency guard: never resend an event if its business action already succeeded
        payload = msg.payload or {}
        if payload.get("provider_message_id"):
            raise ConflictError(
                "Cannot retry outbox message: external provider already confirmed delivery (provider_message_id present)."
            )

        if msg.event_type == "AI_QUALIFICATION_REQUESTED" and msg.aggregate_type == "LEAD":
            from app.qualification.models import LeadQualificationProposal
            prop_stmt = select(LeadQualificationProposal).where(
                LeadQualificationProposal.tenant_id == tenant_id,
                LeadQualificationProposal.lead_id == uuid.UUID(msg.aggregate_id),
                LeadQualificationProposal.status == "ACCEPTED",
            )
            accepted_prop = (await self.db.execute(prop_stmt)).scalars().first()
            if accepted_prop:
                raise ConflictError(
                    "Cannot retry qualification outbox event: proposal has already been reviewed and accepted by broker."
                )

        # Ambiguous Delivery Safety Gate:
        # If delivery state is AMBIGUOUS, blind retries risk duplicate outbound message delivery.
        # Require either:
        # 1. A verified reconciliation outcome ('CONFIRMED_UNDELIVERED'), or
        # 2. An explicit, audited override with duplicate-risk acknowledgment and justification (min 10 chars).
        is_ambiguous = (
            payload.get("delivery_state") == "AMBIGUOUS"
            or payload.get("ambiguous_delivery") is True
            or (msg.last_error and "AMBIGUOUS_DELIVERY" in msg.last_error)
        )

        now = datetime.now(UTC)
        updated_payload = dict(msg.payload) if msg.payload else {}
        updated_payload.pop("_claim_token", None)

        if is_ambiguous:
            prior_reconciled = payload.get("reconciliation_status") == "CONFIRMED_UNDELIVERED"
            has_valid_reconciliation = prior_reconciled or (reconciliation_outcome == "CONFIRMED_UNDELIVERED")
            has_valid_override = allow_duplicate_risk_override and bool(override_reason and len(override_reason.strip()) >= 10)

            if not has_valid_reconciliation and not has_valid_override:
                logger.warning(
                    "outbox_retry_ambiguous_delivery_rejected",
                    message_id=str(message_id),
                    tenant_id=str(tenant_id),
                    reason="missing_reconciliation_or_override",
                )
                raise ConflictError(
                    "Cannot retry ambiguous delivery outbox message without verified reconciliation ('CONFIRMED_UNDELIVERED') "
                    "or an explicit duplicate-risk override with valid justification (min 10 characters)."
                )

            if has_valid_override:
                updated_payload["duplicate_risk_override"] = True
                updated_payload["duplicate_risk_warning"] = "OPERATOR_ACKNOWLEDGED_DUPLICATE_MESSAGE_RISK"
                updated_payload["override_reason"] = override_reason.strip() if override_reason else "ADMIN_OVERRIDE"
                updated_payload["override_by"] = str(operator_id) if operator_id else "OPERATOR"
                updated_payload["override_at"] = now.isoformat()
                logger.warning(
                    "outbox_dead_letter_duplicate_risk_override_applied",
                    message_id=str(msg.id),
                    tenant_id=str(tenant_id),
                    operator_id=str(operator_id) if operator_id else None,
                    reason=updated_payload["override_reason"],
                )
            elif has_valid_reconciliation:
                updated_payload["reconciliation_status"] = "CONFIRMED_UNDELIVERED"
                updated_payload["reconciled_by"] = str(operator_id) if operator_id else "OPERATOR"
                updated_payload["reconciled_at"] = now.isoformat()
                logger.info(
                    "outbox_dead_letter_reconciled_undelivered",
                    message_id=str(msg.id),
                    tenant_id=str(tenant_id),
                )

        msg.status = "PENDING"
        msg.retry_count = 0
        msg.scheduled_at = now
        msg.updated_at = now
        msg.processed_at = None

        if operator_id:
            updated_payload["_operator_retried_by"] = str(operator_id)
        msg.payload = updated_payload

        # Record auditable activity if aggregate is LEAD and lead exists
        if msg.aggregate_type == "LEAD":
            try:
                from app.leads.models import Lead, LeadActivity
                lead_id = uuid.UUID(msg.aggregate_id)
                lead_check = await self.db.get(Lead, lead_id)
                if lead_check:
                    act_desc = f"Dead-letter outbox message {msg.id} ({msg.event_type}) reset to PENDING by operator."
                    if is_ambiguous:
                        if updated_payload.get("duplicate_risk_override"):
                            act_desc += f" (DUPLICATE RISK OVERRIDE: {updated_payload.get('override_reason')})"
                        else:
                            act_desc += " (Reconciled as CONFIRMED_UNDELIVERED)"

                    act = LeadActivity(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        lead_id=lead_id,
                        actor_type="OPERATOR",
                        activity_type="OUTBOX_DEAD_LETTER_RETRIED",
                        description=act_desc,
                        metadata_={
                            "message_id": str(msg.id),
                            "event_type": msg.event_type,
                            "operator_id": str(operator_id) if operator_id else None,
                            "is_ambiguous": is_ambiguous,
                            "duplicate_risk_override": updated_payload.get("duplicate_risk_override", False),
                            "override_reason": updated_payload.get("override_reason"),
                        },
                        created_at=now,
                    )
                    self.db.add(act)
            except Exception as audit_err:
                logger.warning("retry_dead_letter_audit_failed", error=str(audit_err))

        await self.db.flush()
        logger.info(
            "outbox_dead_letter_retried",
            message_id=str(msg.id),
            tenant_id=str(tenant_id),
            event_type=msg.event_type,
            operator_id=str(operator_id) if operator_id else None,
        )
        return msg

    async def reconcile_dead_letter(
        self,
        message_id: uuid.UUID,
        tenant_id: uuid.UUID,
        reconciliation_outcome: str,
        provider_message_id: str | None = None,
        notes: str | None = None,
        operator_id: uuid.UUID | None = None,
    ) -> OutboxMessage:
        """
        Dedicated reconciliation workflow for ambiguous delivery dead-letters.
        - CONFIRMED_DELIVERED: Message was received by customer/provider; marks outbox status=SENT
          and records provider_message_id, preventing duplicate outbound resend.
        - CONFIRMED_UNDELIVERED: Message was definitely not received upstream; marks reconciliation
          status so that retry_dead_letter can safely resend without duplicate risk.
        """
        stmt = (
            select(OutboxMessage)
            .where(
                OutboxMessage.id == message_id,
                OutboxMessage.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        res = await self.db.execute(stmt)
        msg = res.scalars().first()
        if not msg:
            raise NotFoundError("Dead-letter outbox message not found.")

        if msg.status != "DEAD_LETTER":
            raise ConflictError(f"Cannot reconcile outbox message in status '{msg.status}'.")

        outcome = reconciliation_outcome.strip().upper()
        if outcome not in {"CONFIRMED_DELIVERED", "CONFIRMED_UNDELIVERED"}:
            raise ConflictError("Reconciliation outcome must be either CONFIRMED_DELIVERED or CONFIRMED_UNDELIVERED.")

        now = datetime.now(UTC)
        payload = dict(msg.payload) if msg.payload else {}
        payload["reconciliation_status"] = outcome
        payload["reconciled_by"] = str(operator_id) if operator_id else "OPERATOR"
        payload["reconciled_at"] = now.isoformat()
        if notes:
            payload["reconciliation_notes"] = notes

        if outcome == "CONFIRMED_DELIVERED":
            if provider_message_id:
                payload["provider_message_id"] = provider_message_id
            msg.status = "SENT"
            msg.processed_at = now
            msg.last_error = f"RECONCILED_AS_DELIVERED: {msg.last_error or 'Ambiguous delivery resolved'}"
        else:
            msg.last_error = "RECONCILED_AS_UNDELIVERED: Ready for safe retry"

        msg.payload = payload
        msg.updated_at = now

        # Auditable activity
        if msg.aggregate_type == "LEAD":
            try:
                from app.leads.models import Lead, LeadActivity
                lead_id = uuid.UUID(msg.aggregate_id)
                lead_check = await self.db.get(Lead, lead_id)
                if lead_check:
                    act = LeadActivity(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        lead_id=lead_id,
                        actor_type="OPERATOR",
                        activity_type="OUTBOX_RECONCILIATION_COMPLETED",
                        description=f"Outbox message {msg.id} reconciled as {outcome}.",
                        metadata_={
                            "message_id": str(msg.id),
                            "outcome": outcome,
                            "operator_id": str(operator_id) if operator_id else None,
                            "notes": notes,
                        },
                        created_at=now,
                    )
                    self.db.add(act)
            except Exception as audit_err:
                logger.warning("reconcile_dead_letter_audit_failed", error=str(audit_err))

        await self.db.flush()
        return msg
