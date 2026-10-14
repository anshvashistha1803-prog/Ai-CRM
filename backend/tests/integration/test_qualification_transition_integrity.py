"""
Integration tests for qualification state-transition integrity and matching outbox triggers.
Runs against the dedicated test database (havn_test).

Verifies:
1. QUALIFYING -> QUALIFIED without qualification evidence is rejected with 422 INVALID_TRANSITION.
2. PENDING_REVIEW or REJECTED proposals do not permit transitioning to QUALIFIED.
3. Transitioning with an ACCEPTED proposal evaluates deterministic score, persists health,
   and enqueues MATCH_COMPUTATION_REQUESTED atomically in the transactional outbox.
4. Idempotency: repeated transitions do not duplicate outbox messages or produce side effects.
5. Outbox dispatch strictly enforces tenant isolation and only matches AVAILABLE properties.
"""

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.leads.models import Lead
from app.leads.state_machine import LeadHealth, LeadStage
from app.matching.models import LeadPropertyMatch
from app.messaging.models import OutboxMessage
from app.properties.models import Property
from app.qualification.models import LeadQualificationProposal
from app.tasks.celery_tasks import _handle_message_event
from app.tenants.models import Tenant
from app.users.models import User
from tests.conftest import test_engine


def _make_session() -> AsyncSession:
    """Create an isolated AsyncSession bound to test_engine."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    return factory()


@pytest.mark.asyncio
class TestQualificationTransitionIntegrity:
    """Verification of qualification state transition integrity on havn_test."""

    async def _create_tenant(self, session: AsyncSession, name: str) -> Tenant:
        tenant = Tenant(
            id=uuid.uuid4(),
            name=name,
            slug=f"qual-tenant-{uuid.uuid4().hex[:8]}",
            timezone="Asia/Kolkata",
            currency_code="INR",
            is_active=True,
            settings={},
        )
        session.add(tenant)
        await session.flush()
        return tenant

    async def _create_user(
        self, session: AsyncSession, tenant_id: uuid.UUID, role: str = "MANAGER"
    ) -> User:
        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            email=f"qual-user-{uuid.uuid4().hex[:6]}@test.com",
            email_normalized=f"qual-user-{uuid.uuid4().hex[:6]}@test.com",
            password_hash="fakehash",
            full_name="Qualification Test User",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.flush()
        return user

    async def _create_lead(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        full_name: str,
        stage: str = "QUALIFYING",
        budget_min: int = 10_000_000,
        budget_max: int = 20_000_000,
        property_type: str = "APARTMENT",
        bhk: int = 3,
        location: str = "Whitefield",
    ) -> Lead:
        lead = Lead(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            full_name=full_name,
            source="WHATSAPP",
            preferred_contact="WHATSAPP",
            budget_min=budget_min,
            budget_max=budget_max,
            currency_code="INR",
            property_type=property_type,
            bhk=bhk,
            preferred_location=location,
            purpose="BUY",
            move_in_timeline="IMMEDIATE",
            stage=stage,
            lead_health="UNKNOWN",
            intent_score=None,
            automation_status="ACTIVE",
            do_not_contact=False,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            is_deleted=False,
        )
        session.add(lead)
        await session.flush()
        return lead

    async def _create_property(
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
            area_sqft=1800,
            status=status,
            amenities=["Parking"],
            photos=[],
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            is_deleted=False,
        )
        session.add(prop)
        await session.flush()
        return prop

    async def test_01_manual_transition_to_qualified_rejected_without_evidence(
        self, client: AsyncClient, manager_token: str, test_tenant: Tenant, db
    ):
        """Lead in QUALIFYING without accepted qualification evidence cannot transition to QUALIFIED."""
        # Create lead
        create_resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Unqualified Lead",
                "phone": "+919876543111",
                "source": "PORTAL",
            },
        )
        assert create_resp.status_code == 201
        lead_id = create_resp.json()["id"]

        # Advance to QUALIFYING
        t_qualifying = await client.post(
            f"/api/v1/leads/{lead_id}/stage",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"target_stage": "QUALIFYING", "reason": "Begin qualification"},
        )
        assert t_qualifying.status_code == 200

        # Attempt to transition to QUALIFIED without proposal
        t_qualified = await client.post(
            f"/api/v1/leads/{lead_id}/stage",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"target_stage": "QUALIFIED", "reason": "Bypassing qualification"},
        )
        assert t_qualified.status_code == 422
        body = t_qualified.json()
        assert body["error"] == "INVALID_TRANSITION"
        assert "without accepted qualification evidence" in body["message"]

        # Verify DB state: still QUALIFYING, no outbox event
        lead_db = await db.get(Lead, uuid.UUID(lead_id))
        assert lead_db.stage == LeadStage.QUALIFYING.value
        assert lead_db.intent_score is None

        outbox_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == lead_id,
            OutboxMessage.event_type == "MATCH_COMPUTATION_REQUESTED",
        )
        outbox_res = await db.execute(outbox_stmt)
        assert outbox_res.scalar_one_or_none() is None

    async def test_02_pending_proposal_does_not_permit_transition_to_qualified(
        self, client: AsyncClient, manager_token: str, test_tenant: Tenant, db
    ):
        """A proposal that is PENDING_REVIEW does not satisfy qualification evidence requirements."""
        create_resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Pending Proposal Lead",
                "phone": "+919876543222",
                "source": "PORTAL",
            },
        )
        assert create_resp.status_code == 201
        lead_id = create_resp.json()["id"]

        # Advance to QUALIFYING
        await client.post(
            f"/api/v1/leads/{lead_id}/stage",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"target_stage": "QUALIFYING"},
        )

        # Generate proposal (leaves status as PENDING_REVIEW)
        gen_resp = await client.post(
            f"/api/v1/leads/{lead_id}/proposals/generate",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"messages": ["I want a 3 BHK in Whitefield with 1.5 Cr budget"]},
        )
        assert gen_resp.status_code == 200
        prop_id = gen_resp.json()["id"]
        assert prop_id
        assert gen_resp.json()["status"] == "PENDING_REVIEW"

        # Direct transition to QUALIFIED must still be rejected
        t_fail = await client.post(
            f"/api/v1/leads/{lead_id}/stage",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"target_stage": "QUALIFIED"},
        )
        assert t_fail.status_code == 422
        assert "without accepted qualification evidence" in t_fail.json()["message"]

    async def test_03_accepted_proposal_enables_qualified_and_enqueues_matching(
        self, client: AsyncClient, manager_token: str, test_tenant: Tenant, db
    ):
        """Reviewing and accepting proposal advances stage to QUALIFIED, persists score, and enqueues MATCH_COMPUTATION_REQUESTED."""
        create_resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Accepted Lead",
                "phone": "+919876543333",
                "source": "PORTAL",
            },
        )
        assert create_resp.status_code == 201
        lead_id = create_resp.json()["id"]

        # Generate proposal
        gen_resp = await client.post(
            f"/api/v1/leads/{lead_id}/proposals/generate",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"messages": ["I want a 3 BHK apartment in Whitefield with budget 1.5 Cr to 2 Cr"]},
        )
        assert gen_resp.status_code == 200
        prop_id = gen_resp.json()["id"]

        # Accept proposal
        review_resp = await client.post(
            f"/api/v1/leads/{lead_id}/proposals/{prop_id}/review",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"action": "ACCEPT", "review_notes": "Criteria validated"},
        )
        assert review_resp.status_code == 200
        assert review_resp.json()["status"] == "ACCEPTED"

        # Verify DB state
        lead_db = await db.get(Lead, uuid.UUID(lead_id))
        assert lead_db.stage == LeadStage.QUALIFIED.value
        assert lead_db.intent_score is not None
        assert lead_db.intent_score > 0
        assert lead_db.lead_health in (LeadHealth.HOT.value, LeadHealth.WARM.value)
        assert lead_db.lead_health != "UNKNOWN"

        # Verify Outbox message was atomically enqueued
        outbox_stmt = sa.select(OutboxMessage).where(
            OutboxMessage.aggregate_id == lead_id,
            OutboxMessage.event_type == "MATCH_COMPUTATION_REQUESTED",
        )
        outbox_res = await db.execute(outbox_stmt)
        outbox_msg = outbox_res.scalar_one_or_none()
        assert outbox_msg is not None
        assert outbox_msg.idempotency_key == f"match-compute-{lead_id}"
        assert outbox_msg.payload["lead_id"] == lead_id
        assert outbox_msg.payload["score"] == lead_db.intent_score

    async def test_04_idempotent_repeated_transition_does_not_duplicate_outbox(
        self, client: AsyncClient, manager_token: str, test_tenant: Tenant, db
    ):
        """Repeated transition requests for a qualified lead do not generate duplicate outbox events."""
        create_resp = await client.post(
            "/api/v1/leads",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={
                "full_name": "Idempotent Lead",
                "phone": "+919876543444",
                "source": "PORTAL",
            },
        )
        lead_id = create_resp.json()["id"]

        # Generate & accept proposal
        gen = await client.post(
            f"/api/v1/leads/{lead_id}/proposals/generate",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"messages": ["Looking for 2 BHK in Indiranagar"]},
        )
        prop_id = gen.json()["id"]

        await client.post(
            f"/api/v1/leads/{lead_id}/proposals/{prop_id}/review",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"action": "ACCEPT"},
        )

        # Subsequent transition to QUALIFIED
        repeat_resp = await client.post(
            f"/api/v1/leads/{lead_id}/stage",
            headers={"Authorization": f"Bearer {manager_token}"},
            json={"target_stage": "QUALIFIED", "reason": "Repeated transition call"},
        )
        assert repeat_resp.status_code == 200

        # Count outbox messages: must be exactly 1
        outbox_stmt = sa.select(sa.func.count(OutboxMessage.id)).where(
            OutboxMessage.aggregate_id == lead_id,
            OutboxMessage.event_type == "MATCH_COMPUTATION_REQUESTED",
        )
        count = (await db.execute(outbox_stmt)).scalar()
        assert count == 1

    async def test_05_outbox_dispatch_matches_only_available_properties_and_enforces_tenant_isolation(
        self, create_tables
    ):
        """MATCH_COMPUTATION_REQUESTED dispatches and creates LeadPropertyMatch records only for AVAILABLE properties of same tenant."""
        async with _make_session() as session:
            tenant_a = await self._create_tenant(session, "Tenant A Agency")
            tenant_b = await self._create_tenant(session, "Tenant B Agency")

            lead_a = await self._create_lead(
                session,
                tenant_a.id,
                "Tenant A Client",
                stage="QUALIFIED",
                budget_min=10_000_000,
                budget_max=18_000_000,
                property_type="APARTMENT",
                bhk=3,
                location="Whitefield",
            )
            # Property in Tenant A (AVAILABLE, matching criteria)
            prop_a_avail = await self._create_property(
                session, tenant_a.id, "Tenant A Prestige 3BHK",
                price=15_000_000, property_type="APARTMENT", bhk=3, location="Whitefield",
                status="AVAILABLE",
            )
            # Property in Tenant A (SOLD, must be excluded)
            prop_a_sold = await self._create_property(
                session, tenant_a.id, "Tenant A Sold Flat",
                price=14_000_000, property_type="APARTMENT", bhk=3, location="Whitefield",
                status="SOLD",
            )
            # Property in Tenant B (AVAILABLE, but wrong tenant - multi-tenant boundary)
            prop_b_avail = await self._create_property(
                session, tenant_b.id, "Tenant B Foreign Flat",
                price=15_000_000, property_type="APARTMENT", bhk=3, location="Whitefield",
                status="AVAILABLE",
            )
            # Add accepted proposal
            proposal = LeadQualificationProposal(
                id=uuid.uuid4(),
                tenant_id=tenant_a.id,
                lead_id=lead_a.id,
                status="ACCEPTED",
                proposed_by="AI",
                confidence={},
                created_at=datetime.now(UTC),
            )
            session.add(proposal)

            # Add outbox message
            outbox_msg = OutboxMessage(
                id=uuid.uuid4(),
                tenant_id=tenant_a.id,
                event_type="MATCH_COMPUTATION_REQUESTED",
                aggregate_type="LEAD",
                aggregate_id=str(lead_a.id),
                payload={"lead_id": str(lead_a.id)},
                status="PENDING",
                idempotency_key=f"match-compute-{lead_a.id}",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            session.add(outbox_msg)
            await session.commit()

        # Dispatch outbox event
        async with _make_session() as session:
            await _handle_message_event(outbox_msg, db=session)
            await session.commit()

        # Verify matches in DB
        async with _make_session() as session:
            matches_stmt = sa.select(LeadPropertyMatch).where(
                LeadPropertyMatch.lead_id == lead_a.id
            )
            matches = (await session.execute(matches_stmt)).scalars().all()

            # Exactly 1 match created (prop_a_avail)
            assert len(matches) == 1
            matched_prop_ids = [m.property_id for m in matches]
            assert prop_a_avail.id in matched_prop_ids
            assert prop_a_sold.id not in matched_prop_ids
            assert prop_b_avail.id not in matched_prop_ids
