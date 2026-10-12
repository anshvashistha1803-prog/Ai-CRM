"""
Celery tasks for background processing: outbox dispatch, stalled lead detection,
and async AI qualifications.
"""

import asyncio
import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.messaging.models import OutboxMessage
from app.messaging.outbox_service import OutboxService
from app.messaging.whatsapp_client import WhatsAppClient
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()
settings = get_settings()


def _get_async_session():
    """Create a scoped async sessionmaker for Celery worker processes."""
    engine = create_async_engine(settings.database_url, echo=False)
    return async_sessionmaker(engine, expire_on_commit=False)


async def _handle_message_event(msg: OutboxMessage, db) -> str | None:
    """Route outbox message to its domain handler and return provider message ID if applicable."""
    logger.info(
        "processing_outbox_event",
        message_id=str(msg.id),
        event_type=msg.event_type,
        aggregate_type=msg.aggregate_type,
        aggregate_id=msg.aggregate_id,
    )

    if msg.event_type == "WHATSAPP_OUTBOUND_DISPATCH":
        # Reconcile if already dispatched in prior crashed attempt
        if msg.payload and msg.payload.get("provider_message_id"):
            logger.info(
                "whatsapp_already_dispatched_to_provider",
                message_id=str(msg.id),
                provider_message_id=msg.payload["provider_message_id"],
            )
            return msg.payload["provider_message_id"]

        recipient = msg.payload.get("recipient_phone") if msg.payload else None
        text = msg.payload.get("message_text") if msg.payload else None
        if recipient and text:
            wa_client = WhatsAppClient()
            res = await wa_client.send_text_message(recipient_phone=recipient, text=text)
            messages = res.get("messages", [])
            provider_id = messages[0].get("id") if messages else None
            return provider_id
        return None

    elif msg.event_type == "AI_QUALIFICATION_REQUESTED":
        lead_id = uuid.UUID(msg.aggregate_id)
        text = msg.payload.get("message_text", "") if msg.payload else ""
        sender_phone = msg.payload.get("sender_phone") if msg.payload else None
        from app.qualification.conversation_service import QualificationConversationService
        conv_svc = QualificationConversationService(db)
        try:
            await conv_svc.process_inbound_message(
                lead_id=lead_id,
                message_text=text,
                sender_phone=sender_phone,
            )
        except Exception as exc:
            logger.warning("ai_qualification_task_error", lead_id=str(lead_id), error=str(exc))
        return None

    elif msg.event_type == "BROKER_HANDOFF_NOTIFICATION":
        lead_id = uuid.UUID(msg.aggregate_id)
        logger.info(
            "broker_handoff_notification_dispatched",
            lead_id=str(lead_id),
            payload=msg.payload,
        )
        from datetime import UTC, datetime

        from app.notifications.models import Notification
        notif = Notification(
            id=uuid.uuid4(),
            tenant_id=msg.tenant_id,
            recipient_contact="broker@havnrealty.com",
            channel="IN_APP",
            title=f"Lead Handoff: {msg.payload.get('health', 'Qualified') if msg.payload else 'Lead'}",
            body=msg.payload.get("summary", "New qualified lead awaiting broker contact.") if msg.payload else "",
            is_read=False,
            sent_at=datetime.now(UTC),
        )
        db.add(notif)
        await db.flush()
        return None

    elif msg.event_type == "SHEETS_SYNC_REQUESTED":
        lead_id = msg.aggregate_id
        payload = msg.payload or {}
        intent = payload.get("intent", "BUY")
        answers = payload.get("answers", {})
        from app.integrations.sheets.service import GoogleSheetsSyncService
        sheets_svc = GoogleSheetsSyncService()
        spreadsheet_id = settings.google_sheets_spreadsheet_id or "havn-operational-sheets-default"
        if intent == "SELL":
            await sheets_svc.sync_seller_listing(spreadsheet_id, answers)
        else:
            await sheets_svc.sync_buyer_lead(spreadsheet_id, {
                "id": lead_id,
                "full_name": answers.get("full_name") or payload.get("lead_name"),
                "phone": answers.get("phone"),
                "property_type": answers.get("property_type"),
                "bhk": answers.get("bhk"),
                "budget_display": answers.get("budget_display"),
                "preferred_location": answers.get("preferred_location"),
                "purpose": answers.get("purpose"),
                "move_in_timeline": answers.get("move_in_timeline"),
                "intent_score": payload.get("score"),
                "lead_health": payload.get("health"),
                "summary": payload.get("summary"),
            })
        return None

    elif msg.event_type == "SITE_VISIT_NOTIFICATION":
        visit_id = msg.aggregate_id
        logger.info("dispatching_site_visit_notification", visit_id=visit_id)
        return None

    else:
        logger.info("handled_generic_event", event_type=msg.event_type)
        return None


async def _process_outbox_batch_async() -> int:
    """Async implementation of outbox batch processor."""
    session_factory = _get_async_session()
    processed_count = 0

    async with session_factory() as db:
        svc = OutboxService(db)
        await svc.reclaim_stale_processing_messages(lease_timeout_minutes=15)
        await db.commit()

        pending = await svc.fetch_pending_messages(batch_size=50)

        for msg in pending:
            try:
                await svc.mark_processing(msg.id)
                provider_id = await _handle_message_event(msg, db)
                await svc.mark_sent(msg.id, provider_message_id=provider_id)
                processed_count += 1
            except Exception as exc:
                logger.error(
                    "outbox_processing_failed",
                    message_id=str(msg.id),
                    error=str(exc),
                )
                await svc.mark_failed(msg.id, error_message=str(exc))
            finally:
                await db.commit()

    return processed_count


async def _detect_stalled_leads_async() -> int:
    """Async worker to scan for inactive leads and transition them to STALLED stage."""
    from datetime import UTC, datetime, timedelta

    from app.core.dependencies import TenantContext, UserRole
    from app.leads.models import Lead
    from app.leads.service import LeadService
    from app.leads.state_machine import LeadStage

    session_factory = _get_async_session()
    stalled_count = 0
    cutoff = datetime.now(UTC) - timedelta(days=settings.default_stall_threshold_days)

    async with session_factory() as db:
        stmt = (
            select(Lead)
            .where(
                Lead.is_deleted.is_(False),
                Lead.stage.in_([
                    LeadStage.NEW.value,
                    LeadStage.QUALIFYING.value,
                    LeadStage.QUALIFIED.value,
                    LeadStage.SITE_VISIT_SCHEDULED.value,
                    LeadStage.NEGOTIATION.value,
                ]),
                Lead.last_activity_at < cutoff,
            )
            .limit(100)
        )
        res = await db.execute(stmt)
        inactive_leads = res.scalars().all()

        for lead in inactive_leads:
            ctx = TenantContext(
                user_id=uuid.uuid4(),
                tenant_id=lead.tenant_id,
                role=UserRole.SUPER_ADMIN,
            )
            lead_svc = LeadService(db, ctx)
            try:
                await lead_svc.transition_stage(
                    lead_id=lead.id,
                    target_stage=LeadStage.STALLED,
                    reason=f"No activity for {settings.default_stall_threshold_days} days (automated detection).",
                    is_system=True,
                )
                stalled_count += 1
            except Exception as exc:
                logger.warning("stalled_transition_failed", lead_id=str(lead.id), error=str(exc))
            finally:
                await db.commit()

    return stalled_count


@celery_app.task(name="app.tasks.celery_tasks.process_outbox_batch")
def process_outbox_batch() -> int:
    """Periodic Celery task: Poll and process pending outbox messages."""
    return asyncio.run(_process_outbox_batch_async())


@celery_app.task(name="app.tasks.celery_tasks.detect_stalled_leads_batch")
def detect_stalled_leads_batch() -> int:
    """Periodic Celery task: Transition inactive leads to STALLED stage."""
    return asyncio.run(_detect_stalled_leads_async())


@celery_app.task(name="app.tasks.celery_tasks.dispatch_outbox_message")
def dispatch_outbox_message(message_id_str: str) -> bool:
    """Immediately dispatch a specific outbox message."""
    message_id = uuid.UUID(message_id_str)

    async def _dispatch():
        session_factory = _get_async_session()
        async with session_factory() as db:
            svc = OutboxService(db)
            msg = await svc.get_message(message_id)
            await svc.mark_processing(msg.id)
            try:
                provider_id = await _handle_message_event(msg, db)
                await svc.mark_sent(msg.id, provider_message_id=provider_id)
                await db.commit()
                return True
            except Exception as exc:
                await svc.mark_failed(msg.id, error_message=str(exc))
                await db.commit()
                return False

    return asyncio.run(_dispatch())
