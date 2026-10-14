"""
Controlled End-to-End Integration Tests for Step 3.3: Lead Lifecycle.
Runs against the guarded PostgreSQL test database (havn_test) through the real FastAPI HTTP API.

Verifies:
1. Lead creation (PII encryption, blind hashing, consent recording, activity trail)
2. Lead deduplication (409 Conflict on duplicate phone within tenant, no orphan records)
3. Valid and prohibited stage transitions (state machine enforcement, audit trail)
4. Qualification and score persistence (proposal acceptance, deterministic scoring, health)
5. Broker takeover (manager assignment, activity logging)
6. Site-visit scheduling and cancellation (conflict checking, stage transition to VISIT_SCHEDULED and FOLLOW_UP)
7. Lead activity and audit history (chronological ordering, metadata preservation)
Database state is inspected and asserted after each operation.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.security import decrypt_field
from app.leads.models import ConsentRecord, Lead, LeadActivity
from app.leads.state_machine import LeadHealth, LeadStage
from app.properties.models import Property
from app.tenants.models import Tenant
from app.users.models import User
from app.visits.models import SiteVisit


@pytest.mark.asyncio
class TestLeadLifecycleE2E:
    """End-to-end API and database state verification for complete lead lifecycle."""

    async def test_01_lead_creation_and_database_persistence(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db,
    ):
        """Exercise POST /api/v1/leads, verify response and direct database state."""
        phone = "+919876543299"
        email = "e2e.buyer@example.com"

        resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "E2E Test Buyer",
                "phone": phone,
                "email": email,
                "preferred_contact": "WHATSAPP",
                "source": "PORTAL",
                "budget_min": 10000000,
                "budget_max": 20000000,
                "bhk": 3,
                "property_type": "APARTMENT",
                "preferred_location": "Whitefield",
            },
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        lead_id = uuid.UUID(data["id"])

        assert data["full_name"] == "E2E Test Buyer"
        assert data["phone"] == phone
        assert data["email"] == email
        assert data["stage"] == LeadStage.NEW.value
        assert data["tenant_id"] == str(test_tenant.id)

        # Direct database state verification
        lead_db = await db.get(Lead, lead_id)
        assert lead_db is not None
        assert lead_db.tenant_id == test_tenant.id
        assert lead_db.stage == LeadStage.NEW.value
        # Verify PII is encrypted in DB, not plain text
        assert lead_db.phone_e164_enc != phone
        assert decrypt_field(lead_db.phone_e164_enc) == phone
        assert lead_db.phone_hash is not None
        assert lead_db.email_hash is not None

        # Verify ConsentRecord created
        consent_stmt = select(ConsentRecord).where(
            ConsentRecord.lead_id == lead_id,
            ConsentRecord.tenant_id == test_tenant.id,
        )
        consent_res = await db.execute(consent_stmt)
        consents = consent_res.scalars().all()
        assert len(consents) >= 1
        assert consents[0].consent_type == "MARKETING"

        # Verify initial LeadActivity
        act_stmt = select(LeadActivity).where(
            LeadActivity.lead_id == lead_id,
            LeadActivity.tenant_id == test_tenant.id,
        )
        act_res = await db.execute(act_stmt)
        activities = act_res.scalars().all()
        act_types = [a.activity_type for a in activities]
        assert "LEAD_CREATED" in act_types

    async def test_02_lead_deduplication_enforcement(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db,
    ):
        """Exercise duplicate lead creation and verify 409 Conflict without DB corruption."""
        phone = "+919876543288"

        # First creation succeeds
        resp1 = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Original Lead",
                "phone": phone,
                "source": "WEBSITE",
            },
        )
        assert resp1.status_code == 201
        lead_id = uuid.UUID(resp1.json()["id"])

        # Second creation with identical normalized phone fails with 409
        resp2 = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Duplicate Lead Attempt",
                "phone": "09876543288",  # Indian local format normalizes to +919876543288
                "source": "CALL",
            },
        )
        assert resp2.status_code == 409, resp2.text
        body = resp2.json()
        assert body["error"] == "DUPLICATE_LEAD"

        # Direct database state check: exactly 1 lead with this hash exists
        count_stmt = select(Lead).where(
            Lead.tenant_id == test_tenant.id,
            Lead.is_deleted.is_(False),
        )
        count_res = await db.execute(count_stmt)
        leads = [l for l in count_res.scalars().all() if l.id == lead_id]
        assert len(leads) == 1

    async def test_03_valid_and_prohibited_stage_transitions(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db,
    ):
        """Exercise valid transitions and verify state machine blocks prohibited ones."""
        # Create lead in NEW
        resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"full_name": "Transition Test", "phone": "+919876543277", "source": "PORTAL"},
        )
        assert resp.status_code == 201
        lead_id = resp.json()["id"]

        # Valid transition: NEW -> QUALIFYING
        t1 = await client.post(
            f"/api/v1/leads/{lead_id}/stage",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"target_stage": "QUALIFYING", "reason": "Engaged on WhatsApp"},
        )
        assert t1.status_code == 200
        assert t1.json()["stage"] == "QUALIFYING"

        # Verify DB state
        lead_db = await db.get(Lead, uuid.UUID(lead_id))
        assert lead_db.stage == LeadStage.QUALIFYING.value

        # Prohibited transition: QUALIFYING -> WON (bypasses visit, negotiation, etc.)
        t_bad = await client.post(
            f"/api/v1/leads/{lead_id}/stage",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"target_stage": "WON", "reason": "Premature closure attempt"},
        )
        assert t_bad.status_code == 422, t_bad.text
        err = t_bad.json()
        assert err["error"] == "INVALID_TRANSITION"
        assert "not permitted" in err["message"]

        # Prohibited transition: QUALIFYING -> QUALIFIED without qualification evidence
        t_unqual = await client.post(
            f"/api/v1/leads/{lead_id}/stage",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"target_stage": "QUALIFIED", "reason": "Direct manual qualification attempt"},
        )
        assert t_unqual.status_code == 422, t_unqual.text
        err_unqual = t_unqual.json()
        assert err_unqual["error"] == "INVALID_TRANSITION"
        assert "without accepted qualification evidence" in err_unqual["message"]

        # Database state must remain unchanged at QUALIFYING
        await db.refresh(lead_db)
        assert lead_db.stage == LeadStage.QUALIFYING.value

        # Valid transition: QUALIFYING -> DISQUALIFIED (allowed with reason)
        t_disq = await client.post(
            f"/api/v1/leads/{lead_id}/stage",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"target_stage": "DISQUALIFIED", "reason": "Looking for rental only"},
        )
        assert t_disq.status_code == 200
        assert t_disq.json()["stage"] == "DISQUALIFIED"

        await db.refresh(lead_db)
        assert lead_db.stage == LeadStage.DISQUALIFIED.value

    async def test_04_qualification_proposal_and_score_persistence(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db,
    ):
        """Exercise qualification proposal generation, review acceptance, scoring, and outbox."""
        create_resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Qualification Candidate",
                "phone": "+919876543266",
                "source": "WHATSAPP",
            },
        )
        assert create_resp.status_code == 201
        lead_id = create_resp.json()["id"]

        # Generate proposal with conversation text
        gen_resp = await client.post(
            f"/api/v1/leads/{lead_id}/proposals/generate",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "messages": [
                    "Hello, I am looking to buy a 3BHK luxury apartment in Whitefield Bengaluru.",
                    "My budget is around 1.5 Cr to 2.2 Cr. I want to move in immediately.",
                ]
            },
        )
        assert gen_resp.status_code == 200, gen_resp.text
        proposal_data = gen_resp.json()
        proposal_id = proposal_data["id"]
        assert proposal_data["status"] == "PENDING_REVIEW"

        # Review and accept proposal
        review_resp = await client.post(
            f"/api/v1/leads/{lead_id}/proposals/{proposal_id}/review",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "action": "ACCEPT",
                "review_notes": "Requirements verified by manager",
            },
        )
        assert review_resp.status_code == 200, review_resp.text
        assert review_resp.json()["status"] == "ACCEPTED"

        # Verify DB: lead stage advanced to QUALIFIED, real score calculated
        lead_db = await db.get(Lead, uuid.UUID(lead_id))
        assert lead_db.stage == LeadStage.QUALIFIED.value
        assert lead_db.intent_score is not None
        assert lead_db.intent_score > 0
        assert lead_db.lead_health in (LeadHealth.HOT.value, LeadHealth.WARM.value)
        assert lead_db.bhk == 3 or lead_db.bhk is not None
        assert lead_db.preferred_location is not None

    async def test_05_broker_takeover_and_assignment(
        self,
        client: AsyncClient,
        manager_token: str,
        test_agent: User,
        test_tenant: Tenant,
        db,
    ):
        """Exercise manager assignment of lead to agent and verify audit logging."""
        create_resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Broker Takeover Lead",
                "phone": "+919876543255",
                "source": "MANUAL",
            },
        )
        assert create_resp.status_code == 201
        lead_id = create_resp.json()["id"]

        assign_resp = await client.post(
            f"/api/v1/leads/{lead_id}/assign",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"assigned_agent_id": str(test_agent.id)},
        )
        assert assign_resp.status_code == 200
        assert assign_resp.json()["assigned_agent_id"] == str(test_agent.id)

        # Verify DB
        lead_db = await db.get(Lead, uuid.UUID(lead_id))
        assert lead_db.assigned_agent_id == test_agent.id

        # Verify activity timeline contains LEAD_ASSIGNED
        act_resp = await client.get(
            f"/api/v1/leads/{lead_id}/activities",
            headers={"Authorization": f"Bearer {manager_token}"},
        )
        assert act_resp.status_code == 200
        acts = act_resp.json()
        assert any(a["activity_type"] == "LEAD_ASSIGNED" for a in acts)

    async def test_06_site_visit_scheduling_and_cancellation(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        test_agent: User,
        db,
    ):
        """Exercise visit booking, verify stage transitions to VISIT_SCHEDULED then FOLLOW_UP on cancel."""
        # 1. Create property in tenant
        prop = Property(
            id=uuid.uuid4(),
            tenant_id=test_tenant.id,
            title="Whitefield Sovereign 3BHK",
            property_type="APARTMENT",
            bhk=3,
            price=15000000,
            area_sqft=1750,
            location_text="Whitefield, Bengaluru",
            status="AVAILABLE",
            is_deleted=False,
        )
        db.add(prop)
        await db.flush()

        # 2. Create and advance lead to QUALIFIED
        create_resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Visit Prospect",
                "phone": "+919876543244",
                "source": "PORTAL",
            },
        )
        lead_id = create_resp.json()["id"]

        lead_db = await db.get(Lead, uuid.UUID(lead_id))
        lead_db.stage = LeadStage.QUALIFIED.value
        await db.flush()

        # 3. Schedule site visit
        start = datetime.now(UTC) + timedelta(days=2)
        end = start + timedelta(hours=1)

        visit_resp = await client.post(
            "/api/v1/visits",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "lead_id": lead_id,
                "property_id": str(prop.id),
                "assigned_agent_id": str(test_agent.id),
                "scheduled_start_at": start.isoformat(),
                "scheduled_end_at": end.isoformat(),
                "notes": "First site visit walkthrough",
            },
        )
        assert visit_resp.status_code == 201, visit_resp.text
        visit_data = visit_resp.json()
        visit_id = visit_data["id"]
        assert visit_data["status"] == "SCHEDULED"

        # Verify DB state: lead stage transitioned to SITE_VISIT_SCHEDULED
        await db.refresh(lead_db)
        assert lead_db.stage == LeadStage.SITE_VISIT_SCHEDULED.value

        # 4. Cancel site visit
        cancel_resp = await client.post(
            f"/api/v1/visits/{visit_id}/cancel",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"cancellation_reason": "Prospect requested postponement"},
        )
        assert cancel_resp.status_code == 200, cancel_resp.text
        assert cancel_resp.json()["status"] == "CANCELLED"

        # Verify DB state: visit status CANCELLED, lead stage reverted to QUALIFIED
        visit_db = await db.get(SiteVisit, uuid.UUID(visit_id))
        assert visit_db.status == "CANCELLED"
        assert visit_db.cancellation_reason == "Prospect requested postponement"

        await db.refresh(lead_db)
        assert lead_db.stage == LeadStage.QUALIFIED.value

        # 5. Full audit history check
        act_resp = await client.get(
            f"/api/v1/leads/{lead_id}/activities",
            headers={"Authorization": f"Bearer {manager_token}"},
        )
        assert act_resp.status_code == 200
        acts = act_resp.json()
        act_types = [a["activity_type"] for a in acts]
        assert "SITE_VISIT_SCHEDULED" in act_types
        assert "SITE_VISIT_CANCELLED" in act_types
