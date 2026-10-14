"""
Celery tasks for background processing: outbox dispatch, stalled lead detection,
and async AI qualifications.
"""

import asyncio
import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.integrations.google.inventory_sync import PropertyInventorySyncService
from app.integrations.google.service import GoogleIntegrationService
from app.messaging.models import OutboxMessage
from app.messaging.outbox_service import OutboxService
from app.messaging.whatsapp_client import WhatsAppClient
from app.tasks.celery_app import celery_app

logger = structlog.get_logger()
settings = get_settings()


def _get_async_session():
    """
    Create a scoped async sessionmaker for Celery worker processes with strict database isolation.
    Guarantees that test workers fail-fast if attempted against production or development databases.
    """
    import os
    import sys

    from sqlalchemy.engine import make_url

    is_testing = (
        bool(os.getenv("PYTEST_CURRENT_TEST"))
        or "pytest" in sys.modules
        or os.getenv("TESTING") == "1"
        or settings.environment == "testing"
    )

    if is_testing:
        test_db_url = os.getenv("TEST_DATABASE_URL")
        if not test_db_url:
            raise RuntimeError(
                "CRITICAL: TEST_DATABASE_URL is required for worker tasks in testing mode. "
                "Refusing to fall back to application database."
            )
        parsed = make_url(test_db_url)
        if parsed.database != "havn_test":
            raise RuntimeError(
                f"CRITICAL: Test worker database must be 'havn_test', got '{parsed.database}'. "
                "Refusing to execute."
            )
        db_url = test_db_url
    else:
        db_url = os.getenv("TEST_DATABASE_URL") or settings.database_url
        parsed = make_url(db_url)
        if parsed.database == "havn_crm" and os.getenv("TEST_DATABASE_URL"):
            raise RuntimeError("CRITICAL: TEST_DATABASE_URL cannot point to 'havn_crm'. Refusing to execute.")

    engine = create_async_engine(db_url, echo=False)
    return async_sessionmaker(engine, expire_on_commit=False)



async def _handle_message_event(msg: OutboxMessage, db: AsyncSession | None = None) -> str | None:
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

        # Reconcile if already suppressed
        if msg.payload and msg.payload.get("suppressed"):
            logger.info(
                "whatsapp_already_suppressed",
                message_id=str(msg.id),
                reason=msg.payload.get("suppression_reason"),
            )
            return f"suppressed:{msg.payload.get('suppression_reason', 'ineligible')}"

        recipient = msg.payload.get("recipient_phone") if msg.payload else None
        template_name = msg.payload.get("template_name") if msg.payload else None
        text = msg.payload.get("message_text") if msg.payload else None
        if not recipient:
            raise ValueError("Missing recipient_phone in payload.")

        # Pre-flight revalidation immediately before external dispatch.
        # Note: In distributed architectures, there is an unavoidable narrow window (microseconds)
        # between database revalidation and provider HTTP socket write. Revalidating here as close
        # as practical to dispatch minimizes this race condition.
        from app.leads.models import ConsentRecord, Lead, LeadActivity
        from app.tenants.models import Tenant

        is_opt_out_notice = bool(
            (msg.payload or {}).get("is_opt_out_notice")
            or (msg.idempotency_key and msg.idempotency_key.startswith("ai-optout-"))
        )

        async def _revalidate_and_get_sender(session: AsyncSession) -> tuple[bool, str | None, str | None]:
            if msg.aggregate_type != "LEAD":
                return True, None, None

            try:
                lead_id = uuid.UUID(msg.aggregate_id)
            except (ValueError, TypeError):
                return False, "invalid_lead_id", None

            # 1. Lead existence and tenant isolation
            lead_stmt = select(Lead).where(
                Lead.id == lead_id,
                Lead.tenant_id == msg.tenant_id,
            )
            lead = (await session.execute(lead_stmt)).scalars().first()
            if not lead:
                return False, "lead_not_found_or_tenant_mismatch", None

            if lead.is_deleted:
                return False, "lead_is_deleted", None

            # 2. Fetch tenant to get configured sender phone number ID
            tenant_stmt = select(Tenant).where(Tenant.id == msg.tenant_id)
            tenant = (await session.execute(tenant_stmt)).scalars().first()
            tenant_phone_id = tenant.whatsapp_phone_number_id if tenant else None

            # Allow approved compliance opt-out receipt even if lead is opted out
            if is_opt_out_notice:
                return True, None, tenant_phone_id

            # 3. Standard qualification/outbound dispatch checks
            if lead.do_not_contact:
                return False, "lead_do_not_contact", tenant_phone_id

            if lead.automation_status in {"OPTED_OUT", "HUMAN_TAKEOVER", "PAUSED"}:
                return False, f"lead_automation_{lead.automation_status}", tenant_phone_id

            # 4. Consent recheck
            consent_stmt = (
                select(ConsentRecord)
                .where(
                    ConsentRecord.tenant_id == msg.tenant_id,
                    ConsentRecord.lead_id == lead_id,
                )
                .order_by(ConsentRecord.created_at.desc())
            )
            consent = (await session.execute(consent_stmt)).scalars().first()
            if consent and not consent.granted:
                return False, "consent_revoked", tenant_phone_id
            if not consent:
                return False, "missing_consent_record", tenant_phone_id

            return True, None, tenant_phone_id

        if db is not None:
            is_eligible, suppression_reason, tenant_phone_id = await _revalidate_and_get_sender(db)
        else:
            session_factory = _get_async_session()
            async with session_factory() as session:
                is_eligible, suppression_reason, tenant_phone_id = await _revalidate_and_get_sender(session)

        if not is_eligible:
            logger.warning(
                "whatsapp_outbound_dispatch_suppressed",
                message_id=str(msg.id),
                lead_id=str(msg.aggregate_id),
                tenant_id=str(msg.tenant_id),
                reason=suppression_reason,
            )
            # Update in-memory message payload so mark_sent commits the suppression details
            updated_payload = dict(msg.payload) if msg.payload else {}
            updated_payload["suppressed"] = True
            updated_payload["suppression_reason"] = suppression_reason
            msg.payload = updated_payload

            # Record auditable activity
            async def _record_suppression_activity(session: AsyncSession):
                try:
                    lead_id = uuid.UUID(msg.aggregate_id)
                    lead_check = await session.get(Lead, lead_id)
                    if not lead_check:
                        return
                    from datetime import UTC, datetime
                    act = LeadActivity(
                        id=uuid.uuid4(),
                        tenant_id=msg.tenant_id,
                        lead_id=lead_id,
                        actor_type="SYSTEM",
                        activity_type="OUTBOUND_MESSAGE_SUPPRESSED",
                        description=f"Outbound WhatsApp message suppressed: {suppression_reason}",
                        metadata_={"reason": suppression_reason, "outbox_message_id": str(msg.id)},
                        created_at=datetime.now(UTC),
                    )
                    session.add(act)
                    await session.flush()
                except Exception as act_err:
                    logger.warning("suppression_activity_recording_failed", error=str(act_err))

            if db is not None:
                await _record_suppression_activity(db)
            else:
                session_factory = _get_async_session()
                async with session_factory() as session:
                    await _record_suppression_activity(session)
                    await session.commit()

            return f"suppressed:{suppression_reason}"

        # External network send executed outside long-lived DB transactions
        wa_client = WhatsAppClient(default_phone_number_id=tenant_phone_id)
        if template_name:
            lang_code = msg.payload.get("language_code", "en_US")
            components = msg.payload.get("components")
            res = await wa_client.send_template_message(
                recipient_phone=recipient,
                template_name=template_name,
                language_code=lang_code,
                components=components,
                phone_number_id=tenant_phone_id,
            )
        elif text:
            res = await wa_client.send_text_message(
                recipient_phone=recipient,
                text=text,
                phone_number_id=tenant_phone_id,
            )
        else:
            raise ValueError("Missing both template_name and message_text in payload.")

        messages = res.get("messages", [])
        provider_id = messages[0].get("id") if messages else None
        if not provider_id:
            raise RuntimeError(f"WhatsApp provider did not return message ID: {res}")
        return provider_id

    elif msg.event_type == "AI_QUALIFICATION_REQUESTED":
        lead_id = uuid.UUID(msg.aggregate_id)
        payload = msg.payload or {}
        sender_phone = payload.get("sender_phone")
        action = payload.get("action")

        # Inbound WhatsApp conversational intake branch
        if sender_phone and action != "GENERATE_PROPOSAL":
            from app.qualification.conversation_service import QualificationConversationService

            text = payload.get("message_text", "")
            if db is not None:
                conv_svc = QualificationConversationService(db)
                await conv_svc.process_inbound_message(
                    lead_id=lead_id,
                    message_text=text,
                    sender_phone=sender_phone,
                )
            else:
                session_factory = _get_async_session()
                async with session_factory() as session:
                    conv_svc = QualificationConversationService(session)
                    await conv_svc.process_inbound_message(
                        lead_id=lead_id,
                        message_text=text,
                        sender_phone=sender_phone,
                    )
                    await session.commit()
            return None

        # Automatic AI qualification proposal creation branch
        from app.core.dependencies import TenantContext, UserRole
        from app.leads.models import Lead
        from app.qualification.conversation_service import QualificationConversationService
        from app.qualification.models import LeadQualificationProposal
        from app.qualification.service import QualificationService

        context = TenantContext(
            tenant_id=msg.tenant_id,
            user_id=None,
            role=UserRole.SUPER_ADMIN,
        )

        async def _execute_proposal_generation(session: AsyncSession) -> None:
            # 1. Strict tenant-isolated lead verification
            stmt = select(Lead).where(
                Lead.id == lead_id,
                Lead.tenant_id == msg.tenant_id,
                Lead.is_deleted.is_(False),
            )
            res = await session.execute(stmt)
            lead = res.scalars().first()
            if not lead:
                logger.warning(
                    "qualification_lead_not_found_or_tenant_mismatch",
                    lead_id=str(lead_id),
                    tenant_id=str(msg.tenant_id),
                )
                return None

            # 2. Hard guard: Check automation status
            if lead.automation_status in {
                "HUMAN_TAKEOVER",
                "PAUSED",
                "OPTED_OUT",
            }:
                logger.info(
                    "qualification_skipped_due_to_automation_status",
                    lead_id=str(lead.id),
                    status=lead.automation_status,
                )
                return None

            # 3. Idempotency check: Do not create duplicate proposals on retries or repeated delivery
            prop_stmt = select(LeadQualificationProposal).where(
                LeadQualificationProposal.tenant_id == msg.tenant_id,
                LeadQualificationProposal.lead_id == lead_id,
                LeadQualificationProposal.status.in_(["PENDING_REVIEW", "ACCEPTED"]),
            )
            existing_prop = (await session.execute(prop_stmt)).scalars().first()
            if existing_prop:
                logger.info(
                    "ai_qualification_proposal_already_exists",
                    lead_id=str(lead_id),
                    proposal_id=str(existing_prop.id),
                    status=existing_prop.status,
                )
                conv_svc = QualificationConversationService(session)
                await conv_svc.schedule_initial_contact(lead_id)
                return None

            # 4. Extract messages
            extraction_messages = payload.get("messages")
            if not extraction_messages:
                text = payload.get("message_text")
                if text:
                    extraction_messages = [text]
                elif lead.raw_message_text:
                    extraction_messages = [lead.raw_message_text]
                else:
                    extraction_messages = [f"Lead created via {lead.source} for {lead.full_name}."]

            qual_svc = QualificationService(session, context)
            proposal = await qual_svc.generate_proposal(
                lead_id=lead_id,
                messages=extraction_messages,
            )
            logger.info(
                "ai_qualification_proposal_automatically_created",
                lead_id=str(lead_id),
                proposal_id=str(proposal.id),
                tenant_id=str(msg.tenant_id),
            )

            # Phase 9: For eligible website/Meta leads, schedule initial WhatsApp qualification outreach
            conv_svc = QualificationConversationService(session)
            await conv_svc.schedule_initial_contact(lead_id)
            return None


        if db is not None:
            await _execute_proposal_generation(db)
        else:
            session_factory = _get_async_session()
            async with session_factory() as session:
                await _execute_proposal_generation(session)
                await session.commit()
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
        if db is not None:
            db.add(notif)
            await db.flush()
        else:
            session_factory = _get_async_session()
            async with session_factory() as session:
                session.add(notif)
                await session.commit()
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

    elif msg.event_type == "MATCH_COMPUTATION_REQUESTED":
        lead_id = uuid.UUID(msg.aggregate_id)
        from app.core.dependencies import TenantContext, UserRole
        from app.matching.service import MatchingService

        context = TenantContext(
            tenant_id=msg.tenant_id,
            user_id=None,
            role=UserRole.SUPER_ADMIN,
        )

        if db is not None:
            matching_svc = MatchingService(db, context)
            matches = await matching_svc.compute_matches_for_lead(lead_id)
            logger.info(
                "match_computation_completed",
                lead_id=str(lead_id),
                tenant_id=str(msg.tenant_id),
                matches_count=len(matches),
            )
        else:
            session_factory = _get_async_session()
            async with session_factory() as session:
                matching_svc = MatchingService(session, context)
                matches = await matching_svc.compute_matches_for_lead(lead_id)
                await session.commit()
                logger.info(
                    "match_computation_completed",
                    lead_id=str(lead_id),
                    tenant_id=str(msg.tenant_id),
                    matches_count=len(matches),
                )
        return None

    else:
        logger.info("handled_generic_event", event_type=msg.event_type)
        return None


async def _process_outbox_batch_async() -> int:
    """Async implementation of outbox batch processor with two-phase claim-and-process and OCC."""
    session_factory = _get_async_session()
    processed_count = 0

    # Phase 1: Reclaim expired leases and atomically claim pending batch in a short transaction
    claimed_msgs = []
    async with session_factory() as db:
        svc = OutboxService(db)
        await svc.reclaim_stale_processing_messages(lease_timeout_minutes=15)
        claimed_msgs = await svc.claim_pending_messages(batch_size=50)
        # Commit immediately so locks are released before any provider call
        await db.commit()

    if not claimed_msgs:
        return 0

    # Phase 2: Process each claimed message independently without holding long-lived DB transactions
    for msg in claimed_msgs:
        claimed_at = msg.updated_at
        claim_token = (msg.payload or {}).get("_claim_token")
        try:
            # External network calls execute without open DB transaction
            provider_id = await _handle_message_event(msg, db=None)

            async with session_factory() as db:
                svc = OutboxService(db)
                await svc.mark_sent(
                    msg.id,
                    provider_message_id=provider_id,
                    claimed_at=claimed_at,
                    claim_token=claim_token,
                )
                await db.commit()
            processed_count += 1
        except Exception as exc:
            from app.messaging.whatsapp_client import WhatsAppAmbiguousDeliveryError

            is_ambiguous = getattr(exc, "is_ambiguous", False) or isinstance(exc, WhatsAppAmbiguousDeliveryError)
            logger.error(
                "outbox_processing_failed",
                message_id=str(msg.id),
                error=str(exc),
                is_ambiguous=is_ambiguous,
            )
            try:
                async with session_factory() as db:
                    svc = OutboxService(db)
                    await svc.mark_failed(
                        msg.id,
                        error_message=str(exc),
                        claimed_at=claimed_at,
                        claim_token=claim_token,
                        is_ambiguous=is_ambiguous,
                    )
                    await db.commit()
            except Exception as commit_exc:
                logger.error(
                    "outbox_mark_failed_commit_error",
                    message_id=str(msg.id),
                    error=str(commit_exc),
                )

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


@celery_app.task(name="app.tasks.celery_tasks.process_outbox_batch", queue="messages")
def process_outbox_batch() -> int:
    """Periodic Celery task: Poll and process pending outbox messages."""
    return asyncio.run(_process_outbox_batch_async())


@celery_app.task(name="app.tasks.celery_tasks.detect_stalled_leads_batch", queue="default")
def detect_stalled_leads_batch() -> int:
    """Periodic Celery task: Transition inactive leads to STALLED stage."""
    return asyncio.run(_detect_stalled_leads_async())


@celery_app.task(name="app.tasks.celery_tasks.dispatch_outbox_message", queue="messages")
def dispatch_outbox_message(message_id_str: str) -> bool:
    """Immediately dispatch a specific outbox message with atomic claim and OCC protection."""
    message_id = uuid.UUID(message_id_str)

    async def _dispatch():
        session_factory = _get_async_session()

        # Phase 1: Atomically claim message if PENDING, releasing locks immediately
        claimed_msg = None
        async with session_factory() as db:
            svc = OutboxService(db)
            claimed_msg = await svc.claim_message(message_id)
            if not claimed_msg:
                logger.info("dispatch_outbox_message_already_claimed_or_processed", message_id=str(message_id))
                return False
            await db.commit()

        claimed_at = claimed_msg.updated_at
        claim_token = (claimed_msg.payload or {}).get("_claim_token")

        # Phase 2: Provider call outside DB transaction
        try:
            provider_id = await _handle_message_event(claimed_msg, db=None)
            async with session_factory() as db:
                svc = OutboxService(db)
                await svc.mark_sent(
                    message_id,
                    provider_message_id=provider_id,
                    claimed_at=claimed_at,
                    claim_token=claim_token,
                )
                await db.commit()
            return True
        except Exception as exc:
            logger.error("dispatch_outbox_message_failed", message_id=str(message_id), error=str(exc))
            try:
                async with session_factory() as db:
                    svc = OutboxService(db)
                    await svc.mark_failed(
                        message_id,
                        error_message=str(exc),
                        claimed_at=claimed_at,
                        claim_token=claim_token,
                    )
                    await db.commit()
            except Exception as commit_exc:
                logger.error("dispatch_mark_failed_commit_error", message_id=str(message_id), error=str(commit_exc))
            return False

    return asyncio.run(_dispatch())


async def _sync_all_active_google_sheets_inventory_async() -> dict[str, int]:
    """
    Iterate across all active, eligible tenant Google integrations and execute
    inventory synchronization with distributed lease locking and strict tenant isolation.
    Failures for any individual tenant are caught and logged without affecting other tenants.
    """
    session_factory = _get_async_session()
    async with session_factory() as db:
        tenant_ids = await GoogleIntegrationService.get_active_sync_tenant_ids(db)

    total_eligible = len(tenant_ids)
    synced_full = 0
    synced_partial = 0
    failed_validation = 0
    failed_infrastructure = 0
    skipped_locked = 0

    logger.info("google_sheets_scheduled_sync_batch_started", total_eligible=total_eligible)

    for tenant_id in tenant_ids:
        # Phase 1: Acquire lease-backed sync lock atomically
        token = None
        try:
            async with session_factory() as db:
                service = GoogleIntegrationService(db, tenant_id)
                token = await service.acquire_sync_lock()
                await db.commit()
        except Exception as exc:
            logger.error("google_sheets_sync_lock_acquisition_failed", tenant_id=str(tenant_id), error=str(exc))
            failed_infrastructure += 1
            continue

        if not token:
            logger.info("google_sheets_sync_skipped_locked", tenant_id=str(tenant_id))
            skipped_locked += 1
            continue

        # Phase 2: Execute synchronization with isolated error boundary
        try:
            async with session_factory() as db:
                sync_svc = PropertyInventorySyncService(db, tenant_id, lock_token=token)
                res = await sync_svc.sync_inventory()
                await db.commit()

                # Release lock with outcome status
                service = GoogleIntegrationService(db, tenant_id)
                await service.release_sync_lock(token, final_status=res.status)
                await db.commit()

            if res.status == "SUCCESS":
                synced_full += 1
            elif res.status == "PARTIAL_SUCCESS":
                synced_partial += 1
            else:
                failed_validation += 1

            logger.info(
                "google_sheets_sync_tenant_completed",
                tenant_id=str(tenant_id),
                status=res.status,
                created=res.created,
                updated=res.updated,
                staged=res.staged,
                errors=res.error_count,
            )
        except Exception as exc:
            failed_infrastructure += 1
            logger.error("google_sheets_sync_tenant_failed", tenant_id=str(tenant_id), error=str(exc))
            try:
                async with session_factory() as db:
                    service = GoogleIntegrationService(db, tenant_id)
                    await service.release_sync_lock(token, final_status="FAILED", error_message=str(exc))
                    await db.commit()
            except Exception as release_exc:
                logger.error("google_sheets_sync_release_lock_failed", tenant_id=str(tenant_id), error=str(release_exc))

    failed_total = failed_infrastructure + failed_validation
    logger.info(
        "google_sheets_scheduled_sync_batch_completed",
        total_eligible=total_eligible,
        synced=synced_full,
        synced_full=synced_full,
        synced_partial=synced_partial,
        failed=failed_total,
        failed_validation=failed_validation,
        failed_infrastructure=failed_infrastructure,
        skipped_locked=skipped_locked,
    )
    return {
        "total_eligible": total_eligible,
        "synced": synced_full,
        "synced_full": synced_full,
        "synced_partial": synced_partial,
        "failed": failed_total,
        "failed_validation": failed_validation,
        "failed_infrastructure": failed_infrastructure,
        "skipped_locked": skipped_locked,
    }


@celery_app.task(name="app.tasks.celery_tasks.sync_all_active_google_sheets_inventory", queue="default")
def sync_all_active_google_sheets_inventory() -> dict[str, int]:
    """Periodic Celery task: Synchronize Google Sheets inventory for all active tenants."""
    return asyncio.run(_sync_all_active_google_sheets_inventory_async())

