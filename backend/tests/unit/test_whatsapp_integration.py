"""
Unit tests for WhatsApp Business Platform Cloud API integration.
Verifies webhook verification, HMAC signature validation, inbound message parsing,
tenant-safe lead deduplication, consent opt-out enforcement, outbox queuing, and mock client delivery.
"""

import hmac
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError
from app.leads.models import Lead
from app.leads.state_machine import LeadStage
from app.messaging.whatsapp_client import WhatsAppClient
from app.tenants.models import Tenant
from app.webhooks.whatsapp_service import WhatsAppWebhookService

settings = get_settings()


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


def _create_mock_tenant(tenant_id: uuid.UUID) -> Tenant:
    return Tenant(
        id=tenant_id,
        name="Bangalore Prime Realty",
        slug="blr-prime",
        timezone="Asia/Kolkata",
        currency_code="INR",
        is_active=True,
        whatsapp_phone_number_id="10987654321",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        is_deleted=False,
    )


def test_verify_subscription_success(mock_db):
    svc = WhatsAppWebhookService(mock_db)
    challenge = svc.verify_subscription(
        mode="subscribe",
        token="test_token_123",
        challenge="challenge_xyz_789",
        tenant_verify_token="test_token_123",
    )
    assert challenge == "challenge_xyz_789"


def test_verify_subscription_invalid_token(mock_db):
    svc = WhatsAppWebhookService(mock_db)
    with pytest.raises(AuthenticationError, match="Invalid webhook verification token"):
        svc.verify_subscription(
            mode="subscribe",
            token="wrong_token",
            challenge="challenge_xyz_789",
            tenant_verify_token="test_token_123",
        )


def test_verify_signature_validation():
    secret = "my_meta_app_secret_123"
    payload = b'{"object": "whatsapp_business_account"}'
    import hashlib
    valid_sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

    mock_db = AsyncMock()
    svc = WhatsAppWebhookService(mock_db)

    # Monkeypatch settings app secret for test
    original_secret = settings.whatsapp_app_secret
    try:
        settings.whatsapp_app_secret = secret
        assert svc.verify_signature(payload, valid_sig) is True
        assert svc.verify_signature(payload, "sha256=invalidhex") is False
    finally:
        settings.whatsapp_app_secret = original_secret


@pytest.mark.asyncio
async def test_inbound_message_creates_lead_and_enqueues_outbox(mock_db):
    tenant_id = uuid.uuid4()
    tenant = _create_mock_tenant(tenant_id)

    # First query resolves tenant, second checks if lead exists
    tenant_res = MagicMock()
    tenant_res.scalars.return_value.first.return_value = tenant

    lead_check_res = MagicMock()
    lead_check_res.scalars.return_value.first.return_value = None

    outbox_check_res = MagicMock()
    outbox_check_res.scalars.return_value.first.return_value = None

    mock_db.execute.side_effect = [tenant_res, lead_check_res, outbox_check_res]

    svc = WhatsAppWebhookService(mock_db)

    webhook_payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550254589",
                                "phone_number_id": "10987654321",
                            },
                            "contacts": [{"profile": {"name": "Arjun Rao"}, "wa_id": "919876543210"}],
                            "messages": [
                                {
                                    "from": "919876543210",
                                    "id": "wamid.HBgMOTE5ODc2NTQzMjEwFQIAEhgWM0VCMDEyMzQ1Njc4OUFCQ0RFRjAxMgA=",
                                    "timestamp": "1710000000",
                                    "type": "text",
                                    "text": {"body": "Hi, I am looking for a 3BHK flat in Indiranagar."},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }

    events = await svc.process_inbound_webhook(webhook_payload)

    assert len(events) == 1
    assert events[0]["action"] == "created"
    assert mock_db.add.call_count >= 3  # Lead, ConsentRecord, LeadActivity, OutboxMessage
    mock_db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_inbound_message_opt_out_enforcement(mock_db):
    tenant_id = uuid.uuid4()
    tenant = _create_mock_tenant(tenant_id)

    existing_lead = Lead(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        full_name="Arjun Rao",
        stage=LeadStage.QUALIFYING.value,
        phone_hash="hash123",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        is_deleted=False,
    )

    tenant_res = MagicMock()
    tenant_res.scalars.return_value.first.return_value = tenant

    lead_res = MagicMock()
    lead_res.scalars.return_value.first.return_value = existing_lead

    mock_db.execute.side_effect = [tenant_res, lead_res]

    svc = WhatsAppWebhookService(mock_db)

    stop_payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": "10987654321"},
                            "messages": [
                                {
                                    "from": "919876543210",
                                    "id": "wamid.stop123",
                                    "timestamp": "1710000000",
                                    "type": "text",
                                    "text": {"body": "STOP"},
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }

    events = await svc.process_inbound_webhook(stop_payload)

    assert len(events) == 1
    assert events[0]["action"] == "opt_out"
    assert existing_lead.do_not_contact is True
    assert existing_lead.stage == LeadStage.DO_NOT_CONTACT.value
    assert existing_lead.automation_status == "OPTED_OUT"


@pytest.mark.asyncio
async def test_whatsapp_client_mock_dispatch():
    client = WhatsAppClient(force_mock=True)
    assert client.is_mock is True

    # Free-form message
    resp = await client.send_text_message(
        recipient_phone="+919876543210",
        text="Hello, thank you for contacting us!",
    )
    assert resp["_is_mock"] is True
    assert "messages" in resp
    assert resp["messages"][0]["id"].startswith("wamid.mock.")

    # Template message
    tpl_resp = await client.send_template_message(
        recipient_phone="+919876543210",
        template_name="lead_welcome_v1",
    )
    assert tpl_resp["_is_mock"] is True
    assert tpl_resp["messages"][0]["id"].startswith("wamid.mock.template.")
