"""
Unit tests for Phase 10 Production Readiness & Reliability Guards:
- PII Log Redaction for WhatsApp Client
- Fail-fast test database protection in Celery sessionmaker
- Outbox dead-letter inspection, idempotency guards, and operator recovery
- Meta Lead Ads Webhook verification and signature validation
"""

import hashlib
import hmac
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.exceptions import AuthenticationError, ConflictError
from app.messaging.models import OutboxMessage
from app.messaging.outbox_service import OutboxService
from app.messaging.whatsapp_client import mask_phone_for_logging
from app.tasks.celery_tasks import _get_async_session
from app.webhooks.meta_service import MetaLeadAdsWebhookService


# ── 1. PII Log Redaction Tests ────────────────────────────────────────────────
def test_mask_phone_for_logging():
    assert mask_phone_for_logging("+919876543210") == "+91******3210"
    assert "+919876543210"[3:-4] not in mask_phone_for_logging("+919876543210")
    assert mask_phone_for_logging("+14155552671") == "+14*****2671"
    assert mask_phone_for_logging("") == "[EMPTY]"
    assert mask_phone_for_logging("123") == "***"


def test_whatsapp_client_logs_do_not_expose_raw_phone():
    import asyncio

    from app.messaging.whatsapp_client import WhatsAppClient

    client = WhatsAppClient(force_mock=True)
    raw_phone = "+919876543210"

    with patch("app.messaging.whatsapp_client.logger.info") as mock_log:
        asyncio.run(client.send_text_message(raw_phone, "Hello test message"))

        assert mock_log.called
        call_kwargs = mock_log.call_args.kwargs
        # The raw phone number must NEVER appear in logged kwargs
        assert call_kwargs.get("recipient_phone") != raw_phone
        assert call_kwargs.get("recipient_phone") == "+91******3210"


# ── 2. Fail-Fast Test Database Protection Tests ──────────────────────────────
def test_get_async_session_guards_missing_test_db(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_foo")
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="TEST_DATABASE_URL is required"):
        _get_async_session()


def test_get_async_session_guards_forbidden_db(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_foo")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://postgres:pass@localhost:5433/havn_crm")

    with pytest.raises(RuntimeError, match="must be 'havn_test'"):
        _get_async_session()


def test_get_async_session_succeeds_with_havn_test(monkeypatch):
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "test_foo")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://postgres:pass@localhost:5433/havn_test")

    sessionmaker_inst = _get_async_session()
    assert sessionmaker_inst is not None


# ── 3. Dead-Letter Inspection & Operator Recovery Tests ────────────────────────
@pytest.mark.asyncio
async def test_retry_dead_letter_not_in_dead_letter_status():
    session = AsyncMock()
    svc = OutboxService(session)

    msg = OutboxMessage(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        status="PENDING",
        event_type="WHATSAPP_OUTBOUND_DISPATCH",
        aggregate_type="LEAD",
        aggregate_id=str(uuid.uuid4()),
        payload={},
    )
    result_mock = MagicMock()
    result_mock.scalars.return_value.first.return_value = msg
    session.execute.return_value = result_mock

    with pytest.raises(ConflictError, match="Only DEAD_LETTER messages can be retried"):
        await svc.retry_dead_letter(msg.id, msg.tenant_id)


@pytest.mark.asyncio
async def test_retry_dead_letter_already_delivered_guard():
    session = AsyncMock()
    svc = OutboxService(session)

    msg = OutboxMessage(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        status="DEAD_LETTER",
        event_type="WHATSAPP_OUTBOUND_DISPATCH",
        aggregate_type="LEAD",
        aggregate_id=str(uuid.uuid4()),
        payload={"provider_message_id": "wamid.12345"},
    )
    result_mock = MagicMock()
    result_mock.scalars.return_value.first.return_value = msg
    session.execute.return_value = result_mock

    with pytest.raises(ConflictError, match="already confirmed delivery"):
        await svc.retry_dead_letter(msg.id, msg.tenant_id)


@pytest.mark.asyncio
async def test_retry_dead_letter_resets_pending_and_records_operator():
    session = AsyncMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    svc = OutboxService(session)

    msg = OutboxMessage(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        status="DEAD_LETTER",
        event_type="AI_QUALIFICATION_REQUESTED",
        aggregate_type="LEAD",
        aggregate_id=str(uuid.uuid4()),
        payload={"_claim_token": "stale-claim"},
        retry_count=3,
    )
    result_mock = MagicMock()
    # First query returns msg, second query for accepted proposal returns None
    result_mock.scalars.return_value.first.side_effect = [msg, None]
    session.execute.return_value = result_mock

    operator_id = uuid.uuid4()
    updated = await svc.retry_dead_letter(msg.id, msg.tenant_id, operator_id=operator_id)

    assert updated.status == "PENDING"
    assert updated.retry_count == 0
    assert updated.payload.get("_operator_retried_by") == str(operator_id)
    assert "_claim_token" not in updated.payload


# ── 4. Meta Lead Ads Webhook Service Tests ────────────────────────────────────
def test_meta_webhook_verify_subscription():
    session = AsyncMock()
    svc = MetaLeadAdsWebhookService(session)

    with patch("app.webhooks.meta_service.settings") as s:
        s.meta_verify_token = "valid_verify_token"
        s.whatsapp_verify_token = "valid_verify_token"
        # Success
        assert svc.verify_subscription("subscribe", "valid_verify_token", "challenge_123") == "challenge_123"

        # Invalid token
        with pytest.raises(AuthenticationError):
            svc.verify_subscription("subscribe", "wrong_token", "challenge_123")


def test_meta_webhook_verify_signature():
    session = AsyncMock()
    svc = MetaLeadAdsWebhookService(session)
    secret = "test_meta_app_secret_123"
    body = b'{"object": "page"}'

    with patch("app.webhooks.meta_service.settings") as s:
        s.meta_app_secret = secret
        s.whatsapp_app_secret = secret

        # Valid signature
        valid_hash = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        valid_header = f"sha256={valid_hash}"
        assert svc.verify_signature(body, valid_header) is True

        # Invalid signature
        assert svc.verify_signature(body, "sha256=invalidhash") is False
        assert svc.verify_signature(body, None) is False
