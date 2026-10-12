"""
Unit tests for OutboxService and Celery task configurations.
Verifies durable task queuing, idempotency deduplication, status transitions,
exponential backoff retries, and dead-letter queue handling.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.messaging.models import OutboxMessage
from app.messaging.outbox_service import OutboxService
from app.tasks.celery_app import celery_app


@pytest.fixture
def mock_db():
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_enqueue_message_new(mock_db):
    tenant_id = uuid.uuid4()
    lead_id = uuid.uuid4()

    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_res

    svc = OutboxService(mock_db)
    msg = await svc.enqueue_message(
        tenant_id=tenant_id,
        event_type="WHATSAPP_OUTBOUND_DISPATCH",
        aggregate_type="LEAD",
        aggregate_id=lead_id,
        payload={"message": "Hello from CRM"},
        idempotency_key="msg-12345",
    )

    assert msg.tenant_id == tenant_id
    assert msg.event_type == "WHATSAPP_OUTBOUND_DISPATCH"
    assert msg.status == "PENDING"
    assert msg.retry_count == 0
    assert msg.max_retries == 3
    mock_db.add.assert_called_once()
    mock_db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_enqueue_message_idempotent_deduplication(mock_db):
    tenant_id = uuid.uuid4()
    existing_msg = OutboxMessage(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        event_type="WHATSAPP_OUTBOUND_DISPATCH",
        aggregate_type="LEAD",
        aggregate_id=str(uuid.uuid4()),
        payload={"message": "Existing"},
        idempotency_key="idemp-key-999",
        status="SENT",
    )

    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = existing_msg
    mock_db.execute.return_value = mock_res

    svc = OutboxService(mock_db)
    result = await svc.enqueue_message(
        tenant_id=tenant_id,
        event_type="WHATSAPP_OUTBOUND_DISPATCH",
        aggregate_type="LEAD",
        aggregate_id=uuid.uuid4(),
        payload={"message": "Duplicate"},
        idempotency_key="idemp-key-999",
    )

    assert result.id == existing_msg.id
    assert result.status == "SENT"
    # Should NOT call session.add because it was already present
    mock_db.add.assert_not_called()


@pytest.mark.asyncio
async def test_mark_processing_and_sent(mock_db):
    msg_id = uuid.uuid4()
    msg = OutboxMessage(
        id=msg_id,
        tenant_id=uuid.uuid4(),
        event_type="LEAD_CREATED",
        aggregate_type="LEAD",
        aggregate_id=str(uuid.uuid4()),
        status="PENDING",
    )

    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = msg
    mock_db.execute.return_value = mock_res

    svc = OutboxService(mock_db)
    proc_msg = await svc.mark_processing(msg_id)
    assert proc_msg.status == "PROCESSING"

    sent_msg = await svc.mark_sent(msg_id)
    assert sent_msg.status == "SENT"
    assert sent_msg.processed_at is not None
    assert mock_db.flush.await_count == 2


@pytest.mark.asyncio
async def test_mark_failed_with_backoff(mock_db):
    msg_id = uuid.uuid4()
    msg = OutboxMessage(
        id=msg_id,
        tenant_id=uuid.uuid4(),
        event_type="WHATSAPP_OUTBOUND_DISPATCH",
        aggregate_type="LEAD",
        aggregate_id=str(uuid.uuid4()),
        status="PROCESSING",
        retry_count=0,
        max_retries=3,
        scheduled_at=datetime.now(UTC),
    )

    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = msg
    mock_db.execute.return_value = mock_res

    svc = OutboxService(mock_db)
    failed_msg = await svc.mark_failed(
        msg_id,
        error_message="Meta API HTTP 500 error",
        base_backoff_seconds=30,
    )

    assert failed_msg.status == "PENDING"
    assert failed_msg.retry_count == 1
    assert failed_msg.last_error == "Meta API HTTP 500 error"
    # Backoff for first retry = 30 * 2^0 = 30 seconds
    assert failed_msg.scheduled_at > datetime.now(UTC) + timedelta(seconds=20)


@pytest.mark.asyncio
async def test_mark_failed_dead_letter_on_max_retries(mock_db):
    msg_id = uuid.uuid4()
    msg = OutboxMessage(
        id=msg_id,
        tenant_id=uuid.uuid4(),
        event_type="WHATSAPP_OUTBOUND_DISPATCH",
        aggregate_type="LEAD",
        aggregate_id=str(uuid.uuid4()),
        status="PROCESSING",
        retry_count=2,  # Already failed twice
        max_retries=3,
    )

    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = msg
    mock_db.execute.return_value = mock_res

    svc = OutboxService(mock_db)
    dl_msg = await svc.mark_failed(
        msg_id,
        error_message="Fatal recipient phone number invalid",
    )

    assert dl_msg.status == "DEAD_LETTER"
    assert dl_msg.retry_count == 3
    assert dl_msg.processed_at is not None


def test_celery_app_configuration():
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.task_serializer == "json"
    assert "process-outbox-every-30s" in celery_app.conf.beat_schedule
    assert "detect-stalled-leads-daily" in celery_app.conf.beat_schedule
