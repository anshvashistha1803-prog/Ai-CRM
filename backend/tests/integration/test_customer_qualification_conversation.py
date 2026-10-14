"""
Integration tests for Phase 9: Automatic Customer Qualification Conversation.

Verifies:
1. Website lead creation & event scheduling via transactional outbox.
2. Missing consent or tenant WABA configuration blocks scheduling.
3. Idempotent initial contact scheduling (wa-init-{lead.id}).
4. Inbound replies & turn-by-turn question progression.
5. Form pre-seeding (does not re-ask known criteria).
6. Chronological corrections (updating previously extracted answers).
7. Skipped questions preserve null/None on qualification proposals.
8. Distinct BUY, SELL, and RENT handling (rental out-of-scope declination).
9. Opt-out keywords stop automation and revoke consent.
10. Human takeover request stops automation and alerts broker.
11. Strict preservation of the broker-review gate (stage stays QUALIFYING until broker accepts).
12. Multi-tenant isolation.
13. WhatsApp provider failure retries without losing lead record.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import TenantContext, UserRole
from app.core.security import compute_blind_hash, encrypt_field
from app.leads.models import ConsentRecord, Lead
from app.leads.state_machine import AutomationStatus, LeadStage
from app.messaging.models import OutboxMessage
from app.messaging.outbox_service import OutboxService
from app.qualification.conversation_service import QualificationConversationService
from app.qualification.models import LeadQualificationProposal
from app.qualification.service import QualificationService
from app.tasks.celery_tasks import _handle_message_event
from app.tenants.models import Tenant
from app.users.models import User
from app.webhooks.whatsapp_service import WhatsAppWebhookService


@pytest.mark.asyncio
class TestCustomerQualificationConversation:
    """Comprehensive test suite for Phase 9 automatic qualification conversation."""

    async def test_01_website_lead_with_consent_and_waba_schedules_initial_contact(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 1: Eligible website lead with consent and WABA config automatically schedules intro WhatsApp outreach."""
        test_tenant.whatsapp_phone_number_id = "109876543210"
        test_tenant.settings = {"whatsapp_enabled": True}
        await db.flush()

        resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Aarav Sharma",
                "phone": "+919876543210",
                "email": "aarav.sharma@example.com",
                "source": "WEBSITE",
                "preferred_contact": "WHATSAPP",
                "raw_message_text": "I am interested in buying property in Dehradun",
            },
        )
        assert resp.status_code == 201, resp.text
        lead_id = uuid.UUID(resp.json()["id"])

        # Add explicit active consent record for WhatsApp
        consent = ConsentRecord(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            lead_id=lead_id,
            consent_type="WHATSAPP",
            granted=True,
            channel="WEB_FORM",
            created_at=datetime.now(UTC),
        )
        db.add(consent)
        await db.commit()

        # 1. Fetch AI_QUALIFICATION_REQUESTED outbox event created on lead creation
        stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
        )
        outbox_msg = (await db.execute(stmt)).scalars().first()
        assert outbox_msg is not None

        # 2. Process via background worker
        await _handle_message_event(outbox_msg, db=db)
        await db.commit()

        # 3. Verify Proposal generated in PENDING_REVIEW
        prop_stmt = sa.select(LeadQualificationProposal).where(
            LeadQualificationProposal.lead_id == lead_id,
        )
        proposal = (await db.execute(prop_stmt)).scalars().first()
        assert proposal is not None
        assert proposal.status == "PENDING_REVIEW"

        # 4. Verify WHATSAPP_OUTBOUND_DISPATCH scheduled with template lead_qualification_intro
        wa_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "WHATSAPP_OUTBOUND_DISPATCH",
        )
        wa_msg = (await db.execute(wa_stmt)).scalars().first()
        assert wa_msg is not None
        assert wa_msg.idempotency_key == f"wa-init-{lead_id}"
        assert wa_msg.payload["template_name"] == "lead_qualification_intro"
        assert wa_msg.payload["recipient_phone"] == "+919876543210"
        assert "first_question" in wa_msg.payload

        # 5. Process outbound dispatch through background worker
        provider_id = await _handle_message_event(wa_msg, db=db)
        assert provider_id is not None
        assert provider_id.startswith("wamid.mock.template.")

    async def test_02_missing_consent_blocks_whatsapp_scheduling(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 2: Missing consent blocks initial outbound WhatsApp contact."""
        test_tenant.whatsapp_phone_number_id = "109876543210"
        await db.flush()

        resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "No Consent Lead",
                "phone": "+919876543211",
                "source": "WEBSITE",
                "preferred_contact": "WHATSAPP",
                "raw_message_text": "Looking for villa",
                "consent_granted": False,
            },
        )
        assert resp.status_code == 201
        lead_id = uuid.UUID(resp.json()["id"])
        await db.commit()

        # Process proposal generation
        stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
        )
        msg = (await db.execute(stmt)).scalars().first()
        await _handle_message_event(msg, db=db)
        await db.commit()

        # Verify proposal created, but NO WhatsApp message enqueued
        wa_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "WHATSAPP_OUTBOUND_DISPATCH",
        )
        wa_msg = (await db.execute(wa_stmt)).scalars().first()
        assert wa_msg is None, "WhatsApp dispatch should not be scheduled without consent"

    async def test_03_revoked_consent_blocks_whatsapp_scheduling(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 3: Denied/revoked consent blocks WhatsApp scheduling."""
        test_tenant.whatsapp_phone_number_id = "109876543210"
        await db.flush()

        resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Revoked Consent Lead",
                "phone": "+919876543212",
                "source": "WEBSITE",
                "preferred_contact": "WHATSAPP",
                "raw_message_text": "Looking for plot",
            },
        )
        assert resp.status_code == 201
        lead_id = uuid.UUID(resp.json()["id"])

        consent = ConsentRecord(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            lead_id=lead_id,
            consent_type="WHATSAPP",
            granted=False,
            channel="WEB_FORM",
            created_at=datetime.now(UTC),
        )
        db.add(consent)
        await db.commit()

        stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
        )
        msg = (await db.execute(stmt)).scalars().first()
        await _handle_message_event(msg, db=db)
        await db.commit()

        wa_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "WHATSAPP_OUTBOUND_DISPATCH",
        )
        wa_msg = (await db.execute(wa_stmt)).scalars().first()
        assert wa_msg is None

    async def test_04_missing_waba_config_blocks_whatsapp_scheduling(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 4: Tenant without WABA configuration cannot schedule WhatsApp outbound."""
        test_tenant.whatsapp_phone_number_id = None
        test_tenant.settings = {}
        await db.flush()

        resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Unconfigured Tenant Lead",
                "phone": "+919876543213",
                "source": "WEBSITE",
                "preferred_contact": "WHATSAPP",
                "raw_message_text": "Looking for flat",
            },
        )
        assert resp.status_code == 201
        lead_id = uuid.UUID(resp.json()["id"])

        consent = ConsentRecord(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            lead_id=lead_id,
            consent_type="WHATSAPP",
            granted=True,
            channel="WEB_FORM",
            created_at=datetime.now(UTC),
        )
        db.add(consent)
        await db.commit()

        stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
        )
        msg = (await db.execute(stmt)).scalars().first()
        await _handle_message_event(msg, db=db)
        await db.commit()

        wa_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "WHATSAPP_OUTBOUND_DISPATCH",
        )
        wa_msg = (await db.execute(wa_stmt)).scalars().first()
        assert wa_msg is None

    async def test_05_ineligible_lead_source_blocks_whatsapp_scheduling(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 5: Manual CRM lead does not trigger cold outbound automated WhatsApp outreach."""
        test_tenant.whatsapp_phone_number_id = "109876543210"
        await db.flush()

        resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Manual Entry Lead",
                "phone": "+919876543214",
                "source": "MANUAL",
                "preferred_contact": "WHATSAPP",
                "raw_message_text": "Manual broker walkthrough notes",
            },
        )
        assert resp.status_code == 201
        lead_id = uuid.UUID(resp.json()["id"])

        consent = ConsentRecord(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            lead_id=lead_id,
            consent_type="WHATSAPP",
            granted=True,
            channel="DIRECT_CONSENT",
            created_at=datetime.now(UTC),
        )
        db.add(consent)
        await db.commit()

        stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
        )
        msg = (await db.execute(stmt)).scalars().first()
        await _handle_message_event(msg, db=db)
        await db.commit()

        wa_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "WHATSAPP_OUTBOUND_DISPATCH",
        )
        wa_msg = (await db.execute(wa_stmt)).scalars().first()
        assert wa_msg is None

    async def test_06_idempotency_on_repeated_worker_delivery(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 6: Repeated processing of the same outbox message does not duplicate proposals or WhatsApp dispatches."""
        test_tenant.whatsapp_phone_number_id = "109876543210"
        await db.flush()

        resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Idempotent Lead",
                "phone": "+919876543215",
                "source": "WEBSITE",
                "preferred_contact": "WHATSAPP",
                "raw_message_text": "Interested in 3 BHK flat",
            },
        )
        assert resp.status_code == 201
        lead_id = uuid.UUID(resp.json()["id"])

        consent = ConsentRecord(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            lead_id=lead_id,
            consent_type="WHATSAPP",
            granted=True,
            channel="WEB_FORM",
            created_at=datetime.now(UTC),
        )
        db.add(consent)
        await db.commit()

        stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
        )
        msg = (await db.execute(stmt)).scalars().first()

        # Run 1
        await _handle_message_event(msg, db=db)
        await db.commit()

        # Run 2 (Simulated redelivery/retry)
        await _handle_message_event(msg, db=db)
        await db.commit()

        # Verify exactly 1 proposal exists
        props = (
            await db.execute(
                sa.select(LeadQualificationProposal).where(
                    LeadQualificationProposal.lead_id == lead_id,
                )
            )
        ).scalars().all()
        assert len(props) == 1

        # Verify exactly 1 WHATSAPP_OUTBOUND_DISPATCH exists
        wa_msgs = (
            await db.execute(
                sa.select(OutboxMessage).where(
                    OutboxMessage.aggregate_id == str(lead_id),
                    OutboxMessage.event_type == "WHATSAPP_OUTBOUND_DISPATCH",
                )
            )
        ).scalars().all()
        assert len(wa_msgs) == 1
        assert wa_msgs[0].idempotency_key == f"wa-init-{lead_id}"

    async def test_07_inbound_reply_progression_and_single_question_at_a_time(
        self,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 7: Full conversational progression asking one question at a time and completing."""
        test_tenant.whatsapp_phone_number_id = "109876543210"
        phone = "+919876543216"
        p_hash = compute_blind_hash(phone)
        lead_id = uuid.uuid4()
        now = datetime.now(UTC)

        lead = Lead(
            id=lead_id,
            tenant_id=test_tenant.id,
            full_name="Kavita Reddy",
            phone_e164_enc=encrypt_field(phone),
            phone_hash=p_hash,
            source="WEBSITE",
            preferred_contact="WHATSAPP",
            stage=LeadStage.QUALIFYING.value,
            automation_status=AutomationStatus.ACTIVE.value,
            purpose="BUY",
            property_type="FLAT",
            raw_message_text="Looking for luxury apartment",
            created_at=now,
            updated_at=now,
        )
        db.add(lead)

        consent = ConsentRecord(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            lead_id=lead_id,
            consent_type="WHATSAPP",
            granted=True,
            channel="WEB_FORM",
            created_at=now,
        )
        db.add(consent)

        # Pre-create initial AI qualification proposal
        proposal = LeadQualificationProposal(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            lead_id=lead_id,
            status="PENDING_REVIEW",
            proposed_by="AI",
            property_type="FLAT",
        )
        db.add(proposal)
        await db.commit()

        conv_svc = QualificationConversationService(db)

        # Turn 1: Customer replies specifying BHK
        r1 = await conv_svc.process_inbound_message(lead_id, "We need 3 BHK on high floor", sender_phone=phone)
        assert r1.is_completed is False
        assert "location" in r1.reply_text.lower() or "Rajpur" in r1.reply_text

        # Turn 2: Customer specifies location
        r2 = await conv_svc.process_inbound_message(lead_id, "Prefer Rajpur Road", sender_phone=phone)
        assert r2.is_completed is False
        assert "budget" in r2.reply_text.lower() or "Cr" in r2.reply_text

        # Turn 3: Customer specifies budget
        r3 = await conv_svc.process_inbound_message(lead_id, "Budget is 2.5 Cr", sender_phone=phone)
        assert r3.is_completed is False
        assert "timeline" in r3.reply_text.lower() or "months" in r3.reply_text

        # Turn 4: Customer specifies timeline and purpose -> Completion!
        r4 = await conv_svc.process_inbound_message(lead_id, "Immediate purchase for self residence", sender_phone=phone)
        assert r4.is_completed is True
        assert r4.stage == LeadStage.QUALIFYING.value
        assert r4.automation_status == AutomationStatus.HUMAN_TAKEOVER.value

        # Verify proposal was synced with extracted criteria and kept in PENDING_REVIEW
        await db.refresh(proposal)
        assert proposal.status == "PENDING_REVIEW"
        assert proposal.bhk == 3
        assert proposal.preferred_location == "Rajpur Road"
        assert proposal.budget_max == 25000000
        assert proposal.purpose in ["PRIMARY RESIDENCE", "PRIMARY_RESIDENCE"]
        assert proposal.move_in_timeline == "IMMEDIATE"

        # Verify broker handoff notification was scheduled
        handoff_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "BROKER_HANDOFF_NOTIFICATION",
        )
        handoff_msg = (await db.execute(handoff_stmt)).scalars().first()
        assert handoff_msg is not None

        # Verify MATCH_COMPUTATION_REQUESTED was NOT enqueued automatically
        match_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "MATCH_COMPUTATION_REQUESTED",
        )
        match_msg = (await db.execute(match_stmt)).scalars().first()
        assert match_msg is None, "Matching computation must wait for broker acceptance"

    async def test_08_form_pre_seeding_avoids_re_asking_known_info(
        self,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 8: Lead created with complete form attributes seeds answers and skips already answered questions."""
        test_tenant.whatsapp_phone_number_id = "109876543210"
        phone = "+919876543217"
        lead_id = uuid.uuid4()
        now = datetime.now(UTC)

        lead = Lead(
            id=lead_id,
            tenant_id=test_tenant.id,
            full_name="Vikram Seth",
            phone_e164_enc=encrypt_field(phone),
            phone_hash=compute_blind_hash(phone),
            source="WEBSITE",
            preferred_contact="WHATSAPP",
            stage=LeadStage.QUALIFYING.value,
            automation_status=AutomationStatus.ACTIVE.value,
            purpose="BUY",
            property_type="VILLA",
            bhk=4,
            preferred_location="Mussoorie Road",
            budget_max=40000000,
            raw_message_text="Website form submission with luxury villa preferences",
            created_at=now,
            updated_at=now,
        )
        db.add(lead)

        consent = ConsentRecord(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            lead_id=lead_id,
            consent_type="WHATSAPP",
            granted=True,
            channel="WEB_FORM",
            created_at=now,
        )
        db.add(consent)
        await db.commit()

        conv_svc = QualificationConversationService(db)
        outbox_msg = await conv_svc.schedule_initial_contact(lead_id)
        assert outbox_msg is not None

        # The first question should directly ask for timeline/purpose, NOT property type, BHK, location, or budget!
        first_q = outbox_msg.payload["message_text"].lower()
        assert "timeline" in first_q or "purchase timeline" in first_q

    async def test_09_chronological_corrections_update_proposal(
        self,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 9: Customer corrections in subsequent turns overwrite previous values."""
        phone = "+919876543218"
        lead_id = uuid.uuid4()
        now = datetime.now(UTC)

        lead = Lead(
            id=lead_id,
            tenant_id=test_tenant.id,
            full_name="Pooja Batra",
            phone_e164_enc=encrypt_field(phone),
            phone_hash=compute_blind_hash(phone),
            source="WEBSITE",
            stage=LeadStage.QUALIFYING.value,
            automation_status=AutomationStatus.ACTIVE.value,
            purpose="BUY",
            property_type="FLAT",
            bhk=2,
            created_at=now,
            updated_at=now,
        )
        db.add(lead)

        proposal = LeadQualificationProposal(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            lead_id=lead_id,
            status="PENDING_REVIEW",
            proposed_by="AI",
            property_type="FLAT",
            bhk=2,
        )
        db.add(proposal)
        await db.commit()

        conv_svc = QualificationConversationService(db)

        # Customer changes mind to 3 BHK
        await conv_svc.process_inbound_message(lead_id, "Actually make that 3 BHK flat, we decided we need more space", sender_phone=phone)

        await db.refresh(proposal)
        assert proposal.bhk == 3, "Chronological correction should update proposed BHK to 3"

    async def test_10_skipped_questions_preserve_null_on_proposal(
        self,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 10: Skipping a question keeps proposal and lead fields null."""
        phone = "+919876543219"
        lead_id = uuid.uuid4()
        now = datetime.now(UTC)

        lead = Lead(
            id=lead_id,
            tenant_id=test_tenant.id,
            full_name="Ananya Roy",
            phone_e164_enc=encrypt_field(phone),
            phone_hash=compute_blind_hash(phone),
            source="WEBSITE",
            stage=LeadStage.QUALIFYING.value,
            automation_status=AutomationStatus.ACTIVE.value,
            purpose="BUY",
            property_type="FLAT",
            bhk=3,
            preferred_location="Balbir Road",
            created_at=now,
            updated_at=now,
        )
        # Put state machine at AWAIT_BUDGET
        lead.score_breakdown = {
            "conversation_state": {
                "current_node": "AWAIT_BUDGET",
                "answers": {"intent": "BUY", "property_type": "FLAT", "bhk": 3, "preferred_location": "Balbir Road"},
                "turns_count": 2,
                "language": "EN",
            }
        }
        db.add(lead)

        proposal = LeadQualificationProposal(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            lead_id=lead_id,
            status="PENDING_REVIEW",
            proposed_by="AI",
            property_type="FLAT",
            bhk=3,
            preferred_location="Balbir Road",
            budget_max=None,
        )
        db.add(proposal)
        await db.commit()

        conv_svc = QualificationConversationService(db)

        # Customer skips budget
        resp = await conv_svc.process_inbound_message(lead_id, "skip for now", sender_phone=phone)
        assert resp.is_completed is False

        await db.refresh(proposal)
        assert proposal.budget_max is None, "Skipped budget must preserve null on proposal"

    async def test_11_opt_out_keyword_halts_automation_and_revokes_consent(
        self,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 11: Opt-out request halts automation, revokes consent, and sets DO_NOT_CONTACT."""
        phone = "+919876543220"
        lead_id = uuid.uuid4()
        now = datetime.now(UTC)

        lead = Lead(
            id=lead_id,
            tenant_id=test_tenant.id,
            full_name="Opt Out Tester",
            phone_e164_enc=encrypt_field(phone),
            phone_hash=compute_blind_hash(phone),
            source="WEBSITE",
            stage=LeadStage.QUALIFYING.value,
            automation_status=AutomationStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
        )
        db.add(lead)
        await db.commit()

        conv_svc = QualificationConversationService(db)
        resp = await conv_svc.process_inbound_message(lead_id, "STOP", sender_phone=phone)
        assert resp.automation_status == AutomationStatus.OPTED_OUT.value

        await db.refresh(lead)
        assert lead.do_not_contact is True
        assert lead.stage == LeadStage.DO_NOT_CONTACT.value
        assert lead.automation_status == AutomationStatus.OPTED_OUT.value

        # Verify revoked consent record created
        consent_stmt = sa.select(ConsentRecord).where(
            ConsentRecord.lead_id == lead_id,
            ConsentRecord.granted.is_(False),
        )
        revoked = (await db.execute(consent_stmt)).scalars().first()
        assert revoked is not None

    async def test_12_human_broker_request_pauses_automation_and_notifies_broker(
        self,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 12: Customer requesting a human halts bot questioning and notifies assigned broker."""
        phone = "+919876543221"
        lead_id = uuid.uuid4()
        now = datetime.now(UTC)

        lead = Lead(
            id=lead_id,
            tenant_id=test_tenant.id,
            full_name="Direct Talker",
            phone_e164_enc=encrypt_field(phone),
            phone_hash=compute_blind_hash(phone),
            source="WEBSITE",
            stage=LeadStage.QUALIFYING.value,
            automation_status=AutomationStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
        )
        db.add(lead)
        await db.commit()

        conv_svc = QualificationConversationService(db)
        resp = await conv_svc.process_inbound_message(lead_id, "Please have a human broker call me directly", sender_phone=phone)
        assert resp.is_human_takeover is True
        assert resp.automation_status == AutomationStatus.HUMAN_TAKEOVER.value

        await db.refresh(lead)
        assert lead.automation_status == AutomationStatus.HUMAN_TAKEOVER.value

        # Verify urgent broker handoff notification was queued
        handoff_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "BROKER_HANDOFF_NOTIFICATION",
        )
        msg = (await db.execute(handoff_stmt)).scalars().first()
        assert msg is not None
        assert msg.payload["type"] == "IMMEDIATE_HUMAN_REQUEST"

    async def test_13_distinct_buy_sell_rent_handling(
        self,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 13: Distinct intent branches: Rent is declined, Sell asks listing questions without matching."""
        phone = "+919876543222"
        now = datetime.now(UTC)

        # 1. Rental Enquiry
        rent_lead_id = uuid.uuid4()
        rent_lead = Lead(
            id=rent_lead_id,
            tenant_id=test_tenant.id,
            full_name="Rental Inquirer",
            phone_e164_enc=encrypt_field(phone),
            phone_hash=compute_blind_hash(phone),
            source="WEBSITE",
            stage=LeadStage.QUALIFYING.value,
            automation_status=AutomationStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
        )
        db.add(rent_lead)
        await db.commit()

        conv_svc = QualificationConversationService(db)
        r_resp = await conv_svc.process_inbound_message(rent_lead_id, "I want to rent a flat for 6 months", sender_phone=phone)
        assert r_resp.stage == LeadStage.DISQUALIFIED.value
        assert r_resp.automation_status == AutomationStatus.PAUSED.value
        assert "rental" in r_resp.reply_text.lower() or "rent" in r_resp.reply_text.lower()

        # 2. Seller Enquiry
        seller_lead_id = uuid.uuid4()
        seller_phone = "+919876543229"
        seller_lead = Lead(
            id=seller_lead_id,
            tenant_id=test_tenant.id,
            full_name="Property Seller",
            phone_e164_enc=encrypt_field(seller_phone),
            phone_hash=compute_blind_hash(seller_phone),
            source="WEBSITE",
            stage=LeadStage.QUALIFYING.value,
            automation_status=AutomationStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
        )
        db.add(seller_lead)
        await db.commit()

        s_resp = await conv_svc.process_inbound_message(seller_lead_id, "I want to sell my plot", sender_phone=seller_phone)
        assert s_resp.is_completed is False
        assert "location" in s_resp.reply_text.lower() or "where" in s_resp.reply_text.lower()

    async def test_14_preservation_of_broker_review_gate(
        self,
        test_tenant: Tenant,
        test_manager: User,
        db: AsyncSession,
    ):
        """Scenario 14: Proposal review gate is preserved. Acceptance triggers QUALIFIED and match computation."""
        phone = "+919876543223"
        lead_id = uuid.uuid4()
        now = datetime.now(UTC)

        lead = Lead(
            id=lead_id,
            tenant_id=test_tenant.id,
            full_name="Gate Preservation Tester",
            phone_e164_enc=encrypt_field(phone),
            phone_hash=compute_blind_hash(phone),
            source="WEBSITE",
            stage=LeadStage.QUALIFYING.value,
            automation_status=AutomationStatus.ACTIVE.value,
            purpose="BUY",
            property_type="FLAT",
            bhk=3,
            preferred_location="Rajpur Road",
            budget_max=30000000,
            created_at=now,
            updated_at=now,
        )
        lead.score_breakdown = {
            "conversation_state": {
                "current_node": "AWAIT_TIMELINE",
                "answers": {
                    "intent": "BUY",
                    "property_type": "FLAT",
                    "bhk": 3,
                    "preferred_location": "Rajpur Road",
                    "budget_max": 30000000,
                },
                "turns_count": 3,
                "language": "EN",
            }
        }
        db.add(lead)

        proposal = LeadQualificationProposal(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            lead_id=lead_id,
            status="PENDING_REVIEW",
            proposed_by="AI",
            property_type="FLAT",
            bhk=3,
            preferred_location="Rajpur Road",
            budget_max=30000000,
        )
        db.add(proposal)
        await db.commit()

        conv_svc = QualificationConversationService(db)

        # Complete the conversation turn
        resp = await conv_svc.process_inbound_message(lead_id, "Looking to move in 1-2 months, for self residence", sender_phone=phone)
        assert resp.is_completed is True

        # Gate check: Stage MUST be QUALIFYING, proposal MUST be PENDING_REVIEW
        await db.refresh(lead)
        await db.refresh(proposal)
        assert lead.stage == LeadStage.QUALIFYING.value
        assert proposal.status == "PENDING_REVIEW"

        # Now, human broker reviews and accepts the proposal
        manager_ctx = TenantContext(
            tenant_id=test_tenant.id,
            user_id=test_manager.id,
            role=UserRole.MANAGER,
        )
        qual_svc = QualificationService(db, manager_ctx)
        reviewed_prop = await qual_svc.review_proposal(
            lead_id=lead_id,
            proposal_id=proposal.id,
            action="ACCEPT",
            review_notes="Approved by broker manager",
        )
        await db.commit()

        # Gate release check: Lead stage is now QUALIFIED, proposal is ACCEPTED, and MATCH_COMPUTATION_REQUESTED is enqueued
        await db.refresh(lead)
        assert reviewed_prop.status == "ACCEPTED"
        assert lead.stage == LeadStage.QUALIFIED.value

        match_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "MATCH_COMPUTATION_REQUESTED",
        )
        match_msg = (await db.execute(match_stmt)).scalars().first()
        assert match_msg is not None
        assert match_msg.tenant_id == test_tenant.id

    async def test_15_tenant_isolation_enforcement(
        self,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 15: Cross-tenant isolation blocks unassociated webhook message processing."""
        # Tenant B
        other_tenant = Tenant(
            id=uuid.uuid4(),
            name="Other Agency",
            slug=f"other-agency-{uuid.uuid4().hex[:8]}",
            timezone="UTC",
            currency_code="INR",
            is_active=True,
            whatsapp_phone_number_id="222222222222",
        )
        db.add(other_tenant)

        phone = "+919876543224"
        p_hash = compute_blind_hash(phone)

        # Lead belongs to test_tenant
        lead = Lead(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            full_name="Tenant Isolation Tester",
            phone_e164_enc=encrypt_field(phone),
            phone_hash=p_hash,
            source="WEBSITE",
            stage=LeadStage.QUALIFYING.value,
            automation_status=AutomationStatus.ACTIVE.value,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(lead)
        await db.commit()

        # Incoming webhook arriving at Tenant B's WABA number should NOT touch test_tenant's lead
        webhook_svc = WhatsAppWebhookService(db)
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "222222222222"},
                                "messages": [
                                    {
                                        "from": phone.lstrip("+"),
                                        "id": "wamid.iso.1",
                                        "type": "text",
                                        "text": {"body": "I want to buy in Dehradun"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        events = await webhook_svc.process_inbound_webhook(payload)
        await db.commit()

        # Verify that a new lead was created under other_tenant, and test_tenant's lead remains untouched
        assert len(events) == 1
        created_lead_id = uuid.UUID(events[0]["lead_id"])
        created_lead = await db.get(Lead, created_lead_id)
        assert created_lead.tenant_id == other_tenant.id
        assert created_lead.tenant_id != test_tenant.id

    async def test_16_whatsapp_provider_failure_does_not_lose_lead(
        self,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 16: WhatsApp provider failure retries outbox message without corrupting or deleting lead record."""
        phone = "+919876543225"
        lead_id = uuid.uuid4()
        now = datetime.now(UTC)

        lead = Lead(
            id=lead_id,
            tenant_id=test_tenant.id,
            full_name="Resilience Tester",
            phone_e164_enc=encrypt_field(phone),
            phone_hash=compute_blind_hash(phone),
            source="WEBSITE",
            stage=LeadStage.QUALIFYING.value,
            automation_status=AutomationStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
        )
        db.add(lead)

        consent = ConsentRecord(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            lead_id=lead_id,
            consent_type="WHATSAPP",
            granted=True,
            channel="WEB_FORM",
            created_at=now,
        )
        db.add(consent)

        outbox_svc = OutboxService(db)
        outbox_msg = await outbox_svc.enqueue_message(
            tenant_id=test_tenant.id,
            event_type="WHATSAPP_OUTBOUND_DISPATCH",
            aggregate_type="LEAD",
            aggregate_id=lead_id,
            payload={"recipient_phone": phone, "message_text": "Hello, how can we help?"},
            idempotency_key=f"fail-test-{lead_id}",
        )
        await db.commit()

        # Simulate provider outage
        with (
            patch("app.messaging.whatsapp_client.WhatsAppClient.send_text_message", side_effect=RuntimeError("Provider 503 Outage")),
            pytest.raises(RuntimeError, match="Provider 503 Outage"),
        ):
            await _handle_message_event(outbox_msg, db=db)


        # Lead record is preserved
        persisted_lead = await db.get(Lead, lead_id)
        assert persisted_lead is not None
        assert persisted_lead.full_name == "Resilience Tester"
