"""
PostgreSQL integration tests for Phase 5: Automatic Property Matching Trigger via Outbox.
Runs against the dedicated test database (havn_test).

Verifies:
1. Qualification completion triggers matching via transactional outbox (MATCH_COMPUTATION_REQUESTED).
2. Outbox dispatch computes and persists eligible matches as LeadPropertyMatch.
3. No-match outcomes remain honest (no false positive matches created when inventory doesn't fit).
4. Retries are idempotent and safe (no duplicate LeadPropertyMatch records produced).
5. Only canonical inventory with status AVAILABLE is eligible (RESERVED, SOLD, OFF_MARKET excluded).
6. Strict multi-tenant isolation (Tenant A lead never matches Tenant B properties).
"""

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.dependencies import TenantContext, UserRole
from app.leads.models import Lead
from app.matching.models import LeadPropertyMatch
from app.matching.service import MatchingService
from app.messaging.models import OutboxMessage
from app.messaging.outbox_service import OutboxService
from app.properties.models import Property
from app.qualification.conversation_service import QualificationConversationService
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
class TestAutomaticPropertyMatchingWorkflow:
    """Integration verification of automatic property matching on qualification."""

    async def _create_tenant(self, session: AsyncSession, name: str) -> Tenant:
        tenant = Tenant(
            id=uuid.uuid4(),
            name=name,
            slug=f"match-tenant-{uuid.uuid4().hex[:8]}",
            timezone="Asia/Kolkata",
            currency_code="INR",
            is_active=True,
            settings={},
        )
        session.add(tenant)
        await session.flush()
        return tenant

    async def _create_user(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        role: str = "MANAGER",
    ) -> User:
        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            email=f"user-{uuid.uuid4().hex[:6]}@test.com",
            email_normalized=f"user-{uuid.uuid4().hex[:6]}@test.com",
            password_hash="fakehash",
            full_name="Test User",
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
        budget_min: int = 10_000_000,
        budget_max: int = 20_000_000,
        property_type: str = "APARTMENT",
        bhk: int = 3,
        location: str = "Whitefield",
        stage: str = "QUALIFYING",
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
            lead_health="HOT",
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
        external_id: str | None = None,
    ) -> Property:
        prop = Property(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            external_id=external_id or f"PROP-{uuid.uuid4().hex[:8].upper()}",
            title=title,
            property_type=property_type,
            bhk=bhk,
            price=price,
            currency_code="INR",
            location_text=location,
            area_sqft=1800,
            status=status,
            amenities=["Parking", "Clubhouse"],
            photos=[],
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            is_deleted=False,
        )
        session.add(prop)
        await session.flush()
        return prop

    async def test_01_proposal_approval_enqueues_and_computes_matches(self, create_tables):
        """Reviewing and accepting a proposal enqueues MATCH_COMPUTATION_REQUESTED which persists eligible matches."""
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Agency AutoMatch")
            lead = await self._create_lead(
                session, tenant.id, "Priya Sharma",
                budget_min=10_000_000, budget_max=16_000_000,
                property_type="APARTMENT", bhk=3, location="Whitefield",
                stage="QUALIFYING",
            )
            # Add matching property
            prop_match = await self._create_property(
                session, tenant.id, "Prestige Lakeside 3BHK",
                price=14_000_000, property_type="APARTMENT", bhk=3, location="Whitefield",
                status="AVAILABLE",
            )
            # Add non-matching property (different location/type)
            await self._create_property(
                session, tenant.id, "Luxury Villa Mussoorie",
                price=55_000_000, property_type="VILLA", bhk=5, location="Mussoorie Road",
                status="AVAILABLE",
            )
            # Add proposal
            proposal = LeadQualificationProposal(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                lead_id=lead.id,
                proposed_by="AI_AGENT",
                status="PENDING_REVIEW",
                budget_min=10_000_000,
                budget_max=16_000_000,
                property_type="APARTMENT",
                bhk=3,
                preferred_location="Whitefield",
                purpose="BUY",
                move_in_timeline="IMMEDIATE",
                confidence={"budget": 0.95, "location": 0.90},
                created_at=datetime.now(UTC),
            )
            session.add(proposal)
            await session.commit()

            admin_user = await self._create_user(session, tenant.id, role="MANAGER")
            await session.commit()

        # Review and accept proposal using QualificationService
        async with _make_session() as session:
            ctx = TenantContext(tenant_id=tenant.id, user_id=admin_user.id, role=UserRole.MANAGER)
            qual_svc = QualificationService(session, ctx)
            await qual_svc.review_proposal(lead.id, proposal.id, action="ACCEPT", review_notes="Approved by manager")
            await session.commit()

        # Verify that outbox contains MATCH_COMPUTATION_REQUESTED
        async with _make_session() as session:
            stmt = sa.select(OutboxMessage).where(
                OutboxMessage.tenant_id == tenant.id,
                OutboxMessage.event_type == "MATCH_COMPUTATION_REQUESTED",
                OutboxMessage.aggregate_id == str(lead.id),
            )
            outbox_msg = (await session.execute(stmt)).scalar_one_or_none()
            assert outbox_msg is not None
            assert outbox_msg.status == "PENDING"
            assert outbox_msg.idempotency_key == f"match-compute-{lead.id}"

            # Process the outbox event
            await _handle_message_event(outbox_msg, db=session)
            await session.commit()

        # Verify LeadPropertyMatch persistence
        async with _make_session() as session:
            stmt = sa.select(LeadPropertyMatch).where(
                LeadPropertyMatch.tenant_id == tenant.id,
                LeadPropertyMatch.lead_id == lead.id,
            )
            matches = (await session.execute(stmt)).scalars().all()

            # Exactly the eligible property matched
            assert len(matches) == 1
            match = matches[0]
            assert match.property_id == prop_match.id
            assert match.match_score >= 30.0
            assert match.status == "SUGGESTED"
            assert isinstance(match.match_reasons, dict)

    async def test_02_no_match_outcome_remains_honest(self, create_tables):
        """When no inventory meets criteria, no false matches are created."""
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Agency Honest NoMatch")
            lead = await self._create_lead(
                session, tenant.id, "Vikram Sen",
                budget_min=2_000_000, budget_max=3_000_000,
                property_type="PLOT", bhk=None, location="Rishikesh",
                stage="QUALIFIED",
            )
            # All inventory is expensive apartments in Whitefield
            await self._create_property(
                session, tenant.id, "Whitefield Luxury Flat",
                price=18_000_000, property_type="APARTMENT", bhk=3, location="Whitefield",
                status="AVAILABLE",
            )
            await session.commit()

        # Enqueue and dispatch match computation
        async with _make_session() as session:
            outbox_svc = OutboxService(session)
            msg = await outbox_svc.enqueue_message(
                tenant_id=tenant.id,
                event_type="MATCH_COMPUTATION_REQUESTED",
                aggregate_type="LEAD",
                aggregate_id=lead.id,
                payload={"lead_id": str(lead.id)},
                idempotency_key=f"match-honest-{lead.id}",
            )
            await session.commit()

            await _handle_message_event(msg, db=session)
            await session.commit()

        # Verify no matches were created
        async with _make_session() as session:
            matches = (await session.execute(
                sa.select(LeadPropertyMatch).where(LeadPropertyMatch.lead_id == lead.id)
            )).scalars().all()
            assert len(matches) == 0

    async def test_03_retry_safety_and_idempotency(self, create_tables):
        """Retrying match computation updates existing match in place without duplicate records."""
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Agency Retry Safety")
            lead = await self._create_lead(
                session, tenant.id, "Deepak Joshi",
                budget_min=10_000_000, budget_max=15_000_000,
                property_type="APARTMENT", bhk=3, location="Whitefield",
                stage="QUALIFIED",
            )
            prop = await self._create_property(
                session, tenant.id, "Sobha Dream 3BHK",
                price=12_000_000, property_type="APARTMENT", bhk=3, location="Whitefield",
                status="AVAILABLE",
            )
            await session.commit()

        # Run match computation 3 times in succession
        async with _make_session() as session:
            admin_user = await self._create_user(session, tenant.id, role="AGENCY_ADMIN")
            ctx = TenantContext(tenant_id=tenant.id, user_id=admin_user.id, role=UserRole.AGENCY_ADMIN)
            svc = MatchingService(session, ctx)
            for _ in range(3):
                await svc.compute_matches_for_lead(lead.id)
            await session.commit()

        # Verify only 1 match record exists in database
        async with _make_session() as session:
            matches = (await session.execute(
                sa.select(LeadPropertyMatch).where(
                    LeadPropertyMatch.tenant_id == tenant.id,
                    LeadPropertyMatch.lead_id == lead.id,
                )
            )).scalars().all()
            assert len(matches) == 1
            assert matches[0].property_id == prop.id

    async def test_04_status_gating_excludes_unavailable_properties(self, create_tables):
        """Properties with status RESERVED, SOLD, or OFF_MARKET are never matched."""
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Agency Status Gating")
            lead = await self._create_lead(
                session, tenant.id, "Aman Gupta",
                budget_min=10_000_000, budget_max=20_000_000,
                property_type="VILLA", bhk=4, location="Dehradun",
                stage="QUALIFIED",
            )
            # Create properties with identical specs but unavailable statuses
            await self._create_property(
                session, tenant.id, "Reserved Villa",
                price=15_000_000, property_type="VILLA", bhk=4, location="Dehradun",
                status="RESERVED",
            )
            await self._create_property(
                session, tenant.id, "Sold Villa",
                price=15_000_000, property_type="VILLA", bhk=4, location="Dehradun",
                status="SOLD",
            )
            await self._create_property(
                session, tenant.id, "Off-Market Villa",
                price=15_000_000, property_type="VILLA", bhk=4, location="Dehradun",
                status="OFF_MARKET",
            )
            # And one available villa
            available_villa = await self._create_property(
                session, tenant.id, "Available Villa",
                price=15_000_000, property_type="VILLA", bhk=4, location="Dehradun",
                status="AVAILABLE",
            )
            await session.commit()

        async with _make_session() as session:
            admin_user = await self._create_user(session, tenant.id, role="AGENCY_ADMIN")
            ctx = TenantContext(tenant_id=tenant.id, user_id=admin_user.id, role=UserRole.AGENCY_ADMIN)
            svc = MatchingService(session, ctx)
            matches = await svc.compute_matches_for_lead(lead.id)
            await session.commit()

            assert len(matches) == 1
            assert matches[0].property_id == available_villa.id

    async def test_05_tenant_isolation_strictly_enforced(self, create_tables):
        """Tenant A lead never matches Tenant B properties even if criteria are an exact match."""
        async with _make_session() as session:
            tenant_a = await self._create_tenant(session, "Tenant A")
            tenant_b = await self._create_tenant(session, "Tenant B")

            lead_a = await self._create_lead(
                session, tenant_a.id, "Tenant A Lead",
                budget_min=10_000_000, budget_max=15_000_000,
                property_type="APARTMENT", bhk=3, location="Indiranagar",
                stage="QUALIFIED",
            )

            # Property created in Tenant B with exact matching criteria
            prop_b = await self._create_property(
                session, tenant_b.id, "Tenant B Perfect Match",
                price=12_000_000, property_type="APARTMENT", bhk=3, location="Indiranagar",
                status="AVAILABLE",
            )
            await session.commit()

        # Compute matches for Tenant A lead
        async with _make_session() as session:
            admin_user_a = await self._create_user(session, tenant_a.id, role="AGENCY_ADMIN")
            ctx_a = TenantContext(tenant_id=tenant_a.id, user_id=admin_user_a.id, role=UserRole.AGENCY_ADMIN)
            svc_a = MatchingService(session, ctx_a)
            matches_a = await svc_a.compute_matches_for_lead(lead_a.id)
            await session.commit()

            # Cross-tenant match must NOT exist
            assert len(matches_a) == 0

        # Cross-tenant direct query check
        async with _make_session() as session:
            matches_in_db = (await session.execute(
                sa.select(LeadPropertyMatch).where(
                    LeadPropertyMatch.lead_id == lead_a.id,
                    LeadPropertyMatch.property_id == prop_b.id,
                )
            )).scalars().all()
            assert len(matches_in_db) == 0

    async def test_06_conversation_completion_enqueues_match_and_broker_handoff(self, create_tables):
        """AI conversation completion enqueues MATCH_COMPUTATION_REQUESTED alongside broker handoff."""
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Agency Conv Complete")
            lead = await self._create_lead(
                session, tenant.id, "Kavita Rao",
                budget_min=12_000_000, budget_max=18_000_000,
                property_type="APARTMENT", bhk=3, location="Whitefield",
                stage="QUALIFYING",
            )
            prop = await self._create_property(
                session, tenant.id, "Whitefield Residency 3BHK",
                price=15_000_000, property_type="APARTMENT", bhk=3, location="Whitefield",
                status="AVAILABLE",
            )
            await session.commit()

        async with _make_session() as session:
            conv_svc = QualificationConversationService(session)
            lead_obj = (await session.execute(sa.select(Lead).where(Lead.id == lead.id))).scalar_one()
            answers = {
                "intent": "BUY",
                "property_type": "APARTMENT",
                "bhk": 3,
                "budget_min": 12_000_000,
                "budget_max": 18_000_000,
                "location": "Whitefield",
                "timeline": "IMMEDIATE",
            }
            res = await conv_svc._handle_qualification_completion(
                lead=lead_obj,
                answers=answers,
                lang="en",
                now=datetime.now(UTC),
                sender_phone="+919876543210",
            )
            await session.commit()
            assert res.stage == "QUALIFYING"

        # 2. Broker reviews and accepts the qualification proposal (satisfying the broker review gate)
        async with _make_session() as session:
            admin_user = await self._create_user(session, tenant.id, role="AGENCY_ADMIN")
            ctx = TenantContext(tenant_id=tenant.id, user_id=admin_user.id, role=UserRole.AGENCY_ADMIN)
            from app.qualification.service import QualificationService

            qual_svc = QualificationService(session, ctx)
            proposal = await qual_svc.generate_proposal(
                lead.id,
                messages=[f"Looking for {answers['bhk']} BHK {answers['property_type']} in {answers['location']} for {answers['budget_max']}"],
            )
            accepted_prop = await qual_svc.review_proposal(lead.id, proposal.id, action="ACCEPT")
            assert accepted_prop.status == "ACCEPTED"
            await session.commit()

        # Check Outbox has both broker notification and match computation requested
        async with _make_session() as session:
            lead_check = (await session.execute(sa.select(Lead).where(Lead.id == lead.id))).scalar_one()
            assert lead_check.stage == "QUALIFIED"

            outbox_msgs = (await session.execute(
                sa.select(OutboxMessage).where(OutboxMessage.aggregate_id == str(lead.id))
            )).scalars().all()
            event_types = [m.event_type for m in outbox_msgs]
            assert "BROKER_HANDOFF_NOTIFICATION" in event_types
            assert "MATCH_COMPUTATION_REQUESTED" in event_types

            # Process the match computation event
            match_msg = next(m for m in outbox_msgs if m.event_type == "MATCH_COMPUTATION_REQUESTED")
            await _handle_message_event(match_msg, db=session)
            await session.commit()

        # Verify LeadPropertyMatch created
        async with _make_session() as session:
            matches = (await session.execute(
                sa.select(LeadPropertyMatch).where(LeadPropertyMatch.lead_id == lead.id)
            )).scalars().all()
            assert len(matches) == 1
            assert matches[0].property_id == prop.id
            assert matches[0].status == "SUGGESTED"
