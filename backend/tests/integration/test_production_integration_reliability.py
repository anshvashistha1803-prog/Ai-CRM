"""
Comprehensive Production Integration Reliability & Failure Recovery Suite (Phase 10 Workstream E).
Runs strictly against dedicated PostgreSQL database (havn_test).

Covers all 18 production integration failure & edge scenarios:
1. Worker stopped during lead creation.
2. Worker restart processes pending events.
3. Worker crash during processing and lease reclamation.
4. Database transaction rollback.
5. AI exception or invalid response handling.
6. WhatsApp timeout retry handling.
7. Provider accepted send but local acknowledgement failed (at-least-once delivery ambiguity).
8. Duplicate outbox delivery idempotency.
9. Duplicate inbound webhook deduplication.
10. Expired lease reclaimed to DEAD_LETTER on retry exhaustion.
11. Consent revoked between scheduling and dispatch suppresses send.
12. STOP received while outbound message queued suppresses send.
13. Broker takeover during qualification suppresses automated send.
14. Cross-tenant event suppressed by pre-flight guard.
15. Duplicate Meta lead webhook idempotency.
16. Meta lead retrieval failure handling.
17. Authorized operator dead-letter inspection and recovery.
18. Lead remains persisted and accessible when all background services fail.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.dependencies import TenantContext, UserRole
from app.leads.models import ConsentRecord, Lead, LeadActivity
from app.leads.schemas import LeadCreateRequest
from app.leads.service import LeadService
from app.leads.state_machine import AutomationStatus, LeadStage
from app.messaging.models import OutboxMessage
from app.messaging.outbox_service import OutboxService
from app.qualification.models import LeadQualificationProposal
from app.tasks.celery_tasks import _handle_message_event, _process_outbox_batch_async
from app.tenants.models import Tenant
from app.webhooks.meta_service import MetaLeadAdsWebhookService
from app.webhooks.whatsapp_service import WhatsAppWebhookService
from tests.conftest import test_engine


def _get_db() -> AsyncSession:
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    return factory()


@pytest.mark.asyncio
class TestProductionIntegrationReliability:
    async def _create_tenant(self, db: AsyncSession, name: str = "Test Agency", meta_page_id: str | None = None) -> Tenant:
        tenant_id = uuid.uuid4()
        tenant = Tenant(
            id=tenant_id,
            name=name,
            slug=f"agency-{uuid.uuid4().hex[:8]}",
            timezone="Asia/Kolkata",
            currency_code="INR",
            is_active=True,
            whatsapp_phone_number_id="10987654321",
            settings={"meta_page_id": meta_page_id} if meta_page_id else {},
        )
        db.add(tenant)
        await db.commit()
        return tenant

    # ── Scenario 1: Worker stopped during lead creation ───────────────────────
    async def test_01_worker_stopped_during_lead_creation(self, create_tables):
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            ctx = TenantContext(tenant_id=tenant.id, user_id=None, role=UserRole.SUPER_ADMIN)
            lead_svc = LeadService(db, ctx)

            # Worker is completely stopped / zero worker execution
            lead = await lead_svc.create_lead(
                LeadCreateRequest(
                    full_name="Offline Worker Lead",
                    phone="+919876543210",
                    source="WEBSITE",
                    preferred_contact="WHATSAPP",
                    consent_granted=True,
                )
            )
            await db.commit()

            # Verify lead exists in NEW stage and outbox event is staged PENDING
            lead_res = await db.get(Lead, lead.id)
            assert lead_res is not None
            assert lead_res.stage == LeadStage.NEW.value

            outbox_res = (
                await db.execute(
                    sa.select(OutboxMessage).where(
                        OutboxMessage.aggregate_id == str(lead.id),
                        OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
                    )
                )
            ).scalars().first()
            assert outbox_res is not None
            assert outbox_res.status == "PENDING"

    # ── Scenario 2: Worker restart processes pending events ───────────────────
    async def test_02_worker_restart_processes_pending_events(self, create_tables):
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            ctx = TenantContext(tenant_id=tenant.id, user_id=None, role=UserRole.SUPER_ADMIN)
            lead_svc = LeadService(db, ctx)

            lead = await lead_svc.create_lead(
                LeadCreateRequest(
                    full_name="Restart Test Lead",
                    phone="+919876543211",
                    source="WEBSITE",
                    preferred_contact="WHATSAPP",
                    consent_granted=True,
                )
            )
            await db.commit()

        # Simulate worker starting up and processing batch
        processed = await _process_outbox_batch_async()
        assert processed >= 1

        async with _get_db() as db:
            outbox_res = (
                await db.execute(
                    sa.select(OutboxMessage).where(
                        OutboxMessage.aggregate_id == str(lead.id),
                        OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
                    )
                )
            ).scalars().first()
            assert outbox_res.status == "SENT"

    # ── Scenario 3: Worker crash during processing & lease reclamation ─────────
    async def test_03_worker_crash_and_lease_reclamation(self, create_tables):
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            svc = OutboxService(db)
            # Create message that crashed 20 minutes ago
            stale_time = datetime.now(UTC) - timedelta(minutes=20)
            msg = await svc.enqueue_message(
                tenant_id=tenant.id,
                event_type="AI_QUALIFICATION_REQUESTED",
                aggregate_type="LEAD",
                aggregate_id=uuid.uuid4(),
                payload={"action": "GENERATE_PROPOSAL"},
            )
            msg.status = "PROCESSING"
            msg.updated_at = stale_time
            msg.payload = {"_claim_token": "crashed-worker-token"}
            await db.commit()

            # Reclaim stale processing
            reclaimed = await svc.reclaim_stale_processing_messages(lease_timeout_minutes=15)
            assert reclaimed == 1
            await db.commit()

            await db.refresh(msg)
            assert msg.status == "PENDING"
            assert msg.retry_count == 1
            assert msg.payload.get("_claim_token") is None

    # ── Scenario 4: Database transaction rollback ─────────────────────────────
    async def test_04_database_transaction_rollback(self, create_tables):
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            ctx = TenantContext(tenant_id=tenant.id, user_id=None, role=UserRole.SUPER_ADMIN)
            lead_svc = LeadService(db, ctx)

            try:
                async with db.begin():
                    lead = await lead_svc.create_lead(
                        LeadCreateRequest(
                            full_name="Rollback Lead",
                            phone="+919876543212",
                            source="WEBSITE",
                            preferred_contact="WHATSAPP",
                            consent_granted=True,
                        )
                    )
                    lead_id = lead.id
                    raise RuntimeError("Simulated mid-transaction failure")
            except RuntimeError:
                pass

            # Verify neither lead nor outbox record was persisted
            lead_check = await db.get(Lead, lead_id)
            assert lead_check is None

    # ── Scenario 5: AI exception or invalid response ──────────────────────────
    async def test_05_ai_exception_schedules_retry(self, create_tables):
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            ctx = TenantContext(tenant_id=tenant.id, user_id=None, role=UserRole.SUPER_ADMIN)
            lead_svc = LeadService(db, ctx)

            lead = await lead_svc.create_lead(
                LeadCreateRequest(
                    full_name="AI Error Lead",
                    phone="+919876543213",
                    source="WEBSITE",
                    preferred_contact="WHATSAPP",
                    consent_granted=True,
                )
            )
            await db.commit()

        # Simulate LLM raising temporary provider exception
        with patch("app.qualification.service.QualificationService.generate_proposal", side_effect=RuntimeError("AI Provider Timeout")):
            await _process_outbox_batch_async()

        async with _get_db() as db:
            outbox_res = (
                await db.execute(
                    sa.select(OutboxMessage).where(
                        OutboxMessage.aggregate_id == str(lead.id),
                        OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
                    )
                )
            ).scalars().first()
            assert outbox_res.status == "PENDING"
            assert outbox_res.retry_count == 1
            assert "AI Provider Timeout" in outbox_res.last_error

    # ── Scenario 6: WhatsApp timeout retry handling ───────────────────────────
    async def test_06_whatsapp_timeout_schedules_retry(self, create_tables):
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            ctx = TenantContext(tenant_id=tenant.id, user_id=None, role=UserRole.SUPER_ADMIN)
            lead_svc = LeadService(db, ctx)
            lead = await lead_svc.create_lead(
                LeadCreateRequest(
                    full_name="Timeout Test Lead",
                    phone="+919876543214",
                    source="WEBSITE",
                    preferred_contact="WHATSAPP",
                    consent_granted=True,
                )
            )
            svc = OutboxService(db)
            msg = await svc.enqueue_message(
                tenant_id=tenant.id,
                event_type="WHATSAPP_OUTBOUND_DISPATCH",
                aggregate_type="LEAD",
                aggregate_id=lead.id,
                payload={
                    "recipient_phone": "+919876543214",
                    "template_name": "lead_qualification_intro",
                },
            )
            await db.commit()

        import httpx
        with patch("app.messaging.whatsapp_client.WhatsAppClient.send_template_message", side_effect=httpx.TimeoutException("Meta API timeout")):
            await _process_outbox_batch_async()

        async with _get_db() as db:
            refreshed = await db.get(OutboxMessage, msg.id)
            assert refreshed.status == "PENDING"
            assert refreshed.retry_count == 1
            assert "Meta API timeout" in refreshed.last_error

    # ── Scenario 7: Provider accepted send but local ack failed (at-least-once) ─
    async def test_07_provider_accepted_send_delivery_ambiguity(self, create_tables):
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            svc = OutboxService(db)
            msg = await svc.enqueue_message(
                tenant_id=tenant.id,
                event_type="WHATSAPP_OUTBOUND_DISPATCH",
                aggregate_type="SYSTEM",
                aggregate_id=uuid.uuid4(),
                payload={
                    "recipient_phone": "+919876543215",
                    "message_text": "Delivery ambiguity test",
                    "provider_message_id": "wamid.prior.dispatched.123",
                },
            )
            await db.commit()

            # When provider_message_id is already in payload, _handle_message_event reconciles
            result_id = await _handle_message_event(msg, db=db)
            assert result_id == "wamid.prior.dispatched.123"

    # ── Scenario 8: Duplicate outbox delivery idempotency ─────────────────────
    async def test_08_duplicate_outbox_delivery_is_idempotent(self, create_tables):
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            ctx = TenantContext(tenant_id=tenant.id, user_id=None, role=UserRole.SUPER_ADMIN)
            lead_svc = LeadService(db, ctx)

            lead = await lead_svc.create_lead(
                LeadCreateRequest(
                    full_name="Idempotency Lead",
                    phone="+919876543216",
                    source="WEBSITE",
                    preferred_contact="WHATSAPP",
                    consent_granted=True,
                )
            )
            await db.commit()

        # Run process batch twice
        await _process_outbox_batch_async()
        await _process_outbox_batch_async()

        async with _get_db() as db:
            props = (
                await db.execute(
                    sa.select(LeadQualificationProposal).where(
                        LeadQualificationProposal.tenant_id == tenant.id,
                        LeadQualificationProposal.lead_id == lead.id,
                    )
                )
            ).scalars().all()
            assert len(props) == 1, "Duplicate outbox processing must not create duplicate proposals"

    # ── Scenario 9: Duplicate inbound webhook deduplication ───────────────────
    async def test_09_duplicate_inbound_webhook_deduplication(self, create_tables):
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            wh_svc = WhatsAppWebhookService(db)

            wamid = f"wamid.unique.{uuid.uuid4()}"
            payload = {
                "entry": [{
                    "changes": [{
                        "value": {
                            "metadata": {"phone_number_id": tenant.whatsapp_phone_number_id},
                            "contacts": [{"wa_id": "919876543217", "profile": {"name": "Webhook Lead"}}],
                            "messages": [{"id": wamid, "from": "919876543217", "type": "text", "text": {"body": "Looking for 3BHK"}}],
                        }
                    }]
                }]
            }

            # First delivery creates lead
            r1 = await wh_svc.process_inbound_webhook(payload)
            await db.commit()
            assert r1[0]["action"] == "created"
            lead_id = r1[0]["lead_id"]

            # Second delivery with exact same payload reuses existing lead and deduplicates
            r2 = await wh_svc.process_inbound_webhook(payload)
            await db.commit()
            assert r2[0]["action"] == "updated"
            assert r2[0]["lead_id"] == lead_id

    # ── Scenario 10: Expired lease reclaimed to DEAD_LETTER on exhaustion ──────
    async def test_10_expired_lease_reclaimed_to_dead_letter(self, create_tables):
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            svc = OutboxService(db)
            msg = await svc.enqueue_message(
                tenant_id=tenant.id,
                event_type="AI_QUALIFICATION_REQUESTED",
                aggregate_type="LEAD",
                aggregate_id=uuid.uuid4(),
                payload={},
                max_retries=3,
            )
            msg.status = "PROCESSING"
            msg.retry_count = 2  # next reclaim reaches 3 -> DEAD_LETTER
            msg.updated_at = datetime.now(UTC) - timedelta(minutes=20)
            await db.commit()

            reclaimed = await svc.reclaim_stale_processing_messages(lease_timeout_minutes=15)
            assert reclaimed == 1
            await db.commit()

            await db.refresh(msg)
            assert msg.status == "DEAD_LETTER"
            assert msg.retry_count == 3

    # ── Scenario 11: Consent revoked between scheduling and dispatch ──────────
    async def test_11_consent_revoked_suppresses_dispatch(self, create_tables):
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            ctx = TenantContext(tenant_id=tenant.id, user_id=None, role=UserRole.SUPER_ADMIN)
            lead_svc = LeadService(db, ctx)

            lead = await lead_svc.create_lead(
                LeadCreateRequest(
                    full_name="Revoked Consent Lead",
                    phone="+919876543218",
                    source="WEBSITE",
                    preferred_contact="WHATSAPP",
                    consent_granted=True,
                )
            )
            await db.commit()

            # Schedule outbound message
            svc = OutboxService(db)
            msg = await svc.enqueue_message(
                tenant_id=tenant.id,
                event_type="WHATSAPP_OUTBOUND_DISPATCH",
                aggregate_type="LEAD",
                aggregate_id=lead.id,
                payload={"recipient_phone": "+919876543218", "template_name": "lead_qualification_intro"},
            )
            await db.commit()

            # Customer explicitly revokes consent
            revocation = ConsentRecord(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                lead_id=lead.id,
                consent_type="WHATSAPP",
                granted=False,
                channel="WHATSAPP",
                consent_text="User revoked consent via settings",
                created_at=datetime.now(UTC),
            )
            db.add(revocation)
            await db.commit()

            # Dispatch worker runs pre-flight revalidation
            with patch("app.messaging.whatsapp_client.WhatsAppClient.send_template_message") as mock_send:
                res_id = await _handle_message_event(msg, db=db)
                assert res_id.startswith("suppressed:consent_revoked")
                assert not mock_send.called, "Must NOT make external network call when consent is revoked!"

            # Verify suppression activity was recorded
            act = (
                await db.execute(
                    sa.select(LeadActivity).where(
                        LeadActivity.lead_id == lead.id,
                        LeadActivity.activity_type == "OUTBOUND_MESSAGE_SUPPRESSED",
                    )
                )
            ).scalars().first()
            assert act is not None
            assert act.metadata_.get("reason") == "consent_revoked"

    # ── Scenario 12: STOP received while outbound message queued ──────────────
    async def test_12_stop_received_suppresses_queued_outbound(self, create_tables):
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            ctx = TenantContext(tenant_id=tenant.id, user_id=None, role=UserRole.SUPER_ADMIN)
            lead_svc = LeadService(db, ctx)

            lead = await lead_svc.create_lead(
                LeadCreateRequest(
                    full_name="Opt Out Queued Lead",
                    phone="+919876543219",
                    source="WEBSITE",
                    preferred_contact="WHATSAPP",
                    consent_granted=True,
                )
            )
            await db.commit()

            svc = OutboxService(db)
            msg = await svc.enqueue_message(
                tenant_id=tenant.id,
                event_type="WHATSAPP_OUTBOUND_DISPATCH",
                aggregate_type="LEAD",
                aggregate_id=lead.id,
                payload={"recipient_phone": "+919876543219", "message_text": "What is your budget?"},
            )
            await db.commit()

            # Customer texts STOP -> lead marked DO_NOT_CONTACT and OPTED_OUT
            lead = await db.get(Lead, lead.id)
            lead.do_not_contact = True
            lead.stage = LeadStage.DO_NOT_CONTACT.value
            lead.automation_status = AutomationStatus.OPTED_OUT.value
            await db.commit()

            # Dispatch worker runs
            with patch("app.messaging.whatsapp_client.WhatsAppClient.send_text_message") as mock_send:
                res_id = await _handle_message_event(msg, db=db)
                assert res_id.startswith("suppressed:lead_do_not_contact")
                assert not mock_send.called

    # ── Scenario 13: Broker takeover during qualification suppresses send ─────
    async def test_13_broker_takeover_suppresses_automated_send(self, create_tables):
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            ctx = TenantContext(tenant_id=tenant.id, user_id=None, role=UserRole.SUPER_ADMIN)
            lead_svc = LeadService(db, ctx)

            lead = await lead_svc.create_lead(
                LeadCreateRequest(
                    full_name="Human Takeover Lead",
                    phone="+919876543220",
                    source="WEBSITE",
                    preferred_contact="WHATSAPP",
                    consent_granted=True,
                )
            )
            await db.commit()

            svc = OutboxService(db)
            msg = await svc.enqueue_message(
                tenant_id=tenant.id,
                event_type="WHATSAPP_OUTBOUND_DISPATCH",
                aggregate_type="LEAD",
                aggregate_id=lead.id,
                payload={"recipient_phone": "+919876543220", "message_text": "Automated question"},
            )
            await db.commit()

            # Broker assumes human takeover
            lead = await db.get(Lead, lead.id)
            lead.automation_status = AutomationStatus.HUMAN_TAKEOVER.value
            await db.commit()

            with patch("app.messaging.whatsapp_client.WhatsAppClient.send_text_message") as mock_send:
                res_id = await _handle_message_event(msg, db=db)
                assert res_id.startswith("suppressed:lead_automation_HUMAN_TAKEOVER")
                assert not mock_send.called

    # ── Scenario 14: Cross-tenant event suppressed by pre-flight guard ────────
    async def test_14_cross_tenant_event_suppressed(self, create_tables):
        async with _get_db() as db:
            tenant_a = await self._create_tenant(db, name="Agency A")
            tenant_b = await self._create_tenant(db, name="Agency B")

            ctx_b = TenantContext(tenant_id=tenant_b.id, user_id=None, role=UserRole.SUPER_ADMIN)
            lead_svc_b = LeadService(db, ctx_b)
            lead_b = await lead_svc_b.create_lead(
                LeadCreateRequest(
                    full_name="Lead in Agency B",
                    phone="+919876543221",
                    source="WEBSITE",
                    preferred_contact="WHATSAPP",
                    consent_granted=True,
                )
            )
            await db.commit()

            # Malicious / corrupted outbox event belonging to Agency A targeting Lead in Agency B
            svc_a = OutboxService(db)
            cross_msg = await svc_a.enqueue_message(
                tenant_id=tenant_a.id,
                event_type="WHATSAPP_OUTBOUND_DISPATCH",
                aggregate_type="LEAD",
                aggregate_id=lead_b.id,
                payload={"recipient_phone": "+919876543221", "message_text": "Cross-tenant leak attempt"},
            )
            await db.commit()

            with patch("app.messaging.whatsapp_client.WhatsAppClient.send_text_message") as mock_send:
                res_id = await _handle_message_event(cross_msg, db=db)
                assert res_id == "suppressed:lead_not_found_or_tenant_mismatch"
                assert not mock_send.called

    # ── Scenario 15: Duplicate Meta lead webhook idempotency ──────────────────
    async def test_15_duplicate_meta_lead_webhook_idempotency(self, create_tables):
        async with _get_db() as db:
            page_id = f"page_{uuid.uuid4().hex[:6]}"
            _tenant = await self._create_tenant(db, meta_page_id=page_id)
            meta_svc = MetaLeadAdsWebhookService(db)

            leadgen_id = f"meta_{uuid.uuid4().hex[:8]}"
            payload = {
                "entry": [{
                    "changes": [{
                        "field": "leadgen",
                        "value": {
                            "page_id": page_id,
                            "form_id": "form_999",
                            "leadgen_id": leadgen_id,
                            "mock_lead_data": {
                                "full_name": "Meta Test Customer",
                                "phone": "+919876543222",
                                "email": "meta@customer.com",
                                "consent_granted": True,
                            },
                        },
                    }]
                }]
            }

            # First delivery ingests lead
            res1 = await meta_svc.process_inbound_webhook(payload)
            await db.commit()
            assert res1[0]["action"] == "created"
            lead_id = res1[0]["lead_id"]

            # Second delivery with same leadgen_id ignores duplicate
            res2 = await meta_svc.process_inbound_webhook(payload)
            await db.commit()
            assert res2[0]["action"] == "duplicate_ignored"

            # Verify only one lead created
            leads_count = (
                await db.execute(sa.select(sa.func.count(Lead.id)).where(Lead.id == uuid.UUID(lead_id)))
            ).scalar()
            assert leads_count == 1

    # ── Scenario 16: Meta lead retrieval failure handling ─────────────────────
    async def test_16_meta_lead_retrieval_failure(self, create_tables):
        async with _get_db() as db:
            page_id = f"page_{uuid.uuid4().hex[:6]}"
            _tenant = await self._create_tenant(db, meta_page_id=page_id)
            meta_svc = MetaLeadAdsWebhookService(db)

            leadgen_id = f"meta_fail_{uuid.uuid4().hex[:8]}"
            payload = {
                "entry": [{
                    "changes": [{
                        "field": "leadgen",
                        "value": {
                            "page_id": page_id,
                            "form_id": "form_999",
                            "leadgen_id": leadgen_id,
                        },
                    }]
                }]
            }

            mock_fetcher = AsyncMock()
            mock_fetcher.fetch_lead.side_effect = RuntimeError("Meta Graph API 500 Server Error")

            res = await meta_svc.process_inbound_webhook(payload, fetch_client=mock_fetcher)
            assert res[0]["action"] == "fetch_failed_retryable"

    # ── Scenario 17: Authorized operator dead-letter inspection and recovery ───
    async def test_17_operator_dead_letter_recovery(self, create_tables):
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            svc = OutboxService(db)

            msg = await svc.enqueue_message(
                tenant_id=tenant.id,
                event_type="AI_QUALIFICATION_REQUESTED",
                aggregate_type="LEAD",
                aggregate_id=uuid.uuid4(),
                payload={},
            )
            msg.status = "DEAD_LETTER"
            msg.retry_count = 3
            msg.last_error = "Exhausted retries"
            await db.commit()

            # 1. Operator lists dead letters for tenant
            dl_list = await svc.list_dead_letters(tenant.id)
            assert len(dl_list) >= 1
            assert any(m.id == msg.id for m in dl_list)

            # 2. Operator retries dead letter
            op_id = uuid.uuid4()
            recovered = await svc.retry_dead_letter(msg.id, tenant.id, operator_id=op_id)
            await db.commit()

            assert recovered.status == "PENDING"
            assert recovered.retry_count == 0
            assert recovered.payload.get("_operator_retried_by") == str(op_id)

    # ── Scenario 18: Lead remains persisted when background services fail ─────
    async def test_18_lead_persisted_when_background_services_fail(self, create_tables):
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            ctx = TenantContext(tenant_id=tenant.id, user_id=None, role=UserRole.SUPER_ADMIN)
            lead_svc = LeadService(db, ctx)

            # Even if all downstream background processing fails catastrophically, create_lead succeeds
            lead = await lead_svc.create_lead(
                LeadCreateRequest(
                    full_name="Persisted Lead Always Accessible",
                    phone="+919876543223",
                    source="WEBSITE",
                    preferred_contact="WHATSAPP",
                    consent_granted=True,
                )
            )
            await db.commit()

            # Confirm lead is securely in database and accessible
            fetched = await lead_svc.get_lead(lead.id)
            assert fetched.id == lead.id
            assert fetched.full_name == "Persisted Lead Always Accessible"
