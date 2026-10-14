"""
Phase 7: End-to-End Workflow Acceptance Tests.
Demonstrates the complete synthetic buyer-lead lifecycle through the actual application
API, state machine, scoring engine, transactional outbox, and background worker.

Target: havn_test dedicated test database.
"""

import os
import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.leads.models import ConsentRecord, Lead, LeadActivity
from app.leads.state_machine import LeadHealth, LeadStage
from app.matching.models import LeadPropertyMatch
from app.messaging.models import OutboxMessage
from app.messaging.outbox_service import OutboxService
from app.notifications.models import Notification
from app.properties.models import Property
from app.tasks.celery_tasks import _handle_message_event
from app.tenants.models import Tenant
from tests.conftest import test_engine


def _make_session() -> AsyncSession:
    """Create an isolated AsyncSession bound to test_engine."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    return factory()


@pytest.mark.asyncio
class TestPhase7EndToEndWorkflowAcceptance:
    """End-to-End Workflow Acceptance Suite for HAVN Realty Phase 7."""

    async def _create_test_property(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        title: str,
        price: int,
        property_type: str = "APARTMENT",
        bhk: int = 3,
        location: str = "Whitefield",
        status: str = "AVAILABLE",
    ) -> Property:
        prop = Property(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            external_id=f"PROP-{uuid.uuid4().hex[:8].upper()}",
            title=title,
            property_type=property_type,
            bhk=bhk,
            price=price,
            currency_code="INR",
            location_text=location,
            area_sqft=1850,
            status=status,
            amenities=["Parking", "Clubhouse", "Swimming Pool"],
            photos=[],
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            is_deleted=False,
        )
        session.add(prop)
        await session.flush()
        return prop

    async def test_01_verify_test_database_isolation_and_safety_guards(self):
        """Criterion 1 & 2: Explicitly verify test database operations target havn_test, never havn_crm."""
        test_db_url = os.getenv("TEST_DATABASE_URL")
        assert test_db_url is not None, "TEST_DATABASE_URL environment variable must be set"

        test_url = make_url(test_db_url)
        app_url = make_url(get_settings().database_url)

        # 1. Target database MUST be exactly 'havn_test'
        assert test_url.database == "havn_test", f"Expected havn_test, got {test_url.database}"

        # 2. Application database MUST NOT be equal to test database
        assert test_url.database != app_url.database, (
            f"Test database ({test_url.database}) must not match application database ({app_url.database})"
        )

        # 3. Verify safety guard: conftest refuses to run if test URL matches application URL
        assert not (
            test_url.host == app_url.host
            and test_url.port == app_url.port
            and test_url.database == app_url.database
        ), "Safety violation: test_engine is pointing at the application database!"

    async def test_02_create_synthetic_buyer_lead_and_verify_persistence(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Criterion 3: Create a synthetic BUY lead for a test tenant with encryption and consent."""
        payload = {
            "full_name": "Aarav Sharma",
            "phone": "+919876543210",
            "email": "aarav.sharma@example.com",
            "source": "WHATSAPP",
            "preferred_contact": "WHATSAPP",
        }

        resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json=payload,
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["full_name"] == "Aarav Sharma"
        assert data["stage"] == "NEW"
        assert data["phone"] == "+919876543210"

        lead_id = uuid.UUID(data["id"])

        # Direct DB verification
        lead_db = await db.get(Lead, lead_id)
        assert lead_db is not None
        assert lead_db.tenant_id == test_tenant.id
        assert lead_db.stage == LeadStage.NEW.value
        assert lead_db.phone_e164_enc != "+919876543210"  # PII must be encrypted
        assert lead_db.phone_hash is not None  # Blind hash must exist for deduplication

        # Consent record verification
        consent_stmt = sa.select(ConsentRecord).where(ConsentRecord.lead_id == lead_id)
        consent = (await db.execute(consent_stmt)).scalars().first()
        assert consent is not None
        assert consent.consent_type == "MARKETING"

        # Activity verification
        act_stmt = sa.select(LeadActivity).where(
            LeadActivity.lead_id == lead_id,
            LeadActivity.activity_type == "LEAD_CREATED",
        )
        act = (await db.execute(act_stmt)).scalars().first()
        assert act is not None

    async def test_03_synthetic_messages_proposal_generation_and_invalid_transition_rejection(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Criterion 4 & 10: Submit synthetic messages, generate proposal, and reject premature qualification."""
        # 1. Create lead
        create_resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Priya Nair",
                "phone": "+919876543211",
                "source": "WHATSAPP",
            },
        )
        lead_id = create_resp.json()["id"]

        # 2. Advance to QUALIFYING stage
        t_stage = await client.post(
            f"/api/v1/leads/{lead_id}/stage",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"target_stage": "QUALIFYING", "reason": "Started WhatsApp chat"},
        )
        assert t_stage.status_code == 200
        assert t_stage.json()["stage"] == "QUALIFYING"

        # 3. Generate qualification proposal with synthetic conversation
        messages = [
            "Hi HAVN, I am looking to buy a 3 BHK luxury apartment in Whitefield Bengaluru.",
            "My budget is around 1.5 Cr to 2.2 Cr. I am ready to move in immediately.",
        ]
        gen_resp = await client.post(
            f"/api/v1/leads/{lead_id}/proposals/generate",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"messages": messages},
        )
        assert gen_resp.status_code == 200, gen_resp.text
        proposal_data = gen_resp.json()
        assert proposal_data["status"] == "PENDING_REVIEW"
        assert proposal_data["property_type"] == "APARTMENT"
        assert proposal_data["bhk"] == 3
        assert proposal_data["budget_min"] == 10000000
        assert proposal_data["budget_max"] == 15000000
        assert proposal_data["preferred_location"] == "Whitefield"
        assert proposal_data["purpose"] == "BUY"
        assert proposal_data["move_in_timeline"] == "IMMEDIATE"

        # 4. CRITICAL INTEGRITY CHECK: Attempt manual QUALIFYING -> QUALIFIED transition
        # while proposal is still PENDING_REVIEW (must be REJECTED with 422)
        t_invalid = await client.post(
            f"/api/v1/leads/{lead_id}/stage",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"target_stage": "QUALIFIED", "reason": "Attempting premature qualification"},
        )
        assert t_invalid.status_code == 422, t_invalid.text
        err = t_invalid.json()
        assert err["error"] == "INVALID_TRANSITION"
        assert "without accepted qualification evidence" in err["message"]

        # 5. Verify database state is untouched
        lead_db = await db.get(Lead, uuid.UUID(lead_id))
        assert lead_db.stage == LeadStage.QUALIFYING.value
        assert lead_db.intent_score is None

        # 6. Verify NO outbox message was enqueued
        outbox_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == lead_id,
            OutboxMessage.event_type == "MATCH_COMPUTATION_REQUESTED",
        )
        assert (await db.execute(outbox_stmt)).scalar_one_or_none() is None

    async def test_04_review_and_accept_proposal_verifying_score_and_health_persistence(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Criterion 5 & 6: Review and accept proposal; verify persisted score, health, and criteria."""
        # Create and qualify lead
        lead_resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"full_name": "Vikram Seth", "phone": "+919876543212", "source": "PORTAL"},
        )
        lead_id = lead_resp.json()["id"]

        gen_resp = await client.post(
            f"/api/v1/leads/{lead_id}/proposals/generate",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"messages": ["Looking to buy 3 BHK apartment in Whitefield budget 1.5 Cr immediate move in"]},
        )
        proposal_id = gen_resp.json()["id"]

        # Review and accept proposal
        review_resp = await client.post(
            f"/api/v1/leads/{lead_id}/proposals/{proposal_id}/review",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"action": "ACCEPT", "review_notes": "Requirements verified with Vikram over phone"},
        )
        assert review_resp.status_code == 200
        assert review_resp.json()["status"] == "ACCEPTED"

        # Verify DB: stage advanced, score evaluated deterministically, health rating persisted
        lead_db = await db.get(Lead, uuid.UUID(lead_id))
        assert lead_db.stage == LeadStage.QUALIFIED.value
        assert lead_db.intent_score is not None
        assert lead_db.intent_score > 0
        assert lead_db.lead_health in (LeadHealth.HOT.value, LeadHealth.WARM.value)
        assert lead_db.lead_health != "UNKNOWN"
        assert lead_db.score_breakdown is not None
        assert "components" in lead_db.score_breakdown

        # Criteria fields populated on lead record
        assert lead_db.property_type == "APARTMENT"
        assert lead_db.bhk == 3
        assert lead_db.budget_max == 15000000
        assert lead_db.preferred_location == "Whitefield"

        # Outbox event MATCH_COMPUTATION_REQUESTED created atomically
        outbox_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == lead_id,
            OutboxMessage.event_type == "MATCH_COMPUTATION_REQUESTED",
        )
        outbox_msg = (await db.execute(outbox_stmt)).scalar_one_or_none()
        assert outbox_msg is not None
        assert outbox_msg.status == "PENDING"
        assert outbox_msg.idempotency_key == f"match-compute-{lead_id}"
        assert outbox_msg.payload["lead_id"] == lead_id
        assert outbox_msg.payload["score"] == lead_db.intent_score

    async def test_05_outbox_event_worker_processing_and_available_property_filtering(
        self,
        create_tables: None,
    ):
        """Criterion 7 & 8: Verify worker processes outbox event and matches only AVAILABLE properties within tenant."""
        async with _make_session() as session:
            tenant_a = Tenant(
                id=uuid.uuid4(), name="Agency Alpha", slug=f"alpha-{uuid.uuid4().hex[:6]}",
                timezone="Asia/Kolkata", currency_code="INR", is_active=True, settings={},
            )
            tenant_b = Tenant(
                id=uuid.uuid4(), name="Agency Beta", slug=f"beta-{uuid.uuid4().hex[:6]}",
                timezone="Asia/Kolkata", currency_code="INR", is_active=True, settings={},
            )
            session.add_all([tenant_a, tenant_b])
            await session.flush()

            # Qualified Lead in Tenant A
            lead = Lead(
                id=uuid.uuid4(), tenant_id=tenant_a.id, full_name="Siddharth Verma",
                source="WHATSAPP", preferred_contact="WHATSAPP",
                budget_min=15_000_000, budget_max=22_000_000, currency_code="INR",
                property_type="APARTMENT", bhk=3, preferred_location="Whitefield",
                purpose="BUY", move_in_timeline="IMMEDIATE", stage="QUALIFIED",
                lead_health="HOT", intent_score=85, automation_status="ACTIVE",
                do_not_contact=False, is_deleted=False,
                created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
            )
            session.add(lead)
            await session.flush()

            # Properties for Tenant A
            # Prop 1: AVAILABLE, 3 BHK, Whitefield, 1.8 Cr -> MATCH
            prop_avail = await self._create_test_property(
                session, tenant_a.id, "Prestige Lakeside 3BHK", price=18_000_000,
                property_type="APARTMENT", bhk=3, location="Whitefield", status="AVAILABLE",
            )
            # Prop 2: SOLD, 3 BHK, Whitefield, 1.8 Cr -> EXCLUDED
            prop_sold = await self._create_test_property(
                session, tenant_a.id, "Prestige SOLD 3BHK", price=18_000_000,
                property_type="APARTMENT", bhk=3, location="Whitefield", status="SOLD",
            )
            # Prop 3: RESERVED, 3 BHK, Whitefield, 1.8 Cr -> EXCLUDED
            prop_reserved = await self._create_test_property(
                session, tenant_a.id, "Prestige RESERVED 3BHK", price=18_000_000,
                property_type="APARTMENT", bhk=3, location="Whitefield", status="RESERVED",
            )
            # Prop 4: OFF_MARKET, 3 BHK, Whitefield, 1.8 Cr -> EXCLUDED
            prop_off_market = await self._create_test_property(
                session, tenant_a.id, "Prestige OFF_MARKET 3BHK", price=18_000_000,
                property_type="APARTMENT", bhk=3, location="Whitefield", status="OFF_MARKET",
            )
            # Prop 5: AVAILABLE in Tenant B (Foreign) -> EXCLUDED (multi-tenant isolation)
            prop_foreign = await self._create_test_property(
                session, tenant_b.id, "Tenant B Foreign 3BHK", price=18_000_000,
                property_type="APARTMENT", bhk=3, location="Whitefield", status="AVAILABLE",
            )

            # Enqueue Outbox Message
            outbox_msg = OutboxMessage(
                id=uuid.uuid4(),
                tenant_id=tenant_a.id,
                event_type="MATCH_COMPUTATION_REQUESTED",
                aggregate_type="LEAD",
                aggregate_id=str(lead.id),
                payload={"lead_id": str(lead.id), "score": 85, "health": "HOT"},
                status="PENDING",
                retry_count=0,
                max_retries=3,
                scheduled_at=datetime.now(UTC),
                idempotency_key=f"match-compute-{lead.id}",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add(outbox_msg)
            await session.commit()

        # Execute Background Worker: claim, dispatch, mark sent
        async with _make_session() as session:
            outbox_svc = OutboxService(session)
            claimed = await outbox_svc.claim_pending_messages(batch_size=10)
            assert len(claimed) == 1
            claimed_msg = claimed[0]
            assert claimed_msg.id == outbox_msg.id

            # Dispatch event through worker handler
            await _handle_message_event(claimed_msg, db=session)

            # Mark sent in outbox
            claim_token = (claimed_msg.payload or {}).get("_claim_token")
            await outbox_svc.mark_sent(
                claimed_msg.id,
                claimed_at=claimed_msg.updated_at,
                claim_token=claim_token,
            )
            await session.commit()

        # Verification of Match Results and Status Gating
        async with _make_session() as session:
            # 1. Outbox status is now SENT
            updated_msg = await session.get(OutboxMessage, outbox_msg.id)
            assert updated_msg.status == "SENT"
            assert updated_msg.processed_at is not None

            # 2. Match records in database
            matches_stmt = sa.select(LeadPropertyMatch).where(
                LeadPropertyMatch.lead_id == lead.id
            )
            matches = (await session.execute(matches_stmt)).scalars().all()

            # Exactly ONE match produced: prop_avail
            assert len(matches) == 1
            match = matches[0]
            assert match.property_id == prop_avail.id
            assert match.status == "SUGGESTED"
            assert match.match_score >= 80.0
            assert "location" in match.match_reasons or "budget" in match.match_reasons

            # Strictly verify excluded properties
            matched_prop_ids = [m.property_id for m in matches]
            assert prop_sold.id not in matched_prop_ids
            assert prop_reserved.id not in matched_prop_ids
            assert prop_off_market.id not in matched_prop_ids
            assert prop_foreign.id not in matched_prop_ids

    async def test_06_accessible_through_existing_api_endpoints(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Criterion 9: Verify resulting lead, matches, proposals, and timeline are accessible via API."""
        # Create, qualify, and match lead
        lead_resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"full_name": "Ananya Roy", "phone": "+919876543213", "source": "WEBSITE"},
        )
        lead_id = lead_resp.json()["id"]

        # Add matching property
        prop = await self._create_test_property(
            db, test_tenant.id, "Ananya Matched Flat", price=16_000_000,
            property_type="APARTMENT", bhk=3, location="Whitefield", status="AVAILABLE",
        )
        await db.flush()

        # Generate and accept proposal
        gen = await client.post(
            f"/api/v1/leads/{lead_id}/proposals/generate",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"messages": ["Looking for 3 BHK in Whitefield budget 1.6 Cr"]},
        )
        prop_id = gen.json()["id"]

        await client.post(
            f"/api/v1/leads/{lead_id}/proposals/{prop_id}/review",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"action": "ACCEPT"},
        )

        # Trigger match worker
        outbox_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == lead_id,
            OutboxMessage.event_type == "MATCH_COMPUTATION_REQUESTED",
        )
        msg = (await db.execute(outbox_stmt)).scalar_one()
        await _handle_message_event(msg, db=db)
        await db.flush()

        # 1. GET /api/v1/leads/{id}
        get_lead = await client.get(
            f"/api/v1/leads/{lead_id}",
            headers={"Authorization": f"Bearer {manager_token}"},
        )
        assert get_lead.status_code == 200
        l_data = get_lead.json()
        assert l_data["stage"] == "QUALIFIED"
        assert l_data["phone"] == "+919876543213"
        assert l_data["intent_score"] is not None

        # 2. GET /api/v1/leads/{id}/matches
        get_matches = await client.get(
            f"/api/v1/leads/{lead_id}/matches",
            headers={"Authorization": f"Bearer {manager_token}"},
        )
        assert get_matches.status_code == 200
        matches_list = get_matches.json()
        assert len(matches_list) >= 1
        assert matches_list[0]["property_id"] == str(prop.id)
        assert matches_list[0]["match_score"] > 0

        # 3. GET /api/v1/leads/{id}/proposals
        get_props = await client.get(
            f"/api/v1/leads/{lead_id}/proposals",
            headers={"Authorization": f"Bearer {manager_token}"},
        )
        assert get_props.status_code == 200
        props_list = get_props.json()
        assert len(props_list) == 1
        assert props_list[0]["status"] == "ACCEPTED"

        # 4. GET /api/v1/leads/{id}/activities
        get_acts = await client.get(
            f"/api/v1/leads/{lead_id}/activities",
            headers={"Authorization": f"Bearer {manager_token}"},
        )
        assert get_acts.status_code == 200
        acts = get_acts.json()
        act_types = [a["activity_type"] for a in acts]
        assert "LEAD_CREATED" in act_types
        assert "PROPOSAL_ACCEPTED" in act_types

    async def test_07_idempotency_and_duplicate_request_safety(
        self,
        client: AsyncClient,
        manager_token: str,
        test_tenant: Tenant,
        db: AsyncSession,
    ):
        """Criterion 11 (Part A): Repeated stage transitions do not duplicate outbox events or mutate score."""
        create = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"full_name": "Idempotent Lead", "phone": "+919876543214", "source": "PORTAL"},
        )
        lead_id = create.json()["id"]

        gen = await client.post(
            f"/api/v1/leads/{lead_id}/proposals/generate",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"messages": ["Looking for 2 BHK in Whitefield 1 Cr"]},
        )
        prop_id = gen.json()["id"]

        await client.post(
            f"/api/v1/leads/{lead_id}/proposals/{prop_id}/review",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"action": "ACCEPT"},
        )

        # Call transition_stage again for QUALIFIED
        repeat_resp = await client.post(
            f"/api/v1/leads/{lead_id}/stage",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"target_stage": "QUALIFIED", "reason": "Repeated transition call"},
        )
        assert repeat_resp.status_code == 200

        # Outbox messages for MATCH_COMPUTATION_REQUESTED must remain exactly 1
        outbox_count = (await db.execute(
            sa.select(sa.func.count(OutboxMessage.id)).where(
                OutboxMessage.aggregate_id == lead_id,
                OutboxMessage.event_type == "MATCH_COMPUTATION_REQUESTED",
            )
        )).scalar()
        assert outbox_count == 1

    async def test_08_worker_retry_backoff_and_dead_letter_recovery(
        self,
        create_tables: None,
    ):
        """Criterion 11 (Part B): Worker error handling, exponential backoff retries, and dead-letter queue."""
        async with _make_session() as session:
            tenant = Tenant(
                id=uuid.uuid4(), name="Retry Agency", slug=f"retry-{uuid.uuid4().hex[:6]}",
                timezone="Asia/Kolkata", currency_code="INR", is_active=True, settings={},
            )
            session.add(tenant)
            await session.flush()

            msg = OutboxMessage(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                event_type="MATCH_COMPUTATION_REQUESTED",
                aggregate_type="LEAD",
                aggregate_id=str(uuid.uuid4()),
                payload={"lead_id": str(uuid.uuid4())},
                status="PENDING",
                retry_count=0,
                max_retries=3,
                scheduled_at=datetime.now(UTC),
                idempotency_key=f"retry-test-{uuid.uuid4()}",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add(msg)
            await session.commit()

        # Step 1: Simulate worker attempt 1 failure
        async with _make_session() as session:
            outbox_svc = OutboxService(session)
            claimed = await outbox_svc.claim_pending_messages(batch_size=10)
            assert len(claimed) == 1
            claimed_msg = claimed[0]

            claim_token = (claimed_msg.payload or {}).get("_claim_token")
            await outbox_svc.mark_failed(
                claimed_msg.id,
                error_message="Simulated temporary network failure",
                claimed_at=claimed_msg.updated_at,
                claim_token=claim_token,
            )
            await session.commit()

        # Verify retry state: retry_count = 1, status = PENDING, scheduled_at shifted to future
        async with _make_session() as session:
            m1 = await session.get(OutboxMessage, msg.id)
            assert m1.retry_count == 1
            assert m1.status == "PENDING"
            assert "Simulated temporary network failure" in m1.last_error
            assert m1.scheduled_at > datetime.now(UTC)

            # Fast-forward scheduled_at for retry simulation
            m1.scheduled_at = datetime.now(UTC)
            m1.retry_count = 3  # Hit max retries
            await session.commit()

        # Step 2: Simulate failure at max_retries -> DEAD_LETTER
        async with _make_session() as session:
            outbox_svc = OutboxService(session)
            claimed = await outbox_svc.claim_pending_messages(batch_size=10)
            assert len(claimed) == 1

            claimed_msg = claimed[0]
            claim_token = (claimed_msg.payload or {}).get("_claim_token")
            await outbox_svc.mark_failed(
                claimed_msg.id,
                error_message="Persistent fatal service exception",
                claimed_at=claimed_msg.updated_at,
                claim_token=claim_token,
            )
            await session.commit()

        # Verify transition to DEAD_LETTER
        async with _make_session() as session:
            m_dead = await session.get(OutboxMessage, msg.id)
            assert m_dead.status == "DEAD_LETTER"
            assert "Persistent fatal service exception" in m_dead.last_error

    async def test_09_multi_tenant_isolation_boundary_enforcement(
        self,
        client: AsyncClient,
        manager_token: str,
        agent_b_token: str,
        test_tenant: Tenant,
        test_tenant_b: Tenant,
    ):
        """Criterion 11 (Part C): Strict cross-tenant isolation on leads, matches, proposals, and stage changes."""
        # Create lead in Tenant A
        resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"full_name": "Tenant A Secret Client", "phone": "+919876543215", "source": "PORTAL"},
        )
        lead_id = resp.json()["id"]

        # Tenant B agent attempts access:
        # 1. GET /api/v1/leads/{id} -> 404
        t_b_get = await client.get(
            f"/api/v1/leads/{lead_id}",
            headers={"Authorization": f"Bearer {agent_b_token}"},
        )
        assert t_b_get.status_code == 404

        # 2. GET /api/v1/leads/{id}/matches -> 404
        t_b_matches = await client.get(
            f"/api/v1/leads/{lead_id}/matches",
            headers={"Authorization": f"Bearer {agent_b_token}"},
        )
        assert t_b_matches.status_code == 404

        # 3. POST /api/v1/leads/{id}/stage -> 404
        t_b_stage = await client.post(
            f"/api/v1/leads/{lead_id}/stage",
            headers={"Authorization": f"Bearer {agent_b_token}"},
            json={"target_stage": "QUALIFYING"},
        )
        assert t_b_stage.status_code == 404

        # 4. POST /api/v1/leads/{id}/proposals/generate -> 404
        t_b_gen = await client.post(
            f"/api/v1/leads/{lead_id}/proposals/generate",
            headers={"Authorization": f"Bearer {agent_b_token}"},
            json={"messages": ["Sneaky attempt"]},
        )
        assert t_b_gen.status_code == 404

    async def test_10_broker_handoff_path_and_in_app_notification(
        self,
        create_tables: None,
    ):
        """Criterion 12: Verify broker handoff outbox dispatch creates an in-app Notification."""
        async with _make_session() as session:
            tenant = Tenant(
                id=uuid.uuid4(), name="Handoff Agency", slug=f"handoff-{uuid.uuid4().hex[:6]}",
                timezone="Asia/Kolkata", currency_code="INR", is_active=True, settings={},
            )
            session.add(tenant)
            await session.flush()

            lead = Lead(
                id=uuid.uuid4(), tenant_id=tenant.id, full_name="Handoff Prospect",
                source="WHATSAPP", preferred_contact="WHATSAPP",
                budget_max=20_000_000, currency_code="INR",
                stage="QUALIFIED", lead_health="HOT", intent_score=90,
                automation_status="HUMAN_TAKEOVER", do_not_contact=False, is_deleted=False,
                created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
            )
            session.add(lead)
            await session.flush()

            handoff_msg = OutboxMessage(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                event_type="BROKER_HANDOFF_NOTIFICATION",
                aggregate_type="LEAD",
                aggregate_id=str(lead.id),
                payload={
                    "lead_id": str(lead.id),
                    "lead_name": "Handoff Prospect",
                    "score": 90,
                    "health": "HOT",
                    "summary": "High intent buyer looking for 3 BHK apartment in Whitefield",
                },
                status="PENDING",
                retry_count=0,
                max_retries=3,
                scheduled_at=datetime.now(UTC),
                idempotency_key=f"broker-notify-{lead.id}",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add(handoff_msg)
            await session.commit()

        # Worker processes BROKER_HANDOFF_NOTIFICATION
        async with _make_session() as session:
            msg = await session.get(OutboxMessage, handoff_msg.id)
            await _handle_message_event(msg, db=session)
            await session.commit()

        # Verify in-app Notification record in DB
        async with _make_session() as session:
            notif_stmt = sa.select(Notification).where(
                Notification.tenant_id == tenant.id,
                Notification.recipient_contact == "broker@havnrealty.com",
            )
            notif = (await session.execute(notif_stmt)).scalars().first()
            assert notif is not None
            assert notif.channel == "IN_APP"
            assert "HOT" in notif.title
            assert "High intent buyer" in notif.body
            assert notif.is_read is False
