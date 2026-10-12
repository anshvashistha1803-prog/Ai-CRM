"""
Unit tests for MatchingService.
Verifies property-lead compatibility matching, score calculation, reason explanations,
agent lead isolation, and match status updates (shortlisting, rejecting).
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.leads.models import Lead
from app.matching.models import LeadPropertyMatch
from app.matching.service import MatchingService
from app.properties.models import Property


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
        full_name="Pooja Sharma",
        stage="QUALIFIED",
        assigned_agent_id=agent_id,
        budget_min=10000000,
        budget_max=16000000,
        property_type="APARTMENT",
        bhk=3,
        preferred_location="Whitefield",
        purpose="BUY",
        move_in_timeline="IMMEDIATE",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        is_deleted=False,
    )


def _create_mock_property(
    tenant_id: uuid.UUID,
    title: str = "Prestige Ozone 3BHK",
    price: int = 14500000,
    bhk: int = 3,
    status: str = "AVAILABLE",
) -> Property:
    return Property(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        title=title,
        property_type="APARTMENT",
        bhk=bhk,
        price=price,
        currency_code="INR",
        location_text="Whitefield, Bangalore",
        status=status,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        is_deleted=False,
    )


@pytest.mark.asyncio
async def test_compute_matches_for_lead_success(mock_db):
    tenant_id = uuid.uuid4()
    lead = _create_mock_lead(tenant_id)
    prop1 = _create_mock_property(tenant_id, "Matching Flat", price=14000000, bhk=3, status="AVAILABLE")
    prop2 = _create_mock_property(tenant_id, "Out of Budget Villa", price=50000000, bhk=5, status="AVAILABLE")

    # Lead check
    lead_res = MagicMock()
    lead_res.scalars.return_value.first.return_value = lead

    # Properties list
    props_res = MagicMock()
    props_res.scalars.return_value.all.return_value = [prop1, prop2]

    # Existing match check for prop1
    existing_res1 = MagicMock()
    existing_res1.scalars.return_value.first.return_value = None

    # Existing match check for prop2 (if evaluated)
    existing_res2 = MagicMock()
    existing_res2.scalars.return_value.first.return_value = None

    mock_db.execute.side_effect = [lead_res, props_res, existing_res1, existing_res2]

    ctx = TenantContext(tenant_id=tenant_id, user_id=uuid.uuid4(), role=UserRole.MANAGER)
    svc = MatchingService(mock_db, ctx)

    matches = await svc.compute_matches_for_lead(lead.id, min_score=40.0)

    # Prop 1 matches budget + bhk + prop_type -> score should be high
    assert len(matches) >= 1
    top_match = matches[0]
    assert top_match.property_id == prop1.id
    assert float(top_match.match_score) >= 70.0
    assert "budget" in top_match.match_reasons
    mock_db.add.assert_called()
    mock_db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_compute_matches_agent_isolation(mock_db):
    tenant_id = uuid.uuid4()
    agent_a = uuid.uuid4()
    agent_b = uuid.uuid4()
    lead = _create_mock_lead(tenant_id, agent_id=agent_b)

    lead_res = MagicMock()
    lead_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = lead_res

    # Agent A tries to compute matches for Agent B's lead
    ctx = TenantContext(tenant_id=tenant_id, user_id=agent_a, role=UserRole.AGENT)
    svc = MatchingService(mock_db, ctx)

    with pytest.raises(PermissionDeniedError, match="Agents can only access matches"):
        await svc.compute_matches_for_lead(lead.id)


@pytest.mark.asyncio
async def test_update_match_status_success(mock_db):
    tenant_id = uuid.uuid4()
    lead = _create_mock_lead(tenant_id)
    match = LeadPropertyMatch(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        lead_id=lead.id,
        property_id=uuid.uuid4(),
        match_score=85.0,
        status="SUGGESTED",
    )

    lead_res = MagicMock()
    lead_res.scalars.return_value.first.return_value = lead

    match_res = MagicMock()
    match_res.scalars.return_value.first.return_value = match

    mock_db.execute.side_effect = [lead_res, match_res]

    ctx = TenantContext(tenant_id=tenant_id, user_id=uuid.uuid4(), role=UserRole.MANAGER)
    svc = MatchingService(mock_db, ctx)

    updated = await svc.update_match_status(lead.id, match.id, status="SHORTLISTED")
    assert updated.status == "SHORTLISTED"
    mock_db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_update_match_status_not_found(mock_db):
    tenant_id = uuid.uuid4()
    lead = _create_mock_lead(tenant_id)

    lead_res = MagicMock()
    lead_res.scalars.return_value.first.return_value = lead

    match_res = MagicMock()
    match_res.scalars.return_value.first.return_value = None

    mock_db.execute.side_effect = [lead_res, match_res]

    ctx = TenantContext(tenant_id=tenant_id, user_id=uuid.uuid4(), role=UserRole.MANAGER)
    svc = MatchingService(mock_db, ctx)

    with pytest.raises(NotFoundError, match="Property match not found"):
        await svc.update_match_status(lead.id, uuid.uuid4(), status="REJECTED")
