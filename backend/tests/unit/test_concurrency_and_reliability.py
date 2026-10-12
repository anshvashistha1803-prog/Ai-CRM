"""
Unit tests for Concurrency, Reliability, and Edge-Case Recovery:
- Concurrent refresh-token rotation & revoked token reuse prevention
- Concurrent duplicate lead submissions & DB IntegrityError handling
- Concurrent site visit requests competing for agent or lead time slots
- Outbox message claiming, worker failure recovery, and lease expiration
- Safe reconciliation of uncertain external delivery outcomes
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from app.auth.service import AuthService, _hash_token
from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import (
    ConflictError,
    DuplicateLeadError,
    InvalidTokenError,
)
from app.core.security import create_refresh_token
from app.leads.schemas import LeadCreateRequest
from app.leads.service import LeadService
from app.messaging.models import OutboxMessage
from app.messaging.outbox_service import OutboxService
from app.users.models import RefreshToken, User
from app.visits.models import SiteVisit
from app.visits.schemas import SiteVisitCreateRequest
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
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.rollback = AsyncMock()
    return session


# ── Concurrency: Refresh Token Rotation & Reuse Prevention ────────────────────


@pytest.mark.asyncio
async def test_concurrent_refresh_token_reuse_prevention(mock_db):
    """
    Ensure that once a refresh token is rotated, any subsequent or concurrent attempt
    using the same token is rejected with InvalidTokenError.
    """
    user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    user = User()
    user.id = user_id
    user.tenant_id = tenant_id
    user.role = "agent"
    user.is_active = True
    user.is_deleted = False

    raw_token, _ = create_refresh_token(user_id, tenant_id)
    token_hash = _hash_token(raw_token)

    db_token = RefreshToken()
    db_token.id = uuid.uuid4()
    db_token.user_id = user_id
    db_token.token_hash = token_hash
    db_token.revoked_at = None
    db_token.expires_at = datetime.now(UTC) + timedelta(days=7)

    # First attempt: token is valid and not revoked
    mock_token_res1 = MagicMock()
    mock_token_res1.scalar_one_or_none.return_value = db_token
    mock_user_res1 = MagicMock()
    mock_user_res1.scalar_one_or_none.return_value = user

    mock_db.execute.side_effect = [mock_token_res1, mock_user_res1]

    auth_svc = AuthService(mock_db)
    acc1, ref1, _ = await auth_svc.refresh_access_token(raw_token)
    assert acc1 is not None
    assert ref1 is not None
    assert db_token.revoked_at is not None

    # Second attempt with the SAME token: db_token is now revoked (or not found in active query)
    mock_token_res2 = MagicMock()
    mock_token_res2.scalar_one_or_none.return_value = None
    mock_db.execute.side_effect = [mock_token_res2]

    with pytest.raises(InvalidTokenError, match="Refresh token not found or already used"):
        await auth_svc.refresh_access_token(raw_token)


# ── Concurrency: Duplicate Lead Submission Handling ──────────────────────────


@pytest.mark.asyncio
async def test_concurrent_duplicate_lead_submission_catches_integrity_error(mock_db):
    """
    Simulate two concurrent lead submissions passing the SELECT duplicate check simultaneously.
    The database unique constraint raises IntegrityError on the second insert.
    LeadService must cleanly catch IntegrityError and raise DuplicateLeadError.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    ctx = TenantContext(user_id=user_id, tenant_id=tenant_id, role=UserRole.AGENT)

    # 1st execute for duplicate check returns None (racing condition)
    mock_dup_check = MagicMock()
    mock_dup_check.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_dup_check

    # Flush raises IntegrityError from DB unique constraint
    mock_db.flush.side_effect = IntegrityError("duplicate key value", params={}, orig=Exception("uq_leads"))

    payload = LeadCreateRequest(
        source="WEBSITE",
        full_name="Duplicate Lead",
        phone="+919876543210",
        email="duplicate@example.com",
    )

    lead_svc = LeadService(mock_db, ctx)
    with pytest.raises(DuplicateLeadError, match="already exists"):
        await lead_svc.create_lead(payload)


@pytest.mark.asyncio
async def test_create_lead_unrelated_foreign_key_integrity_error_reraised(mock_db):
    """
    Ensure unrelated foreign-key integrity errors are NOT masked as DuplicateLeadError.
    They must be re-raised so callers and error handlers see the true cause.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    ctx = TenantContext(user_id=user_id, tenant_id=tenant_id, role=UserRole.AGENT)

    mock_dup_check = MagicMock()
    mock_dup_check.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_dup_check

    # Flush raises Foreign Key constraint failure
    mock_db.flush.side_effect = IntegrityError(
        "insert or update on table 'leads' violates foreign key constraint 'fk_leads_assigned_agent_id'",
        params={},
        orig=Exception("23503: foreign key violation"),
    )

    payload = LeadCreateRequest(
        source="WEBSITE",
        full_name="Valid Name",
        phone="+919876543210",
        email="agent@example.com",
    )

    lead_svc = LeadService(mock_db, ctx)
    with pytest.raises(IntegrityError, match="violates foreign key constraint"):
        await lead_svc.create_lead(payload)


@pytest.mark.asyncio
async def test_create_lead_unrelated_not_null_integrity_error_reraised(mock_db):
    """
    Ensure unrelated not-null constraint integrity errors are NOT masked as DuplicateLeadError.
    """
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    ctx = TenantContext(user_id=user_id, tenant_id=tenant_id, role=UserRole.AGENT)

    mock_dup_check = MagicMock()
    mock_dup_check.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_dup_check

    # Flush raises Not-Null constraint failure
    mock_db.flush.side_effect = IntegrityError(
        "null value in column 'full_name' violates not-null constraint",
        params={},
        orig=Exception("23502: not null violation"),
    )

    payload = LeadCreateRequest(
        source="WEBSITE",
        full_name="Valid Name",
        phone="+919876543210",
        email="agent@example.com",
    )

    lead_svc = LeadService(mock_db, ctx)
    with pytest.raises(IntegrityError, match="violates not-null constraint"):
        await lead_svc.create_lead(payload)


# ── Concurrency: Site Visits Conflicting Booking ──────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_visit_requests_detect_agent_conflict(mock_db):
    """
    When two booking requests compete for the same agent in overlapping time slots,
    the agent conflict check detects the existing booking and raises ConflictError.
    """
    tenant_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    ctx = TenantContext(user_id=agent_id, tenant_id=tenant_id, role=UserRole.AGENT)

    lead = MagicMock()
    lead.id = uuid.uuid4()
    lead.tenant_id = tenant_id

    prop = MagicMock()
    prop.id = uuid.uuid4()

    now = datetime.now(UTC)
    start_time = now + timedelta(days=2, hours=10)
    end_time = start_time + timedelta(hours=1)

    existing_visit = SiteVisit()
    existing_visit.id = uuid.uuid4()
    existing_visit.scheduled_start_at = start_time
    existing_visit.scheduled_end_at = end_time

    svc = SiteVisitService(mock_db, ctx)

    with (
        patch.object(svc, "_find_and_authorize_lead", return_value=lead),
        patch.object(svc, "_find_property", return_value=prop),
    ):
        mock_conflict_res = MagicMock()
        mock_conflict_res.scalars.return_value.first.return_value = existing_visit
        mock_db.execute.return_value = mock_conflict_res

        payload = SiteVisitCreateRequest(
            lead_id=lead.id,
            property_id=prop.id,
            assigned_agent_id=agent_id,
            scheduled_start_at=start_time + timedelta(minutes=15),
            scheduled_end_at=end_time + timedelta(minutes=15),
        )

        with pytest.raises(ConflictError, match="Agent is already scheduled"):
            await svc.schedule_visit(payload)


@pytest.mark.asyncio
async def test_concurrent_visit_requests_detect_lead_conflict(mock_db):
    """
    When two booking requests compete for the same lead and property in overlapping time slots,
    the lead conflict check detects the duplicate and raises ConflictError.
    """
    tenant_id = uuid.uuid4()
    ctx = TenantContext(user_id=uuid.uuid4(), tenant_id=tenant_id, role=UserRole.MANAGER)

    lead = MagicMock()
    lead.id = uuid.uuid4()
    lead.tenant_id = tenant_id

    prop = MagicMock()
    prop.id = uuid.uuid4()

    now = datetime.now(UTC)
    start_time = now + timedelta(days=2, hours=10)
    end_time = start_time + timedelta(hours=1)

    existing_visit = SiteVisit()
    existing_visit.id = uuid.uuid4()
    existing_visit.scheduled_start_at = start_time
    existing_visit.scheduled_end_at = end_time

    svc = SiteVisitService(mock_db, ctx)

    with (
        patch.object(svc, "_find_and_authorize_lead", return_value=lead),
        patch.object(svc, "_find_property", return_value=prop),
        patch.object(svc, "_check_agent_conflicts", return_value=None),
    ):
        mock_conflict_res = MagicMock()
        mock_conflict_res.scalars.return_value.first.return_value = existing_visit
        mock_db.execute.return_value = mock_conflict_res

        payload = SiteVisitCreateRequest(
            lead_id=lead.id,
            property_id=prop.id,
            scheduled_start_at=start_time,
            scheduled_end_at=end_time,
        )

        with pytest.raises(ConflictError, match="A site visit for this lead and property is already scheduled"):
            await svc.schedule_visit(payload)


# ── Outbox Reliability: Expired Lease Reclamation & Dead Lettering ────────────


@pytest.mark.asyncio
async def test_reclaim_stale_processing_messages_recovers_to_pending(mock_db):
    """
    Messages stuck in PROCESSING due to crashed workers are reclaimed back to PENDING
    with incremented retry count if under max_retries.
    """
    stale_msg = OutboxMessage()
    stale_msg.id = uuid.uuid4()
    stale_msg.status = "PROCESSING"
    stale_msg.retry_count = 0
    stale_msg.max_retries = 3
    stale_msg.updated_at = datetime.now(UTC) - timedelta(minutes=30)

    mock_res = MagicMock()
    mock_res.scalars.return_value.all.return_value = [stale_msg]
    mock_db.execute.return_value = mock_res

    svc = OutboxService(mock_db)
    reclaimed = await svc.reclaim_stale_processing_messages(lease_timeout_minutes=15)

    assert reclaimed == 1
    assert stale_msg.status == "PENDING"
    assert stale_msg.retry_count == 1
    assert "Worker lease expired" in stale_msg.last_error
    mock_db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_reclaim_stale_processing_messages_exhausted_to_dead_letter(mock_db):
    """
    Messages stuck in PROCESSING that have exhausted their retry limit are transitioned
    to DEAD_LETTER to prevent infinite loops.
    """
    exhausted_msg = OutboxMessage()
    exhausted_msg.id = uuid.uuid4()
    exhausted_msg.status = "PROCESSING"
    exhausted_msg.retry_count = 2
    exhausted_msg.max_retries = 3
    exhausted_msg.updated_at = datetime.now(UTC) - timedelta(minutes=30)

    mock_res = MagicMock()
    mock_res.scalars.return_value.all.return_value = [exhausted_msg]
    mock_db.execute.return_value = mock_res

    svc = OutboxService(mock_db)
    reclaimed = await svc.reclaim_stale_processing_messages(lease_timeout_minutes=15)

    assert reclaimed == 1
    assert exhausted_msg.status == "DEAD_LETTER"
    assert exhausted_msg.retry_count == 3
    assert exhausted_msg.processed_at is not None
    mock_db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_outbox_fetch_pending_messages_with_skip_locked(mock_db):
    """
    Verify that fetch_pending_messages executes a row-locked query
    to prevent duplicate claiming across parallel Celery workers.
    """
    mock_res = MagicMock()
    mock_res.scalars.return_value.all.return_value = []
    mock_db.execute.return_value = mock_res

    svc = OutboxService(mock_db)
    await svc.fetch_pending_messages(batch_size=20, lock_rows=True)

    mock_db.execute.assert_called_once()
    called_stmt = mock_db.execute.call_args[0][0]
    # Verify that the SQL statement includes FOR UPDATE / lock clause
    stmt_str = str(called_stmt)
    assert "FOR UPDATE" in stmt_str


@pytest.mark.asyncio
async def test_outbox_mark_sent_ignored_if_not_processing(mock_db):
    """
    Ensure that if an outdated/zombie worker wakes up and attempts to mark a message
    SENT when the message has already been reclaimed to PENDING or DEAD_LETTER,
    the update is safely ignored and does not overwrite the newer state.
    """
    msg = OutboxMessage()
    msg.id = uuid.uuid4()
    msg.status = "PENDING"  # Was reclaimed by lease recovery

    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = msg
    mock_db.execute.return_value = mock_res

    svc = OutboxService(mock_db)
    res = await svc.mark_sent(msg.id, provider_message_id="wamid.test.123")

    # Status must remain PENDING
    assert res.status == "PENDING"
    mock_db.flush.assert_not_called()


@pytest.mark.asyncio
async def test_outbox_mark_failed_ignored_if_not_processing(mock_db):
    """
    Ensure that if an outdated worker attempts to mark a message FAILED when it is
    no longer in PROCESSING status, the failure transition is safely ignored.
    """
    msg = OutboxMessage()
    msg.id = uuid.uuid4()
    msg.status = "DEAD_LETTER"

    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = msg
    mock_db.execute.return_value = mock_res

    svc = OutboxService(mock_db)
    res = await svc.mark_failed(msg.id, error_message="Delayed network error")

    assert res.status == "DEAD_LETTER"
    mock_db.flush.assert_not_called()


@pytest.mark.asyncio
async def test_outbox_mark_sent_persists_provider_message_id(mock_db):
    """
    Verify that mark_sent correctly records provider_message_id in the message payload
    for future delivery reconciliation with external webhooks.
    """
    msg = OutboxMessage()
    msg.id = uuid.uuid4()
    msg.status = "PROCESSING"
    msg.payload = {"recipient_phone": "+919876543210"}

    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = msg
    mock_db.execute.return_value = mock_res

    svc = OutboxService(mock_db)
    res = await svc.mark_sent(msg.id, provider_message_id="wamid.meta.99999")

    assert res.status == "SENT"
    assert res.payload["provider_message_id"] == "wamid.meta.99999"
    mock_db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_outbox_reconciliation_skips_duplicate_external_dispatch(mock_db):
    """
    When an outbox event already has provider_message_id in its payload (from a prior
    attempt where the worker crashed after provider dispatch but before commit),
    _handle_message_event must reconcile by returning the existing ID without calling
    the external provider again.
    """
    from app.tasks.celery_tasks import _handle_message_event

    msg = OutboxMessage()
    msg.id = uuid.uuid4()
    msg.event_type = "WHATSAPP_OUTBOUND_DISPATCH"
    msg.aggregate_type = "lead"
    msg.aggregate_id = str(uuid.uuid4())
    msg.payload = {
        "recipient_phone": "+919876543210",
        "message_text": "Hello client",
        "provider_message_id": "wamid.meta.existing.123",
    }

    with patch("app.tasks.celery_tasks.WhatsAppClient") as mock_wa:
        provider_id = await _handle_message_event(msg, mock_db)
        assert provider_id == "wamid.meta.existing.123"
        mock_wa.assert_not_called()

