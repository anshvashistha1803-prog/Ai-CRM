"""
Integration tests for Automatic AI Qualification on Lead Creation.
Verifies end-to-end reliability, atomicity, idempotency, tenant isolation,
and failure-recovery using the dedicated havn_test database.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.dependencies import TenantContext, UserRole
from app.leads.models import Lead
from app.leads.schemas import LeadCreateRequest
from app.leads.service import LeadService
from app.leads.state_machine import LeadStage
from app.messaging.models import OutboxMessage
from app.messaging.outbox_service import OutboxService
from app.qualification.models import LeadQualificationProposal
from app.qualification.service import QualificationService
from app.tasks.celery_tasks import _handle_message_event
from app.tenants.models import Tenant
from app.users.models import User
from tests.conftest import test_engine


def _make_session() -> AsyncSession:
    """Create an isolated AsyncSession bound to test_engine."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    return factory()


@pytest.mark.asyncio
class TestAutomaticQualificationTrigger:
    """Comprehensive test suite for automatic qualification trigger."""

    async def test_01_website_lead_enqueues_qualification_automatically(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 1: A new website lead enqueues qualification automatically and worker generates proposal."""
        resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Rohan Mehta",
                "phone": "+919811122233",
                "email": "rohan.mehta@example.com",
                "source": "WEBSITE",
                "preferred_contact": "WHATSAPP",
                "raw_message_text": "Looking for a 3 BHK luxury apartment in Indiranagar, budget around 2.5 Cr, moving in 2 months.",
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        lead_id = uuid.UUID(data["id"])

        # 1. Verify Outbox event was enqueued in the same database transaction
        outbox_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
        )
        outbox_msg = (await db.execute(outbox_stmt)).scalars().first()
        assert outbox_msg is not None
        assert outbox_msg.tenant_id == test_tenant.id
        assert outbox_msg.idempotency_key == f"qual-start-{lead_id}"
        assert outbox_msg.payload["action"] == "GENERATE_PROPOSAL"
        assert outbox_msg.payload["source"] == "WEBSITE"

        # 2. Worker processes event
        await _handle_message_event(outbox_msg, db=db)
        await db.commit()

        # 3. Verify Proposal created in PENDING_REVIEW status (broker review gate intact)
        prop_stmt = sa.select(LeadQualificationProposal).where(
            LeadQualificationProposal.lead_id == lead_id,
        )
        prop = (await db.execute(prop_stmt)).scalars().first()
        assert prop is not None
        assert prop.status == "PENDING_REVIEW"
        assert prop.proposed_by == "AI"
        assert prop.property_type == "APARTMENT"
        assert prop.bhk == 3

        # 4. Verify Lead transitioned to QUALIFYING, NOT QUALIFIED
        lead_db = await db.get(Lead, lead_id)
        assert lead_db.stage == LeadStage.QUALIFYING.value

    async def test_02_manual_lead_enqueues_qualification_automatically(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 2: A new manually created lead enqueues qualification automatically."""
        resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Ananya Sharma",
                "phone": "+919822233344",
                "source": "MANUAL",
                "purpose": "BUY",
                "bhk": 2,
                "property_type": "APARTMENT",
                "budget_max": 12_000_000,
                "preferred_location": "Whitefield",
            },
        )
        assert resp.status_code == 201, resp.text
        lead_id = uuid.UUID(resp.json()["id"])

        outbox_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
        )
        outbox_msg = (await db.execute(outbox_stmt)).scalars().first()
        assert outbox_msg is not None
        assert outbox_msg.payload["source"] == "MANUAL"

        # Worker processes event
        await _handle_message_event(outbox_msg, db=db)
        await db.commit()

        # Proposal generated
        prop_stmt = sa.select(LeadQualificationProposal).where(
            LeadQualificationProposal.lead_id == lead_id,
        )
        prop = (await db.execute(prop_stmt)).scalars().first()
        assert prop is not None
        assert prop.status == "PENDING_REVIEW"

    async def test_03_all_other_supported_sources_enqueue_same_workflow_event(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 3: Every supported source (META_LEAD_ADS, PORTAL) enqueues the exact same workflow event."""
        sources = ["META_LEAD_ADS", "PORTAL"]
        for idx, src in enumerate(sources):
            resp = await client.post(
                "/api/v1/leads",
                headers={"Authorization": f"Bearer {manager_token}"},
                json={
                    "full_name": f"Lead from {src}",
                    "phone": f"+9198333444{idx:02d}",
                    "source": src,
                    "notes": f"Inquired via {src} campaign",
                },
            )
            assert resp.status_code == 201, resp.text
            lead_id = uuid.UUID(resp.json()["id"])

            outbox_stmt = sa.select(OutboxMessage).where(
                OutboxMessage.aggregate_id == str(lead_id),
                OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
            )
            outbox_msg = (await db.execute(outbox_stmt)).scalars().first()
            assert outbox_msg is not None
            assert outbox_msg.event_type == "AI_QUALIFICATION_REQUESTED"
            assert outbox_msg.payload["action"] == "GENERATE_PROPOSAL"
            assert outbox_msg.payload["source"] == src

    async def test_04_lead_and_outbox_event_commit_atomically(
        self,
        test_tenant: Tenant,
        test_manager: User,
        db: AsyncSession,
    ):
        """Scenario 4: The lead and outbox event commit atomically in the same database transaction."""
        ctx = TenantContext(
            user_id=test_manager.id,
            tenant_id=test_tenant.id,
            role=UserRole.MANAGER,
        )
        lead_svc = LeadService(db, ctx)
        req = LeadCreateRequest(
            full_name="Atomic Tester",
            phone="+919844455566",
            source="WEBSITE",
            notes="Testing atomicity",
        )
        res = await lead_svc.create_lead(req)
        lead_id = res.id
        await db.commit()

        # Confirm both are committed and visible
        lead = await db.get(Lead, lead_id)
        assert lead is not None

        outbox_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
        )
        msg = (await db.execute(outbox_stmt)).scalars().first()
        assert msg is not None
        assert msg.idempotency_key == f"qual-start-{lead_id}"

    async def test_05_failed_transaction_leaves_no_orphaned_event(
        self,
        test_tenant: Tenant,
        test_manager: User,
        db: AsyncSession,
    ):
        """Scenario 5: A failed transaction rolls back completely and does not leave an orphaned outbox event."""
        ctx = TenantContext(
            user_id=test_manager.id,
            tenant_id=test_tenant.id,
            role=UserRole.MANAGER,
        )
        lead_svc = LeadService(db, ctx)

        rolled_back_id = None
        try:
            async with db.begin_nested():
                req = LeadCreateRequest(
                    full_name="Rollback Prospect",
                    phone="+919855566677",
                    source="WEBSITE",
                )
                res = await lead_svc.create_lead(req)
                rolled_back_id = res.id
                # Force an explicit rollback / simulated crash before transaction commit
                raise RuntimeError("Simulated transaction crash after lead creation")
        except RuntimeError:
            pass

        # Verify nothing was persisted in database
        assert rolled_back_id is not None
        lead = await db.get(Lead, rolled_back_id)
        assert lead is None

        outbox_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
            OutboxMessage.idempotency_key == f"qual-start-{rolled_back_id}",
        )
        msg = (await db.execute(outbox_stmt)).scalars().first()
        assert msg is None, "Failed transaction must not leave an orphaned outbox event!"

    async def test_06_duplicate_submissions_do_not_create_duplicate_leads_or_proposals(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 6: Duplicate submissions raise 409 and do not create duplicate leads or outbox events."""
        payload = {
            "full_name": "Deepak Verma",
            "phone": "+919866677788",
            "source": "WEBSITE",
            "notes": "First attempt",
        }

        # First submission succeeds
        resp1 = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json=payload,
        )
        assert resp1.status_code == 201, resp1.text
        lead_id = uuid.UUID(resp1.json()["id"])

        # Second submission with identical phone fails with 409
        resp2 = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json=payload,
        )
        assert resp2.status_code == 409, f"Expected 409 Conflict, got {resp2.status_code}"

        # Verify exactly one outbox message exists
        outbox_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
        )
        messages = (await db.execute(outbox_stmt)).scalars().all()
        assert len(messages) == 1

    async def test_07_repeated_event_delivery_is_idempotent(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 7: Repeated delivery of the same outbox event does not duplicate qualification proposals."""
        resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Idempotent Prospect",
                "phone": "+919877788899",
                "source": "WEBSITE",
                "raw_message_text": "Looking for 2 BHK in Koramangala",
            },
        )
        assert resp.status_code == 201, resp.text
        lead_id = uuid.UUID(resp.json()["id"])

        outbox_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
        )
        msg = (await db.execute(outbox_stmt)).scalars().first()

        # Delivery 1: Processes and creates proposal
        await _handle_message_event(msg, db=db)
        await db.commit()

        # Delivery 2: Repeated delivery/worker retry
        await _handle_message_event(msg, db=db)
        await db.commit()

        # Verify exactly ONE proposal exists
        prop_stmt = sa.select(LeadQualificationProposal).where(
            LeadQualificationProposal.lead_id == lead_id,
        )
        proposals = (await db.execute(prop_stmt)).scalars().all()
        assert len(proposals) == 1, f"Expected exactly 1 proposal, found {len(proposals)}"

    async def test_08_background_worker_retries_failures_correctly(
        self,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 8: Background worker retries failures and eventually dead-letters when max retries exceeded."""
        lead = Lead(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            full_name="Retry Prospect",
            source="WEBSITE",
            preferred_contact="WHATSAPP",
            stage=LeadStage.NEW.value,
            automation_status="ACTIVE",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            last_activity_at=datetime.now(UTC),
        )
        db.add(lead)
        await db.flush()

        msg = OutboxMessage(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            event_type="AI_QUALIFICATION_REQUESTED",
            aggregate_type="LEAD",
            aggregate_id=str(lead.id),
            payload={"action": "GENERATE_PROPOSAL", "lead_id": str(lead.id)},
            status="PENDING",
            retry_count=0,
            max_retries=3,
            scheduled_at=datetime.now(UTC),
            idempotency_key=f"qual-start-{lead.id}",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(msg)
        await db.commit()

        # Worker attempt 1: Mark failed (retry_count 0 -> 1, status -> PENDING)
        outbox_svc = OutboxService(db)
        claimed = await outbox_svc.claim_pending_messages(batch_size=10)
        target = next(m for m in claimed if m.id == msg.id)
        claim_token = (target.payload or {}).get("_claim_token")
        await outbox_svc.mark_failed(
            target.id,
            error_message="Transient network error to LLM adapter",
            claimed_at=target.updated_at,
            claim_token=claim_token,
        )
        await db.commit()

        m1 = await db.get(OutboxMessage, msg.id)
        assert m1.status == "PENDING"
        assert m1.retry_count == 1
        assert "Transient network error" in m1.last_error

        # Worker attempts until max_retries reached -> DEAD_LETTER
        m = await db.get(OutboxMessage, msg.id)
        m.retry_count = 2  # next failure will hit max_retries=3
        m.status = "PROCESSING"
        claim_token = "token-test"
        m.payload = {"_claim_token": claim_token}
        await db.flush()

        await outbox_svc.mark_failed(
            m.id,
            error_message="Persistent failure: max retries exceeded",
            claimed_at=m.updated_at,
            claim_token=claim_token,
        )
        await db.commit()

        m_dead = await db.get(OutboxMessage, msg.id)
        assert m_dead.status == "DEAD_LETTER"
        assert m_dead.retry_count == 3
        assert "Persistent failure" in m_dead.last_error

    async def test_09_tenant_a_lead_cannot_trigger_processing_under_tenant_b(
        self,
        test_tenant: Tenant,
        test_tenant_b: Tenant,
        db: AsyncSession,
    ):
        """Scenario 9: Tenant A's lead cannot trigger processing under Tenant B (strict tenant isolation)."""
        lead_a = Lead(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            full_name="Tenant A Lead",
            source="WEBSITE",
            stage=LeadStage.NEW.value,
            automation_status="ACTIVE",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            last_activity_at=datetime.now(UTC),
        )
        db.add(lead_a)
        await db.flush()

        # Malicious or corrupted outbox event: message tenant_id is Tenant B, but aggregate_id is Tenant A's lead
        cross_tenant_msg = OutboxMessage(
            id=uuid.uuid4(),
            tenant_id=test_tenant_b.id,
            event_type="AI_QUALIFICATION_REQUESTED",
            aggregate_type="LEAD",
            aggregate_id=str(lead_a.id),
            payload={"action": "GENERATE_PROPOSAL", "lead_id": str(lead_a.id)},
            status="PENDING",
            retry_count=0,
            max_retries=3,
            scheduled_at=datetime.now(UTC),
            idempotency_key=f"cross-tenant-{lead_a.id}",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(cross_tenant_msg)
        await db.commit()

        # Worker attempts to process
        await _handle_message_event(cross_tenant_msg, db=db)
        await db.commit()

        # Verify NO proposal was created for either tenant
        prop_stmt = sa.select(LeadQualificationProposal).where(
            LeadQualificationProposal.lead_id == lead_a.id,
        )
        proposals = (await db.execute(prop_stmt)).scalars().all()
        assert len(proposals) == 0, "Cross-tenant processing must NOT create a proposal!"

        # Verify lead_a remains untouched in NEW stage
        lead_refreshed = await db.get(Lead, lead_a.id)
        assert lead_refreshed.stage == LeadStage.NEW.value

    async def test_10_ai_provider_failure_does_not_lose_the_saved_lead(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 10: AI provider failure must not lose or delete the saved lead."""
        resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Resilient Lead",
                "phone": "+919899900011",
                "source": "WEBSITE",
                "notes": "Testing resilient lead preservation on AI crash",
            },
        )
        assert resp.status_code == 201, resp.text
        lead_id = uuid.UUID(resp.json()["id"])

        outbox_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == str(lead_id),
            OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
        )
        msg = (await db.execute(outbox_stmt)).scalars().first()

        # Simulate catastrophic AI provider failure during background extraction
        with patch.object(
            QualificationService,
            "generate_proposal",
            side_effect=RuntimeError("AI Provider 503 Service Unavailable"),
        ), pytest.raises(RuntimeError, match="AI Provider 503"):
            await _handle_message_event(msg, db=db)

        # Confirm the lead STILL exists intact in the database
        lead_db = await db.get(Lead, lead_id)
        assert lead_db is not None
        assert lead_db.full_name == "Resilient Lead"
        assert lead_db.is_deleted is False

    async def test_11_sell_rent_human_handoff_and_broker_review_integrity(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Scenario 11: SELL/RENT handling, human takeover skip, and broker-review gate integrity."""
        # 1. SELL lead intake
        resp_sell = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Suresh Seller",
                "phone": "+919800011122",
                "source": "WEBSITE",
                "purpose": "SELL",
                "property_type": "VILLA",
                "raw_message_text": "I want to sell my 4 BHK villa in Sarjapur, expected price 4 Cr.",
            },
        )
        assert resp_sell.status_code == 201
        sell_lead_id = uuid.UUID(resp_sell.json()["id"])

        msg_sell = (
            await db.execute(
                sa.select(OutboxMessage).where(
                    OutboxMessage.aggregate_id == str(sell_lead_id),
                    OutboxMessage.event_type == "AI_QUALIFICATION_REQUESTED",
                )
            )
        ).scalars().first()

        await _handle_message_event(msg_sell, db=db)
        await db.commit()

        # Verify proposal was created with purpose="SELL" and broker-review gate remains intact
        prop_sell = (
            await db.execute(
                sa.select(LeadQualificationProposal).where(
                    LeadQualificationProposal.lead_id == sell_lead_id,
                )
            )
        ).scalars().first()
        assert prop_sell is not None
        assert prop_sell.purpose == "SELL"
        assert prop_sell.status == "PENDING_REVIEW"

        # 2. Human takeover guard: If lead is flagged HUMAN_TAKEOVER, worker skips AI processing
        lead_human = Lead(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            full_name="Direct Human Client",
            source="WEBSITE",
            stage=LeadStage.NEW.value,
            automation_status="HUMAN_TAKEOVER",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            last_activity_at=datetime.now(UTC),
        )
        db.add(lead_human)
        await db.flush()

        msg_human = OutboxMessage(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            event_type="AI_QUALIFICATION_REQUESTED",
            aggregate_type="LEAD",
            aggregate_id=str(lead_human.id),
            payload={"action": "GENERATE_PROPOSAL", "lead_id": str(lead_human.id)},
            status="PENDING",
            retry_count=0,
            max_retries=3,
            scheduled_at=datetime.now(UTC),
            idempotency_key=f"qual-start-{lead_human.id}",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        db.add(msg_human)
        await db.commit()

        await _handle_message_event(msg_human, db=db)
        await db.commit()

        # Verify no proposal was created because human takeover is active
        prop_human = (
            await db.execute(
                sa.select(LeadQualificationProposal).where(
                    LeadQualificationProposal.lead_id == lead_human.id,
                )
            )
        ).scalars().first()
        assert prop_human is None, "AI qualification must be skipped when automation_status is HUMAN_TAKEOVER"
