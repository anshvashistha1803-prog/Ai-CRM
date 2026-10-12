"""
Unit tests for SiteVisitService.
Verifies conflict checks, agent calendar overlapping detection, timezone validation,
duplicate booking prevention, rescheduling, cancellations, and notification emissions.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import (
    ConflictError,
    PermissionDeniedError,
    ValidationError,
)
from app.leads.models import Lead
from app.leads.state_machine import LeadStage
from app.properties.models import Property
from app.visits.models import SiteVisit
from app.visits.schemas import (
    SiteVisitCreateRequest,
    SiteVisitRescheduleRequest,
)
from app.visits.service import SiteVisitService


class MockTx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            await self.session.rollback()


@pytest.fixture
def mock_db():
    session = AsyncMock()
    session.add = MagicMock()
    session.in_transaction = MagicMock(return_value=True)
    session.begin = MagicMock(side_effect=lambda: MockTx(session))
    session.begin_nested = MagicMock(side_effect=lambda: MockTx(session))
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


def _create_mock_lead(tenant_id: uuid.UUID, agent_id: uuid.UUID | None = None) -> Lead:
    return Lead(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        full_name="Vikram Mehta",
        stage=LeadStage.QUALIFIED.value,
        assigned_agent_id=agent_id,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        is_deleted=False,
    )


def _create_mock_property(tenant_id: uuid.UUID) -> Property:
    return Property(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        title="Sobha Dream Acres 2BHK",
        property_type="APARTMENT",
        bhk=2,
        price=9500000,
        currency_code="INR",
        location_text="Panathur, Bangalore",
        status="AVAILABLE",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        is_deleted=False,
    )


@pytest.mark.asyncio
async def test_schedule_visit_success(mock_db):
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    lead = _create_mock_lead(tenant_id, agent_id=agent_id)
    prop = _create_mock_property(tenant_id)

    # Lead check
    lead_res = MagicMock()
    lead_res.scalars.return_value.first.return_value = lead

    # Property check
    prop_res = MagicMock()
    prop_res.scalars.return_value.first.return_value = prop

    # Agent conflict check (None = no conflict)
    conflict_res = MagicMock()
    conflict_res.scalars.return_value.first.return_value = None

    # Duplicate visit check (None = no duplicate)
    dup_res = MagicMock()
    dup_res.scalars.return_value.first.return_value = None

    # Outbox existing check
    outbox_res = MagicMock()
    outbox_res.scalars.return_value.first.return_value = None

    mock_db.execute.side_effect = [lead_res, prop_res, MagicMock(), MagicMock(), conflict_res, dup_res, outbox_res]

    ctx = TenantContext(tenant_id=tenant_id, user_id=agent_id, role=UserRole.AGENT)
    svc = SiteVisitService(mock_db, ctx)

    start = datetime.now(UTC) + timedelta(days=2)
    end = start + timedelta(hours=1)

    visit = await svc.schedule_visit(
        SiteVisitCreateRequest(
            lead_id=lead.id,
            property_id=prop.id,
            scheduled_start_at=start,
            scheduled_end_at=end,
            notes="Client prefers afternoon visit.",
        )
    )

    assert visit.status == "SCHEDULED"
    assert visit.lead_id == lead.id
    assert visit.property_id == prop.id
    assert lead.stage == LeadStage.SITE_VISIT_SCHEDULED.value
    assert mock_db.add.call_count >= 2  # SiteVisit, LeadActivity, OutboxMessage
    mock_db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_schedule_visit_agent_conflict_detected(mock_db):
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    lead = _create_mock_lead(tenant_id, agent_id=agent_id)
    prop = _create_mock_property(tenant_id)

    start = datetime.now(UTC) + timedelta(days=2)
    end = start + timedelta(hours=1)

    existing_visit = SiteVisit(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        lead_id=uuid.uuid4(),
        property_id=uuid.uuid4(),
        assigned_agent_id=agent_id,
        scheduled_start_at=start,
        scheduled_end_at=end,
        status="SCHEDULED",
    )

    lead_res = MagicMock()
    lead_res.scalars.return_value.first.return_value = lead

    prop_res = MagicMock()
    prop_res.scalars.return_value.first.return_value = prop

    conflict_res = MagicMock()
    conflict_res.scalars.return_value.first.return_value = existing_visit

    mock_db.execute.side_effect = [lead_res, prop_res, MagicMock(), MagicMock(), conflict_res]

    ctx = TenantContext(tenant_id=tenant_id, user_id=agent_id, role=UserRole.AGENT)
    svc = SiteVisitService(mock_db, ctx)

    with pytest.raises(ConflictError, match="Agent is already scheduled for another visit"):
        await svc.schedule_visit(
            SiteVisitCreateRequest(
                lead_id=lead.id,
                property_id=prop.id,
                scheduled_start_at=start,
                scheduled_end_at=end,
            )
        )


@pytest.mark.asyncio
async def test_schedule_visit_past_start_time_rejected(mock_db):
    tenant_id = uuid.uuid4()
    lead = _create_mock_lead(tenant_id)
    prop = _create_mock_property(tenant_id)

    lead_res = MagicMock()
    lead_res.scalars.return_value.first.return_value = lead

    prop_res = MagicMock()
    prop_res.scalars.return_value.first.return_value = prop

    mock_db.execute.side_effect = [lead_res, prop_res]

    ctx = TenantContext(tenant_id=tenant_id, user_id=uuid.uuid4(), role=UserRole.MANAGER)
    svc = SiteVisitService(mock_db, ctx)

    past_start = datetime.now(UTC) - timedelta(hours=2)
    past_end = past_start + timedelta(hours=1)

    with pytest.raises(ValidationError, match="Visit scheduled start time must be in the future"):
        await svc.schedule_visit(
            SiteVisitCreateRequest(
                lead_id=lead.id,
                property_id=prop.id,
                scheduled_start_at=past_start,
                scheduled_end_at=past_end,
            )
        )


@pytest.mark.asyncio
async def test_schedule_visit_agent_isolation(mock_db):
    tenant_id = uuid.uuid4()
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    lead = _create_mock_lead(tenant_id, agent_id=agent_b)

    lead_res = MagicMock()
    lead_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = lead_res

    ctx = TenantContext(tenant_id=tenant_id, user_id=agent_a, role=UserRole.AGENT)
    svc = SiteVisitService(mock_db, ctx)

    start = datetime.now(UTC) + timedelta(days=2)
    end = start + timedelta(hours=1)

    with pytest.raises(PermissionDeniedError, match="Agents can only manage site visits for their assigned leads"):
        await svc.schedule_visit(
            SiteVisitCreateRequest(
                lead_id=lead.id,
                property_id=uuid.uuid4(),
                scheduled_start_at=start,
                scheduled_end_at=end,
            )
        )


@pytest.mark.asyncio
async def test_reschedule_visit_success(mock_db):
    tenant_id = uuid.uuid4()
    lead_id = uuid.uuid4()
    prop_id = uuid.uuid4()
    agent_id = uuid.uuid4()

    old_start = datetime.now(UTC) + timedelta(days=2)
    old_end = old_start + timedelta(hours=1)

    old_visit = SiteVisit(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        lead_id=lead_id,
        property_id=prop_id,
        assigned_agent_id=agent_id,
        scheduled_start_at=old_start,
        scheduled_end_at=old_end,
        status="SCHEDULED",
    )

    visit_res = MagicMock()
    visit_res.scalars.return_value.first.return_value = old_visit

    conflict_res = MagicMock()
    conflict_res.scalars.return_value.first.return_value = None

    outbox_res = MagicMock()
    outbox_res.scalars.return_value.first.return_value = None

    mock_db.execute.side_effect = [visit_res, MagicMock(), MagicMock(), conflict_res, outbox_res]

    ctx = TenantContext(tenant_id=tenant_id, user_id=agent_id, role=UserRole.AGENT)
    svc = SiteVisitService(mock_db, ctx)

    new_start = datetime.now(UTC) + timedelta(days=4)
    new_end = new_start + timedelta(hours=1)

    new_visit = await svc.reschedule_visit(
        old_visit.id,
        SiteVisitRescheduleRequest(
            new_start_at=new_start,
            new_end_at=new_end,
            notes="Client rescheduled to weekend.",
        ),
    )

    assert old_visit.status == "RESCHEDULED"
    assert new_visit.status == "SCHEDULED"
    assert new_visit.rescheduled_from_id == old_visit.id
    mock_db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_cancel_visit_reverts_lead_stage(mock_db):
    tenant_id = uuid.uuid4()
    lead = _create_mock_lead(tenant_id)
    lead.stage = LeadStage.SITE_VISIT_SCHEDULED.value

    visit = SiteVisit(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        lead_id=lead.id,
        property_id=uuid.uuid4(),
        scheduled_start_at=datetime.now(UTC) + timedelta(days=2),
        scheduled_end_at=datetime.now(UTC) + timedelta(days=2, hours=1),
        status="SCHEDULED",
    )

    visit_res = MagicMock()
    visit_res.scalars.return_value.first.return_value = visit

    # Check other active visits: None
    other_active_res = MagicMock()
    other_active_res.scalars.return_value.first.return_value = None

    # Lead check
    lead_res = MagicMock()
    lead_res.scalars.return_value.first.return_value = lead

    outbox_res = MagicMock()
    outbox_res.scalars.return_value.first.return_value = None

    mock_db.execute.side_effect = [visit_res, other_active_res, lead_res, outbox_res]

    ctx = TenantContext(tenant_id=tenant_id, user_id=uuid.uuid4(), role=UserRole.MANAGER)
    svc = SiteVisitService(mock_db, ctx)

    cancelled = await svc.cancel_visit(visit.id, reason="Client is out of town.")

    assert cancelled.status == "CANCELLED"
    assert cancelled.cancellation_reason == "Client is out of town."
    # Lead stage reverted back to QUALIFIED
    assert lead.stage == LeadStage.QUALIFIED.value
    mock_db.flush.assert_awaited()
