"""
Unit tests for LeadService authorization, tenant isolation, and transaction boundaries.

Tests cover:
- Agent access limited strictly to assigned leads (view, update, list, activities).
- Unassigned lead access rejected for Agents, permitted for Managers/Admins.
- Manager/Admin agency-wide access.
- Cross-agency access rejection (strict tenant isolation).
- Lead reassignment permissions (Agents rejected, Managers allowed).
- Cross-agency assignment rejection.
- Multi-table write transaction atomicity and rollback on failure.
- Duplicate lead rejection.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import (
    DuplicateLeadError,
    NotFoundError,
    PermissionDeniedError,
)
from app.core.security import compute_blind_hash, encrypt_field
from app.leads.models import Lead
from app.leads.schemas import (
    LeadCreateRequest,
    LeadUpdateRequest,
)
from app.leads.service import LeadService


def _create_mock_lead(
    tenant_id: uuid.UUID,
    assigned_agent_id: uuid.UUID | None = None,
    phone: str = "+919876543210",
    email: str = "test@example.com",
) -> Lead:
    """Helper to instantiate an in-memory Lead model."""
    now = datetime.now(UTC)
    return Lead(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        source="WEB_FORM",
        full_name="Rajesh Sharma",
        phone_e164_enc=encrypt_field(phone),
        email_enc=encrypt_field(email),
        phone_hash=compute_blind_hash(phone),
        email_hash=compute_blind_hash(email),
        preferred_contact="WHATSAPP",
        budget_min=5000000,
        budget_max=10000000,
        currency_code="INR",
        property_type="APARTMENT",
        bhk=3,
        preferred_location="Indiranagar, Bangalore",
        purpose="BUY",
        move_in_timeline="3_MONTHS",
        stage="NEW",
        lead_health="WARM",
        intent_score=75,
        automation_status="ACTIVE",
        assigned_agent_id=assigned_agent_id,
        do_not_contact=False,
        is_deleted=False,
        last_activity_at=now,
        created_at=now,
        updated_at=now,
    )


class MockTransactionContext:
    """Async context manager mocking session.begin_nested() or session.begin()."""

    def __init__(self, session):
        self.session = session
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rolled_back = True
            await self.session.rollback()
        else:
            self.committed = True


@pytest.fixture
def mock_db():
    session = AsyncMock()
    session.add = MagicMock()
    session.in_transaction = MagicMock(return_value=True)
    session.begin_nested = MagicMock(side_effect=lambda: MockTransactionContext(session))
    session.begin = MagicMock(side_effect=lambda: MockTransactionContext(session))
    return session


@pytest.fixture
def tenant_a():
    return uuid.uuid4()


@pytest.fixture
def tenant_b():
    return uuid.uuid4()


@pytest.fixture
def agent_1_id():
    return uuid.uuid4()


@pytest.fixture
def agent_2_id():
    return uuid.uuid4()


@pytest.fixture
def manager_id():
    return uuid.uuid4()


@pytest.mark.asyncio
class TestLeadAuthorizationAndIsolation:
    """Tests verifying role-based authorization and strict tenant boundaries."""

    async def test_agent_can_access_own_assigned_lead(self, mock_db, tenant_a, agent_1_id):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = lead
        mock_db.execute.return_value = mock_result

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        result = await service.get_lead(lead.id)
        assert result.id == lead.id
        assert result.assigned_agent_id == agent_1_id

    async def test_agent_cannot_access_unassigned_lead(self, mock_db, tenant_a, agent_1_id):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=None)
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = lead
        mock_db.execute.return_value = mock_result

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        with pytest.raises(PermissionDeniedError, match="Agents can only access leads assigned to them"):
            await service.get_lead(lead.id)

    async def test_agent_cannot_access_other_agents_lead(self, mock_db, tenant_a, agent_1_id, agent_2_id):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_2_id)
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = lead
        mock_db.execute.return_value = mock_result

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        with pytest.raises(PermissionDeniedError, match="Agents can only access leads assigned to them"):
            await service.get_lead(lead.id)

    async def test_manager_can_access_unassigned_lead(self, mock_db, tenant_a, manager_id):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=None)
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = lead
        mock_db.execute.return_value = mock_result

        context = TenantContext(user_id=manager_id, tenant_id=tenant_a, role=UserRole.MANAGER)
        service = LeadService(mock_db, context)

        result = await service.get_lead(lead.id)
        assert result.id == lead.id
        assert result.assigned_agent_id is None

    async def test_manager_can_access_any_agent_lead_in_agency(self, mock_db, tenant_a, manager_id, agent_1_id):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = lead
        mock_db.execute.return_value = mock_result

        context = TenantContext(user_id=manager_id, tenant_id=tenant_a, role=UserRole.MANAGER)
        service = LeadService(mock_db, context)

        result = await service.get_lead(lead.id)
        assert result.id == lead.id
        assert result.assigned_agent_id == agent_1_id

    async def test_cross_tenant_access_rejected_as_not_found(self, mock_db, tenant_a, tenant_b, manager_id):
        # Lead belongs to Tenant B, requester is Tenant A manager
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = None  # Tenant A query finds nothing
        mock_db.execute.return_value = mock_result

        context = TenantContext(user_id=manager_id, tenant_id=tenant_a, role=UserRole.MANAGER)
        service = LeadService(mock_db, context)

        with pytest.raises(NotFoundError):
            await service.get_lead(uuid.uuid4())

    async def test_agent_list_leads_filters_to_assigned_agent(self, mock_db, tenant_a, agent_1_id):
        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        # Mock count and list queries
        count_res = MagicMock()
        count_res.scalar_one.return_value = 0
        list_res = MagicMock()
        list_res.scalars.return_value.all.return_value = []
        mock_db.execute.side_effect = [count_res, list_res]

        await service.list_leads()
        executed_stmts = [call[0][0] for call in mock_db.execute.call_args_list]
        count_sql = str(executed_stmts[0])
        assert "leads.assigned_agent_id =" in count_sql

    async def test_agent_cannot_filter_by_different_agent_id(self, mock_db, tenant_a, agent_1_id, agent_2_id):
        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        with pytest.raises(PermissionDeniedError, match="Agents can only access their assigned leads"):
            await service.list_leads(assigned_agent_id=agent_2_id)

    async def test_agent_cannot_reassign_lead_on_update(self, mock_db, tenant_a, agent_1_id, agent_2_id):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = lead
        mock_db.execute.return_value = mock_result

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        update_payload = LeadUpdateRequest(assigned_agent_id=agent_2_id)
        with pytest.raises(PermissionDeniedError, match="Agents are not authorized to reassign leads"):
            await service.update_lead(lead.id, update_payload)

    async def test_manager_cannot_assign_lead_to_cross_agency_user(self, mock_db, tenant_a, manager_id):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=None)

        lead_res = MagicMock()
        lead_res.scalars.return_value.first.return_value = lead
        user_check_res = MagicMock()
        user_check_res.scalar_one_or_none.return_value = None  # user not found in tenant_a

        mock_db.execute.side_effect = [lead_res, user_check_res]

        context = TenantContext(user_id=manager_id, tenant_id=tenant_a, role=UserRole.MANAGER)
        service = LeadService(mock_db, context)

        foreign_user_id = uuid.uuid4()
        with pytest.raises(PermissionDeniedError, match="Assigned agent does not belong to this agency"):
            await service.assign_lead(lead.id, foreign_user_id)

    async def test_manager_can_reassign_lead(self, mock_db, tenant_a, manager_id, agent_1_id):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=None)

        lead_res = MagicMock()
        lead_res.scalars.return_value.first.return_value = lead
        user_check_res = MagicMock()
        user_check_res.scalar_one_or_none.return_value = agent_1_id

        mock_db.execute.side_effect = [lead_res, user_check_res]

        context = TenantContext(user_id=manager_id, tenant_id=tenant_a, role=UserRole.MANAGER)
        service = LeadService(mock_db, context)

        result = await service.assign_lead(lead.id, agent_1_id)
        assert result.assigned_agent_id == agent_1_id
        assert lead.assigned_agent_id == agent_1_id

    async def test_super_admin_lead_access_is_logged(self, mock_db, tenant_a, agent_1_id):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = lead
        mock_db.execute.return_value = mock_result

        super_admin_id = uuid.uuid4()
        context = TenantContext(user_id=super_admin_id, tenant_id=tenant_a, role=UserRole.SUPER_ADMIN)
        service = LeadService(mock_db, context)

        with patch("app.leads.service.logger.info") as mock_log:
            await service.get_lead(lead.id)
            mock_log.assert_called_with(
                "super_admin_lead_access",
                super_admin_id=str(super_admin_id),
                tenant_id=str(tenant_a),
                lead_id=str(lead.id),
            )


@pytest.mark.asyncio
class TestTransactionCorrectnessAndRollback:
    """Tests proving atomicity and rollback of multi-table writes."""

    async def test_create_lead_rolls_back_when_activity_fails(self, mock_db, tenant_a, agent_1_id):
        # Deduplication query returns None (no duplicate)
        dup_res = MagicMock()
        dup_res.scalars.return_value.first.return_value = None
        mock_db.execute.return_value = dup_res

        # Fail on activity insertion / flush
        mock_db.flush.side_effect = [None, RuntimeError("Database write failure during activity log")]

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        payload = LeadCreateRequest(
            full_name="Vikram Seth",
            phone="+919876543210",
            email="vikram@example.com",
            source="WEB_FORM",
            consent_granted=True,
        )

        with pytest.raises(RuntimeError, match="Database write failure"):
            await service.create_lead(payload)

        # Confirm rollback was invoked
        mock_db.rollback.assert_awaited()

    async def test_duplicate_phone_rejected_without_mutation(self, mock_db, tenant_a, agent_1_id):
        existing_lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        dup_res = MagicMock()
        dup_res.scalars.return_value.first.return_value = existing_lead
        mock_db.execute.return_value = dup_res

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        payload = LeadCreateRequest(
            full_name="Duplicate Vikram",
            phone="+919876543210",
            source="WEB_FORM",
        )

        with pytest.raises(DuplicateLeadError, match="already exists"):
            await service.create_lead(payload)

        # Nothing was added to the db session
        mock_db.add.assert_not_called()

    async def test_update_duplicate_phone_rejected(self, mock_db, tenant_a, agent_1_id):
        target_lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        other_lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)

        find_res = MagicMock()
        find_res.scalars.return_value.first.return_value = target_lead

        dup_res = MagicMock()
        dup_res.scalars.return_value.first.return_value = other_lead

        mock_db.execute.side_effect = [find_res, dup_res]

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        update_payload = LeadUpdateRequest(phone="+919999999999")
        with pytest.raises(DuplicateLeadError, match="Another lead already has this phone number"):
            await service.update_lead(target_lead.id, update_payload)


@pytest.mark.asyncio
class TestLeadStageTransitions:
    """Tests covering LeadService.transition_stage state machine enforcement."""

    async def test_agent_valid_transition_new_to_qualifying(self, mock_db, tenant_a, agent_1_id):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        lead.stage = "NEW"

        find_res = MagicMock()
        find_res.scalars.return_value.first.return_value = lead
        mock_db.execute.return_value = find_res

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        from app.leads.state_machine import LeadStage
        res = await service.transition_stage(lead.id, LeadStage.QUALIFYING, reason="Started WhatsApp conversation")

        assert res.stage == "QUALIFYING"
        assert lead.stage == "QUALIFYING"
        assert mock_db.add.called
        mock_db.flush.assert_awaited()

    async def test_agent_skip_stage_rejected(self, mock_db, tenant_a, agent_1_id):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        lead.stage = "NEW"

        find_res = MagicMock()
        find_res.scalars.return_value.first.return_value = lead
        mock_db.execute.return_value = find_res

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        from app.core.exceptions import InvalidTransitionError
        from app.leads.state_machine import LeadStage

        with pytest.raises(InvalidTransitionError, match="not permitted"):
            await service.transition_stage(lead.id, LeadStage.WON)

    async def test_agent_disqualify_rejected_requires_manager(self, mock_db, tenant_a, agent_1_id):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        lead.stage = "QUALIFYING"

        find_res = MagicMock()
        find_res.scalars.return_value.first.return_value = lead
        mock_db.execute.return_value = find_res

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        from app.core.exceptions import InvalidTransitionError
        from app.leads.state_machine import LeadStage

        with pytest.raises(InvalidTransitionError, match="requires MANAGER role"):
            await service.transition_stage(lead.id, LeadStage.DISQUALIFIED)

    async def test_manager_disqualify_allowed(self, mock_db, tenant_a):
        lead = _create_mock_lead(tenant_id=tenant_a)
        lead.stage = "QUALIFYING"

        find_res = MagicMock()
        find_res.scalars.return_value.first.return_value = lead
        mock_db.execute.return_value = find_res

        context = TenantContext(user_id=uuid.uuid4(), tenant_id=tenant_a, role=UserRole.MANAGER)
        service = LeadService(mock_db, context)

        from app.leads.state_machine import LeadStage
        res = await service.transition_stage(lead.id, LeadStage.DISQUALIFIED, reason="Budget unrealistic")

        assert res.stage == "DISQUALIFIED"
        assert lead.stage == "DISQUALIFIED"

    async def test_stalled_system_only_rejected_for_user(self, mock_db, tenant_a):
        lead = _create_mock_lead(tenant_id=tenant_a)
        lead.stage = "QUALIFYING"

        find_res = MagicMock()
        find_res.scalars.return_value.first.return_value = lead
        mock_db.execute.return_value = find_res

        context = TenantContext(user_id=uuid.uuid4(), tenant_id=tenant_a, role=UserRole.MANAGER)
        service = LeadService(mock_db, context)

        from app.core.exceptions import InvalidTransitionError
        from app.leads.state_machine import LeadStage

        with pytest.raises(InvalidTransitionError, match="can only be triggered by the system"):
            await service.transition_stage(lead.id, LeadStage.STALLED, is_system=False)

    async def test_stalled_system_only_allowed_with_is_system(self, mock_db, tenant_a):
        lead = _create_mock_lead(tenant_id=tenant_a)
        lead.stage = "QUALIFYING"

        find_res = MagicMock()
        find_res.scalars.return_value.first.return_value = lead
        mock_db.execute.return_value = find_res

        context = TenantContext(user_id=uuid.uuid4(), tenant_id=tenant_a, role=UserRole.SUPER_ADMIN)
        service = LeadService(mock_db, context)

        from app.leads.state_machine import LeadStage
        res = await service.transition_stage(lead.id, LeadStage.STALLED, is_system=True)

        assert res.stage == "STALLED"
        assert lead.stage == "STALLED"


@pytest.mark.asyncio
class TestLeadQualificationStageEnforcement:
    """Regression test suite for qualification integrity and atomic outbox property matching trigger."""

    def _setup_mock_execute(self, lead, proposal=None, existing_outbox=None):
        async def _execute(stmt, *args, **kwargs):
            res = MagicMock()
            stmt_str = str(stmt).lower()
            if "lead_qualification_proposals" in stmt_str:
                res.scalars.return_value.first.return_value = proposal
                res.scalars.return_value.all.return_value = [proposal] if proposal else []
            elif "outbox_messages" in stmt_str:
                res.scalars.return_value.first.return_value = existing_outbox
                res.scalars.return_value.all.return_value = [existing_outbox] if existing_outbox else []
            elif "leads" in stmt_str:
                res.scalars.return_value.first.return_value = lead
                res.scalars.return_value.all.return_value = [lead] if lead else []
            else:
                res.scalars.return_value.first.return_value = None
                res.scalars.return_value.all.return_value = []
            return res
        return _execute

    async def test_qualifying_to_qualified_without_evidence_rejected(self, mock_db, tenant_a, agent_1_id):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        lead.stage = "QUALIFYING"
        lead.score_breakdown = None

        mock_db.execute.side_effect = self._setup_mock_execute(lead, proposal=None)

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        from app.core.exceptions import InvalidTransitionError
        from app.leads.state_machine import LeadStage

        with pytest.raises(InvalidTransitionError, match="without accepted qualification evidence"):
            await service.transition_stage(lead.id, LeadStage.QUALIFIED)

        assert lead.stage == "QUALIFYING"
        # Ensure no activity or outbox was added
        for call_args in mock_db.add.call_args_list:
            added = call_args[0][0]
            from app.messaging.models import OutboxMessage
            assert not isinstance(added, OutboxMessage)

    async def test_qualifying_to_qualified_with_pending_proposal_rejected(self, mock_db, tenant_a, agent_1_id):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        lead.stage = "QUALIFYING"
        lead.score_breakdown = None

        from app.qualification.models import LeadQualificationProposal
        pending_prop = LeadQualificationProposal(
            id=uuid.uuid4(),
            lead_id=lead.id,
            tenant_id=tenant_a,
            status="PENDING_REVIEW",
            proposed_by="AI",
            budget_max=15000000,
            confidence={"budget": 0.8},
        )

        mock_db.execute.side_effect = self._setup_mock_execute(lead, proposal=pending_prop)

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        from app.core.exceptions import InvalidTransitionError
        from app.leads.state_machine import LeadStage

        with pytest.raises(InvalidTransitionError, match="without accepted qualification evidence"):
            await service.transition_stage(lead.id, LeadStage.QUALIFIED)

        assert lead.stage == "QUALIFYING"

    async def test_qualifying_to_qualified_with_rejected_proposal_rejected(self, mock_db, tenant_a, agent_1_id):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        lead.stage = "QUALIFYING"
        lead.score_breakdown = None

        from app.qualification.models import LeadQualificationProposal
        rejected_prop = LeadQualificationProposal(
            id=uuid.uuid4(),
            lead_id=lead.id,
            tenant_id=tenant_a,
            status="REJECTED",
            proposed_by="AI",
            confidence={},
        )

        mock_db.execute.side_effect = self._setup_mock_execute(lead, proposal=rejected_prop)

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        from app.core.exceptions import InvalidTransitionError
        from app.leads.state_machine import LeadStage

        with pytest.raises(InvalidTransitionError, match="without accepted qualification evidence"):
            await service.transition_stage(lead.id, LeadStage.QUALIFIED)

        assert lead.stage == "QUALIFYING"

    async def test_qualifying_to_qualified_with_accepted_proposal_succeeds_and_enqueues_match(
        self, mock_db, tenant_a, agent_1_id
    ):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        lead.stage = "QUALIFYING"
        lead.intent_score = None
        lead.lead_health = "UNKNOWN"
        lead.score_breakdown = None

        from app.qualification.models import LeadQualificationProposal
        accepted_prop = LeadQualificationProposal(
            id=uuid.uuid4(),
            lead_id=lead.id,
            tenant_id=tenant_a,
            status="ACCEPTED",
            proposed_by="AI",
            budget_min=5000000,
            budget_max=10000000,
            property_type="APARTMENT",
            bhk=3,
            preferred_location="Whitefield",
            purpose="BUY",
            move_in_timeline="IMMEDIATE",
            confidence={"budget": 0.95},
        )

        mock_db.execute.side_effect = self._setup_mock_execute(lead, proposal=accepted_prop)

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        from app.leads.state_machine import LeadStage
        res = await service.transition_stage(lead.id, LeadStage.QUALIFIED, reason="Proposal accepted")

        assert res.stage == "QUALIFIED"
        assert lead.stage == "QUALIFIED"
        # Deterministic score & health persisted within transaction
        assert lead.intent_score is not None
        assert lead.intent_score > 0
        assert lead.lead_health in ("HOT", "WARM", "COLD", "INSUFFICIENT_DATA")
        assert lead.lead_health != "UNKNOWN"
        assert lead.score_breakdown is not None

        # Verify Outbox message enqueued
        from app.messaging.models import OutboxMessage
        added_objects = [call_args[0][0] for call_args in mock_db.add.call_args_list]
        outbox_msgs = [obj for obj in added_objects if isinstance(obj, OutboxMessage)]
        assert len(outbox_msgs) == 1
        msg = outbox_msgs[0]
        assert msg.event_type == "MATCH_COMPUTATION_REQUESTED"
        assert msg.aggregate_type == "LEAD"
        assert msg.aggregate_id == str(lead.id)
        assert msg.idempotency_key == f"match-compute-{lead.id}"
        assert msg.payload["lead_id"] == str(lead.id)
        assert msg.payload["score"] == lead.intent_score

    async def test_qualifying_to_qualified_with_completed_conversation_succeeds(
        self, mock_db, tenant_a, agent_1_id
    ):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        lead.stage = "QUALIFYING"
        lead.score_breakdown = {
            "conversation_state": {
                "current_node": "COMPLETED",
                "answers": {"intent": "BUY", "property_type": "APARTMENT"},
            }
        }
        lead.intent_score = 65
        lead.lead_health = "WARM"

        mock_db.execute.side_effect = self._setup_mock_execute(lead, proposal=None)

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        from app.leads.state_machine import LeadStage
        res = await service.transition_stage(lead.id, LeadStage.QUALIFIED)

        assert res.stage == "QUALIFIED"
        assert lead.stage == "QUALIFIED"
        # Verify outbox event was enqueued
        from app.messaging.models import OutboxMessage
        added_objects = [call_args[0][0] for call_args in mock_db.add.call_args_list]
        outbox_msgs = [obj for obj in added_objects if isinstance(obj, OutboxMessage)]
        assert len(outbox_msgs) == 1
        assert outbox_msgs[0].event_type == "MATCH_COMPUTATION_REQUESTED"

    async def test_manager_rollback_from_site_visit_to_qualified_succeeds(
        self, mock_db, tenant_a
    ):
        lead = _create_mock_lead(tenant_id=tenant_a)
        lead.stage = "SITE_VISIT_SCHEDULED"
        lead.intent_score = 80
        lead.lead_health = "HOT"

        mock_db.execute.side_effect = self._setup_mock_execute(lead, proposal=None)

        context = TenantContext(user_id=uuid.uuid4(), tenant_id=tenant_a, role=UserRole.MANAGER)
        service = LeadService(mock_db, context)

        from app.leads.state_machine import LeadStage
        res = await service.transition_stage(lead.id, LeadStage.QUALIFIED, reason="Visit cancelled by client")

        assert res.stage == "QUALIFIED"
        assert lead.stage == "QUALIFIED"
        assert lead.intent_score == 80
        assert lead.lead_health == "HOT"

    async def test_agent_rollback_from_site_visit_to_qualified_rejected(
        self, mock_db, tenant_a, agent_1_id
    ):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        lead.stage = "SITE_VISIT_SCHEDULED"
        lead.intent_score = 80
        lead.lead_health = "HOT"

        mock_db.execute.side_effect = self._setup_mock_execute(lead, proposal=None)

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        from app.core.exceptions import InvalidTransitionError
        from app.leads.state_machine import LeadStage

        with pytest.raises(InvalidTransitionError, match="requires MANAGER role"):
            await service.transition_stage(lead.id, LeadStage.QUALIFIED)

    async def test_repeated_transition_to_qualified_does_not_duplicate_outbox(
        self, mock_db, tenant_a, agent_1_id
    ):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        lead.stage = "QUALIFIED"
        lead.intent_score = 75
        lead.lead_health = "WARM"

        mock_db.execute.side_effect = self._setup_mock_execute(lead, proposal=None)

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        from app.leads.state_machine import LeadStage
        res = await service.transition_stage(lead.id, LeadStage.QUALIFIED)

        assert res.stage == "QUALIFIED"
        # No-op: should not add any outbox message
        added_objects = [call_args[0][0] for call_args in mock_db.add.call_args_list]
        from app.messaging.models import OutboxMessage
        outbox_msgs = [obj for obj in added_objects if isinstance(obj, OutboxMessage)]
        assert len(outbox_msgs) == 0

    async def test_outbox_deduplication_when_event_already_exists(
        self, mock_db, tenant_a, agent_1_id
    ):
        lead = _create_mock_lead(tenant_id=tenant_a, assigned_agent_id=agent_1_id)
        lead.stage = "QUALIFYING"
        lead.intent_score = 70
        lead.lead_health = "HOT"

        from app.qualification.models import LeadQualificationProposal
        accepted_prop = LeadQualificationProposal(
            id=uuid.uuid4(),
            lead_id=lead.id,
            tenant_id=tenant_a,
            status="ACCEPTED",
            proposed_by="AI",
            confidence={},
        )

        from app.messaging.models import OutboxMessage
        existing_msg = OutboxMessage(
            id=uuid.uuid4(),
            tenant_id=tenant_a,
            event_type="MATCH_COMPUTATION_REQUESTED",
            aggregate_type="LEAD",
            aggregate_id=str(lead.id),
            payload={"lead_id": str(lead.id)},
            status="PENDING",
            idempotency_key=f"match-compute-{lead.id}",
        )

        mock_db.execute.side_effect = self._setup_mock_execute(
            lead, proposal=accepted_prop, existing_outbox=existing_msg
        )

        context = TenantContext(user_id=agent_1_id, tenant_id=tenant_a, role=UserRole.AGENT)
        service = LeadService(mock_db, context)

        from app.leads.state_machine import LeadStage
        res = await service.transition_stage(lead.id, LeadStage.QUALIFIED)

        assert res.stage == "QUALIFIED"
        # Since existing_outbox was returned by query, OutboxService did not add a new OutboxMessage
        added_objects = [call_args[0][0] for call_args in mock_db.add.call_args_list]
        outbox_msgs = [obj for obj in added_objects if isinstance(obj, OutboxMessage)]
        assert len(outbox_msgs) == 0

