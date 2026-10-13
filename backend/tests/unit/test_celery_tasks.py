"""
Unit tests for Celery background tasks:
- Outbox message event routing and processing
- Outbox batch processing and failure handling
- Stalled lead detection and automatic transition
- Celery task wrappers
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.messaging.models import OutboxMessage
from app.tasks.celery_tasks import (
    _detect_stalled_leads_async,
    _handle_message_event,
    _process_outbox_batch_async,
    detect_stalled_leads_batch,
    dispatch_outbox_message,
    process_outbox_batch,
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
    session.begin = MagicMock(side_effect=lambda: MockTx(session))
    session.begin_nested = MagicMock(side_effect=lambda: MockTx(session))
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.rollback = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_handle_message_event_whatsapp(mock_db):
    msg = OutboxMessage()
    msg.id = uuid.uuid4()
    msg.event_type = "WHATSAPP_OUTBOUND_DISPATCH"
    msg.aggregate_type = "lead"
    msg.aggregate_id = str(uuid.uuid4())
    msg.payload = {
        "recipient_phone": "+1234567890",
        "message_text": "Hello world",
    }

    # Should not raise
    await _handle_message_event(msg, mock_db)


@pytest.mark.asyncio
async def test_handle_message_event_ai_qualification(mock_db):
    lead_id = uuid.uuid4()
    msg = OutboxMessage()
    msg.id = uuid.uuid4()
    msg.event_type = "AI_QUALIFICATION_REQUESTED"
    msg.aggregate_type = "lead"
    msg.aggregate_id = str(lead_id)
    msg.payload = {"trigger": "manual", "message_text": "Hello", "sender_phone": "+1234567890"}

    with patch("app.qualification.conversation_service.QualificationConversationService.process_inbound_message", new_callable=AsyncMock) as mock_proc:
        await _handle_message_event(msg, mock_db)
        mock_proc.assert_called_once_with(lead_id=lead_id, message_text="Hello", sender_phone="+1234567890")


@pytest.mark.asyncio
async def test_handle_message_event_broker_handoff(mock_db):
    lead_id = uuid.uuid4()
    msg = OutboxMessage()
    msg.id = uuid.uuid4()
    msg.event_type = "BROKER_HANDOFF_NOTIFICATION"
    msg.aggregate_type = "lead"
    msg.aggregate_id = str(lead_id)
    msg.payload = {"lead_name": "Test Lead", "score": 85}

    await _handle_message_event(msg, mock_db)
    assert mock_db.add.called


@pytest.mark.asyncio
async def test_handle_message_event_sheets_sync_seller(mock_db):
    lead_id = uuid.uuid4()
    msg = OutboxMessage()
    msg.id = uuid.uuid4()
    msg.event_type = "SHEETS_SYNC_REQUESTED"
    msg.aggregate_type = "lead"
    msg.aggregate_id = str(lead_id)
    msg.payload = {"intent": "SELL", "answers": {"owner_name": "Dev", "owner_number": "8445121294"}}

    with patch("app.integrations.sheets.service.GoogleSheetsSyncService.sync_seller_listing", new_callable=AsyncMock) as mock_sync:
        await _handle_message_event(msg, mock_db)
        mock_sync.assert_called_once()


@pytest.mark.asyncio
async def test_handle_message_event_sheets_sync_buyer(mock_db):
    lead_id = uuid.uuid4()
    msg = OutboxMessage()
    msg.id = uuid.uuid4()
    msg.event_type = "SHEETS_SYNC_REQUESTED"
    msg.aggregate_type = "lead"
    msg.aggregate_id = str(lead_id)
    msg.payload = {"intent": "BUY", "answers": {"property_type": "FLAT"}}

    with patch("app.integrations.sheets.service.GoogleSheetsSyncService.sync_buyer_lead", new_callable=AsyncMock) as mock_sync:
        await _handle_message_event(msg, mock_db)
        mock_sync.assert_called_once()


@pytest.mark.asyncio
async def test_handle_message_event_site_visit(mock_db):
    visit_id = str(uuid.uuid4())
    msg = OutboxMessage()
    msg.id = uuid.uuid4()
    msg.event_type = "SITE_VISIT_NOTIFICATION"
    msg.aggregate_type = "visit"
    msg.aggregate_id = visit_id
    msg.payload = {"status": "SCHEDULED"}

    await _handle_message_event(msg, mock_db)


@pytest.mark.asyncio
async def test_handle_message_event_generic(mock_db):
    msg = OutboxMessage()
    msg.id = uuid.uuid4()
    msg.event_type = "UNKNOWN_CUSTOM_EVENT"
    msg.aggregate_type = "custom"
    msg.aggregate_id = "123"
    msg.payload = {}

    await _handle_message_event(msg, mock_db)


@pytest.mark.asyncio
async def test_process_outbox_batch_async_success(mock_db):
    msg1 = OutboxMessage()
    msg1.id = uuid.uuid4()
    msg1.event_type = "WHATSAPP_OUTBOUND_DISPATCH"
    msg1.aggregate_type = "lead"
    msg1.aggregate_id = str(uuid.uuid4())
    msg1.payload = {"recipient_phone": "+1234567890", "message_text": "Hello"}

    class MockAsyncContextManager:
        async def __aenter__(self):
            return mock_db

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_session_factory = MagicMock(return_value=MockAsyncContextManager())

    with (
        patch("app.tasks.celery_tasks._get_async_session", return_value=mock_session_factory),
        patch("app.messaging.outbox_service.OutboxService.reclaim_stale_processing_messages", new_callable=AsyncMock) as mock_reclaim,
        patch("app.messaging.outbox_service.OutboxService.fetch_pending_messages", new_callable=AsyncMock) as mock_fetch,
        patch("app.messaging.outbox_service.OutboxService.mark_sent", new_callable=AsyncMock) as mock_sent,
    ):
        mock_fetch.return_value = [msg1]

        count = await _process_outbox_batch_async()
        assert count == 1
        mock_reclaim.assert_called_once()
        assert msg1.status == "PROCESSING"
        assert mock_sent.call_args[0][0] == msg1.id
        assert mock_db.commit.called


@pytest.mark.asyncio
async def test_process_outbox_batch_async_failure(mock_db):
    msg1 = OutboxMessage()
    msg1.id = uuid.uuid4()
    msg1.event_type = "WHATSAPP_OUTBOUND_DISPATCH"
    msg1.aggregate_type = "lead"
    msg1.aggregate_id = str(uuid.uuid4())
    msg1.payload = {}

    class MockAsyncContextManager:
        async def __aenter__(self):
            return mock_db

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_session_factory = MagicMock(return_value=MockAsyncContextManager())

    with (
        patch("app.tasks.celery_tasks._get_async_session", return_value=mock_session_factory),
        patch("app.messaging.outbox_service.OutboxService.reclaim_stale_processing_messages", new_callable=AsyncMock),
        patch("app.messaging.outbox_service.OutboxService.fetch_pending_messages", new_callable=AsyncMock) as mock_fetch,
        patch("app.tasks.celery_tasks._handle_message_event", side_effect=RuntimeError("Dispatch error")),
        patch("app.messaging.outbox_service.OutboxService.mark_failed", new_callable=AsyncMock) as mock_failed,
    ):
        mock_fetch.return_value = [msg1]

        count = await _process_outbox_batch_async()
        assert count == 0
        assert msg1.status == "PROCESSING"
        mock_failed.assert_called_once()
        assert "Dispatch error" in mock_failed.call_args[1]["error_message"]


@pytest.mark.asyncio
async def test_detect_stalled_leads_async(mock_db):
    from app.leads.models import Lead

    lead = Lead()
    lead.id = uuid.uuid4()
    lead.tenant_id = uuid.uuid4()
    lead.stage = "NEW"
    lead.last_activity_at = datetime.now(UTC) - timedelta(days=40)
    lead.is_deleted = False

    mock_res = MagicMock()
    mock_res.scalars.return_value.all.return_value = [lead]
    mock_db.execute.return_value = mock_res

    class MockAsyncContextManager:
        async def __aenter__(self):
            return mock_db

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_session_factory = MagicMock(return_value=MockAsyncContextManager())

    with (
        patch("app.tasks.celery_tasks._get_async_session", return_value=mock_session_factory),
        patch("app.leads.service.LeadService.transition_stage", new_callable=AsyncMock) as mock_transition,
    ):
        count = await _detect_stalled_leads_async()
        assert count == 1
        mock_transition.assert_called_once()
        assert mock_db.commit.called


@pytest.mark.asyncio
async def test_detect_stalled_leads_async_handles_exception(mock_db):
    from app.leads.models import Lead

    lead = Lead()
    lead.id = uuid.uuid4()
    lead.tenant_id = uuid.uuid4()
    lead.stage = "NEW"
    lead.last_activity_at = datetime.now(UTC) - timedelta(days=40)
    lead.is_deleted = False

    mock_res = MagicMock()
    mock_res.scalars.return_value.all.return_value = [lead]
    mock_db.execute.return_value = mock_res

    class MockAsyncContextManager:
        async def __aenter__(self):
            return mock_db

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_session_factory = MagicMock(return_value=MockAsyncContextManager())

    with (
        patch("app.tasks.celery_tasks._get_async_session", return_value=mock_session_factory),
        patch("app.leads.service.LeadService.transition_stage", side_effect=Exception("Invalid transition")),
    ):
        count = await _detect_stalled_leads_async()
        assert count == 0


def test_process_outbox_batch_sync_wrapper():
    with patch("app.tasks.celery_tasks._process_outbox_batch_async", new_callable=AsyncMock) as mock_async:
        mock_async.return_value = 5
        result = process_outbox_batch()
        assert result == 5


def test_detect_stalled_leads_batch_sync_wrapper():
    with patch("app.tasks.celery_tasks._detect_stalled_leads_async", new_callable=AsyncMock) as mock_async:
        mock_async.return_value = 3
        result = detect_stalled_leads_batch()
        assert result == 3


def test_dispatch_outbox_message_success(mock_db):
    msg_id = uuid.uuid4()
    msg = OutboxMessage()
    msg.id = msg_id
    msg.event_type = "GENERIC_EVENT"
    msg.aggregate_type = "test"
    msg.aggregate_id = "test-id"
    msg.payload = {}

    class MockAsyncContextManager:
        async def __aenter__(self):
            return mock_db

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_session_factory = MagicMock(return_value=MockAsyncContextManager())

    with (
        patch("app.tasks.celery_tasks._get_async_session", return_value=mock_session_factory),
        patch("app.messaging.outbox_service.OutboxService.claim_message", new_callable=AsyncMock) as mock_claim,
        patch("app.messaging.outbox_service.OutboxService.mark_sent", new_callable=AsyncMock) as mock_sent,
    ):
        msg.status = "PROCESSING"
        msg.updated_at = datetime.now(UTC)
        mock_claim.return_value = msg
        result = dispatch_outbox_message(str(msg_id))
        assert result is True
        mock_claim.assert_called_once_with(msg_id)
        assert mock_sent.call_args[0][0] == msg_id
        assert mock_db.commit.called


def test_dispatch_outbox_message_failure(mock_db):
    msg_id = uuid.uuid4()
    msg = OutboxMessage()
    msg.id = msg_id
    msg.event_type = "GENERIC_EVENT"
    msg.aggregate_type = "test"
    msg.aggregate_id = "test-id"
    msg.payload = {}

    class MockAsyncContextManager:
        async def __aenter__(self):
            return mock_db

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    mock_session_factory = MagicMock(return_value=MockAsyncContextManager())

    with (
        patch("app.tasks.celery_tasks._get_async_session", return_value=mock_session_factory),
        patch("app.messaging.outbox_service.OutboxService.claim_message", new_callable=AsyncMock) as mock_claim,
        patch("app.tasks.celery_tasks._handle_message_event", side_effect=Exception("Delivery failed")),
        patch("app.messaging.outbox_service.OutboxService.mark_failed", new_callable=AsyncMock) as mock_failed,
    ):
        msg.status = "PROCESSING"
        msg.updated_at = datetime.now(UTC)
        mock_claim.return_value = msg
        result = dispatch_outbox_message(str(msg_id))
        assert result is False
        mock_claim.assert_called_once_with(msg_id)
        mock_failed.assert_called_once()
        assert mock_db.commit.called


def test_celery_task_queue_alignment():
    """
    Ensure every registered task is routed to an explicit queue that is consumed
    by the worker command in docker-compose.yml (-Q default,messages,qualification).
    """
    from app.tasks.celery_app import celery_app

    worker_queues = {"default", "messages", "qualification"}

    # 1. Default queue must be in worker queues (never default to unconsumed 'celery')
    assert celery_app.conf.task_default_queue in worker_queues

    # 2. Inspect registered tasks
    tasks = [
        process_outbox_batch,
        detect_stalled_leads_batch,
        dispatch_outbox_message,
    ]

    for task in tasks:
        effective_queue = (
            task.queue
            or celery_app.conf.task_routes.get(task.name, {}).get("queue")
            or celery_app.conf.task_default_queue
        )
        assert effective_queue in worker_queues, (
            f"Task {task.name} routes to {effective_queue}, not in worker queues {worker_queues}"
        )

    # 3. Check specific queue assignments
    assert process_outbox_batch.queue == "messages"
    assert dispatch_outbox_message.queue == "messages"
    assert detect_stalled_leads_batch.queue == "default"

    # 4. Celery Beat periodic schedules must route to worker queues
    beat_schedule = celery_app.conf.beat_schedule
    for schedule_name, schedule_cfg in beat_schedule.items():
        queue = (
            schedule_cfg.get("options", {}).get("queue")
            or celery_app.conf.task_routes.get(schedule_cfg["task"], {}).get("queue")
            or celery_app.conf.task_default_queue
        )
        assert queue in worker_queues, (
            f"Beat schedule {schedule_name} routes to {queue}, not in worker queues {worker_queues}"
        )


def test_compose_settings_mapping():
    """
    Verify that application Settings attributes correctly map to expected environment variable names.
    """
    from app.core.config import Settings

    field_names = set(Settings.model_fields.keys())
    assert "secret_key" in field_names
    assert "app_encryption_key" in field_names
    assert "jwt_private_key_path" in field_names
    assert "jwt_public_key_path" in field_names
    assert "jwt_algorithm" in field_names
    assert "jwt_access_token_expire_minutes" in field_names
    assert "jwt_refresh_token_expire_days" in field_names
    assert "whatsapp_verify_token" in field_names
    assert "whatsapp_app_secret" in field_names


def test_missing_jwt_key_fails_safely():
    """
    In RS256 mode, missing key files must fail closed with FileNotFoundError and never silently succeed.
    """
    from app.core.config import get_settings
    from app.core.security import _load_private_key

    settings = get_settings()
    original_path = settings.jwt_private_key_path
    try:
        settings.jwt_private_key_path = "./keys/non_existent_key_12345.pem"
        with pytest.raises(FileNotFoundError, match="JWT private key not found"):
            _load_private_key()
    finally:
        settings.jwt_private_key_path = original_path
