"""
Unit tests for QualificationService.
Verifies AI proposal generation, supersede behavior, agent/tenant isolation,
human review acceptance and rejection, and atomic field mutation on leads.
"""

import uuid
from datetime import UTC, datetime
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
from app.qualification.llm_adapter import MockLLMAdapter
from app.qualification.models import LeadQualificationProposal
from app.qualification.service import QualificationService


def _create_mock_lead(
    tenant_id: uuid.UUID,
    assigned_agent_id: uuid.UUID | None = None,
    stage: str = "NEW",
    raw_message_text: str | None = "Looking for 3 BHK in Whitefield budget 1.5 Cr",
) -> Lead:
    now = datetime.now(UTC)
    return Lead(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        full_name="Rajesh Kumar",
        stage=stage,
        assigned_agent_id=assigned_agent_id,
        raw_message_text=raw_message_text,
        created_at=now,
        updated_at=now,
        is_deleted=False,
    )


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
    session.begin = MagicMock(return_value=MockTx(session))
    session.begin_nested = MagicMock(return_value=MockTx(session))
    session.flush = AsyncMock()
    session.rollback = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_generate_proposal_success(mock_db):
    tenant_id = uuid.uuid4()
    lead = _create_mock_lead(tenant_id=tenant_id)

    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = mock_res

    ctx = TenantContext(tenant_id=tenant_id, user_id=uuid.uuid4(), role=UserRole.MANAGER)
    svc = QualificationService(mock_db, ctx, llm_adapter=MockLLMAdapter())

    proposal = await svc.generate_proposal(lead.id)

    assert proposal.lead_id == lead.id
    assert proposal.status == "PENDING_REVIEW"
    assert proposal.bhk == 3
    assert proposal.preferred_location == "Whitefield"
    assert proposal.budget_max == 15000000
    assert lead.stage == LeadStage.QUALIFYING.value
    mock_db.add.assert_called()


@pytest.mark.asyncio
async def test_generate_proposal_agent_isolation(mock_db):
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    other_agent_id = uuid.uuid4()
    lead = _create_mock_lead(tenant_id=tenant_id, assigned_agent_id=other_agent_id)

    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = mock_res

    ctx = TenantContext(tenant_id=tenant_id, user_id=agent_id, role=UserRole.AGENT)
    svc = QualificationService(mock_db, ctx, llm_adapter=MockLLMAdapter())

    with pytest.raises(PermissionDeniedError, match="Agents can only view or manage"):
        await svc.generate_proposal(lead.id)


@pytest.mark.asyncio
async def test_generate_proposal_no_text_raises_validation_error(mock_db):
    tenant_id = uuid.uuid4()
    lead = _create_mock_lead(tenant_id=tenant_id, raw_message_text=None)

    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = lead
    mock_db.execute.return_value = mock_res

    ctx = TenantContext(tenant_id=tenant_id, user_id=uuid.uuid4(), role=UserRole.MANAGER)
    svc = QualificationService(mock_db, ctx, llm_adapter=MockLLMAdapter())

    with pytest.raises(ValidationError, match="No conversation messages provided"):
        await svc.generate_proposal(lead.id, messages=None)


@pytest.mark.asyncio
async def test_review_proposal_accept(mock_db):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    lead = _create_mock_lead(tenant_id=tenant_id, stage=LeadStage.QUALIFYING.value)

    proposal = LeadQualificationProposal(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        lead_id=lead.id,
        status="PENDING_REVIEW",
        budget_min=10000000,
        budget_max=15000000,
        property_type="APARTMENT",
        bhk=3,
        preferred_location="Whitefield",
        purpose="BUY",
        move_in_timeline="IMMEDIATE",
        confidence={"bhk": 0.95},
    )

    # First call retrieves lead, second call retrieves proposal
    lead_res = MagicMock()
    lead_res.scalars.return_value.first.return_value = lead

    prop_res = MagicMock()
    prop_res.scalars.return_value.first.return_value = proposal

    mock_db.execute.side_effect = [lead_res, lead_res, prop_res]

    ctx = TenantContext(tenant_id=tenant_id, user_id=user_id, role=UserRole.MANAGER)
    svc = QualificationService(mock_db, ctx, llm_adapter=MockLLMAdapter())

    reviewed = await svc.review_proposal(
        lead_id=lead.id,
        proposal_id=proposal.id,
        action="ACCEPT",
        review_notes="Verified with client on call.",
    )

    assert reviewed.status == "ACCEPTED"
    assert reviewed.reviewed_by == user_id
    assert reviewed.review_notes == "Verified with client on call."
    # Verify lead fields were mutated
    assert lead.budget_max == 15000000
    assert lead.bhk == 3
    assert lead.preferred_location == "Whitefield"
    assert lead.property_type == "APARTMENT"
    assert lead.stage == LeadStage.QUALIFIED.value


@pytest.mark.asyncio
async def test_review_proposal_reject(mock_db):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    lead = _create_mock_lead(tenant_id=tenant_id, stage=LeadStage.QUALIFYING.value)

    proposal = LeadQualificationProposal(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        lead_id=lead.id,
        status="PENDING_REVIEW",
        budget_min=10000000,
        budget_max=15000000,
        property_type="APARTMENT",
        bhk=3,
        preferred_location="Whitefield",
    )

    lead_res = MagicMock()
    lead_res.scalars.return_value.first.return_value = lead

    prop_res = MagicMock()
    prop_res.scalars.return_value.first.return_value = proposal

    mock_db.execute.side_effect = [lead_res, lead_res, prop_res]

    ctx = TenantContext(tenant_id=tenant_id, user_id=user_id, role=UserRole.MANAGER)
    svc = QualificationService(mock_db, ctx, llm_adapter=MockLLMAdapter())

    reviewed = await svc.review_proposal(
        lead_id=lead.id,
        proposal_id=proposal.id,
        action="REJECT",
        review_notes="Incorrect location and budget.",
    )

    assert reviewed.status == "REJECTED"
    assert reviewed.reviewed_by == user_id
    # Lead fields must remain unchanged
    assert lead.bhk is None
    assert lead.stage == LeadStage.QUALIFYING.value


@pytest.mark.asyncio
async def test_review_proposal_already_reviewed_conflict(mock_db):
    tenant_id = uuid.uuid4()
    lead = _create_mock_lead(tenant_id=tenant_id)
    proposal = LeadQualificationProposal(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        lead_id=lead.id,
        status="ACCEPTED",
    )

    lead_res = MagicMock()
    lead_res.scalars.return_value.first.return_value = lead

    prop_res = MagicMock()
    prop_res.scalars.return_value.first.return_value = proposal

    mock_db.execute.side_effect = [lead_res, lead_res, prop_res]

    ctx = TenantContext(tenant_id=tenant_id, user_id=uuid.uuid4(), role=UserRole.MANAGER)
    svc = QualificationService(mock_db, ctx, llm_adapter=MockLLMAdapter())

    with pytest.raises(ConflictError, match="Proposal cannot be reviewed"):
        await svc.review_proposal(
            lead_id=lead.id,
            proposal_id=proposal.id,
            action="ACCEPT",
        )
