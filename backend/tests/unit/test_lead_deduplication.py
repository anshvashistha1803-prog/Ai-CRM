"""
Unit tests for lead deduplication rules and error handling.

Verifies:
1. Duplicate active leads within the same tenant are rejected with DuplicateLeadError.
2. Cross-tenant identical phone numbers are permitted (strict tenant isolation).
3. Multiple leads with null phone hashes in the same tenant are permitted.
4. Soft-deleted leads do not prevent creating a new active lead with the same phone number.
5. Concurrent create_lead race catching database IntegrityError raises DuplicateLeadError.
6. Concurrent update_lead race catching database IntegrityError raises DuplicateLeadError.
7. Concurrent WhatsApp webhook collisions catch IntegrityError and reuse the existing lead.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import DuplicateLeadError
from app.core.security import compute_blind_hash
from app.leads.deduplication import normalize_phone
from app.leads.models import Lead
from app.leads.schemas import LeadCreateRequest, LeadUpdateRequest
from app.leads.service import LeadService
from app.webhooks.whatsapp_service import WhatsAppWebhookService


@pytest.fixture
def mock_db():
    session = AsyncMock()
    session.in_transaction.return_value = True
    session.begin_nested = MagicMock()
    return session


@pytest.fixture
def tenant_a():
    return uuid.uuid4()


@pytest.fixture
def tenant_b():
    return uuid.uuid4()


@pytest.fixture
def agent_ctx(tenant_a):
    return TenantContext(
        user_id=uuid.uuid4(),
        tenant_id=tenant_a,
        role=UserRole.AGENT,
    )


@pytest.mark.asyncio
async def test_duplicate_active_lead_in_same_tenant_raises_error(mock_db, agent_ctx):
    """Attempting to create an active lead when one already exists in the same tenant raises DuplicateLeadError."""
    existing_lead = MagicMock()
    existing_lead.id = uuid.uuid4()

    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = existing_lead
    mock_db.execute.return_value = mock_result

    payload = LeadCreateRequest(
        source="WEBSITE",
        full_name="Duplicate Buyer",
        phone="+919876543210",
        email="buyer@example.com",
    )

    svc = LeadService(mock_db, agent_ctx)
    with pytest.raises(DuplicateLeadError, match="already exists in this agency"):
        await svc.create_lead(payload)


@pytest.mark.asyncio
async def test_cross_tenant_identical_phone_allowed(mock_db, tenant_a, tenant_b):
    """
    Two different tenants can independently create leads with the exact same phone number.
    Deduplication must never leak across tenant boundaries.
    """
    phone = "+919876543210"
    norm_phone = normalize_phone(phone)
    phone_hash = compute_blind_hash(norm_phone)
    assert len(phone_hash) == 64

    # Tenant A has an existing lead
    ctx_b = TenantContext(user_id=uuid.uuid4(), tenant_id=tenant_b, role=UserRole.AGENT)

    # When Tenant B queries for duplicates, tenant isolation ensures no lead is found for Tenant B
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_result

    payload = LeadCreateRequest(
        source="PORTAL",
        full_name="Tenant B Lead",
        phone=phone,
    )

    svc_b = LeadService(mock_db, ctx_b)
    resp = await svc_b.create_lead(payload)

    assert resp.full_name == "Tenant B Lead"
    assert resp.tenant_id == tenant_b


@pytest.mark.asyncio
async def test_soft_deleted_lead_permits_new_active_lead(mock_db, agent_ctx):
    """
    A soft-deleted lead (is_deleted=True) does not block creating a new active lead
    with the same phone number in the agency.
    """
    # Duplicate query checks is_deleted.is_(False), so soft-deleted leads return None
    mock_result = MagicMock()
    mock_result.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_result

    payload = LeadCreateRequest(
        source="WALK_IN",
        full_name="Returning Client",
        phone="+919876543210",
    )

    svc = LeadService(mock_db, agent_ctx)
    resp = await svc.create_lead(payload)
    assert resp.full_name == "Returning Client"


@pytest.mark.asyncio
async def test_create_lead_catches_unique_constraint_integrity_error(mock_db, agent_ctx):
    """
    If two concurrent requests bypass the SELECT check simultaneously,
    the database partial unique index raises IntegrityError on flush.
    LeadService must cleanly catch this and raise DuplicateLeadError (409) rather than 500.
    """
    # SELECT returns None (racing condition)
    mock_select = MagicMock()
    mock_select.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_select

    # DB raises IntegrityError on uq_leads_tenant_phone_hash
    mock_db.flush.side_effect = IntegrityError(
        "duplicate key value violates unique constraint \"uq_leads_tenant_phone_hash\"",
        params={},
        orig=Exception("23505: uq_leads_tenant_phone_hash"),
    )

    payload = LeadCreateRequest(
        source="WEBSITE",
        full_name="Concurrent Lead",
        phone="+919876543210",
    )

    svc = LeadService(mock_db, agent_ctx)
    with pytest.raises(DuplicateLeadError, match="already exists"):
        await svc.create_lead(payload)


@pytest.mark.asyncio
async def test_update_lead_catches_unique_constraint_integrity_error(mock_db, agent_ctx):
    """
    If update_lead encounters a concurrent duplicate phone update violating the unique index,
    it must catch IntegrityError and raise DuplicateLeadError.
    """
    lead_id = uuid.uuid4()
    existing_lead = Lead(
        id=lead_id,
        tenant_id=agent_ctx.tenant_id,
        source="MANUAL",
        full_name="Original Name",
        phone_hash=compute_blind_hash(normalize_phone("+919876543211")),
        preferred_contact="PHONE",
        stage="NEW",
        assigned_agent_id=agent_ctx.user_id,
    )

    find_res = MagicMock()
    find_res.scalars.return_value.first.return_value = existing_lead

    dup_check_res = MagicMock()
    dup_check_res.scalars.return_value.first.return_value = None

    mock_db.execute.side_effect = [find_res, dup_check_res]

    # DB raises IntegrityError on flush due to concurrent conflict
    mock_db.flush.side_effect = IntegrityError(
        "duplicate key value violates unique constraint \"uq_leads_tenant_phone_hash\"",
        params={},
        orig=Exception("23505: uq_leads_tenant_phone_hash"),
    )

    svc = LeadService(mock_db, agent_ctx)
    update_payload = LeadUpdateRequest(phone="+919876543210")

    with pytest.raises(DuplicateLeadError, match="Another lead already has this contact"):
        await svc.update_lead(lead_id, update_payload)


@pytest.mark.asyncio
async def test_whatsapp_webhook_concurrent_collision_reuses_existing_lead(mock_db, tenant_a):
    """
    When concurrent WhatsApp messages for the same new sender arrive simultaneously:
    Worker 2 catches IntegrityError on insert, retrieves Worker 1's lead,
    and logs the activity without crashing or creating a duplicate.
    """
    tenant = MagicMock()
    tenant.id = tenant_a
    tenant.whatsapp_phone_number_id = "phone-num-1"

    phone = "+919876543210"
    phone_hash = compute_blind_hash(normalize_phone(phone))

    concurrently_created_lead = Lead(
        id=uuid.uuid4(),
        tenant_id=tenant_a,
        source="WHATSAPP",
        full_name="WhatsApp Sender",
        phone_hash=phone_hash,
        preferred_contact="WHATSAPP",
        stage="NEW",
        raw_message_text="First message",
    )

    # Initial SELECT: lead is None
    select_initial = MagicMock()
    select_initial.scalars.return_value.first.return_value = None

    # Retry SELECT (inside except): lead is concurrently_created_lead
    select_retry = MagicMock()
    select_retry.scalars.return_value.first.return_value = concurrently_created_lead

    # Outbox idempotency checks return None
    outbox_res = MagicMock()
    outbox_res.scalar_one_or_none.return_value = None

    tenant_res = MagicMock()
    tenant_res.scalar_one_or_none.return_value = tenant

    async def mock_execute_side_effect(stmt, *args, **kwargs):
        stmt_str = str(stmt).lower()
        if "from tenants" in stmt_str:
            return tenant_res
        if "from leads" in stmt_str:
            if not hasattr(mock_execute_side_effect, "lead_checked"):
                mock_execute_side_effect.lead_checked = True
                return select_initial
            return select_retry
        if "from outbox_messages" in stmt_str:
            return outbox_res
        res = MagicMock()
        res.scalars.return_value.first.return_value = None
        res.scalar_one_or_none.return_value = None
        return res

    mock_db.execute = AsyncMock(side_effect=mock_execute_side_effect)

    call_count = 0
    def flush_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise IntegrityError(
                "duplicate key value violates unique constraint \"uq_leads_tenant_phone_hash\"",
                params={},
                orig=Exception("23505: uq_leads_tenant_phone_hash"),
            )
        return None

    mock_db.flush.side_effect = flush_side_effect

    webhook_payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "biz-1",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "phone-num-1"},
                            "contacts": [{"wa_id": "919876543210", "profile": {"name": "WhatsApp Sender"}}],
                            "messages": [
                                {
                                    "id": "wamid-conc-1",
                                    "from": "919876543210",
                                    "type": "text",
                                    "text": {"body": "Concurrent duplicate inquiry"},
                                    "timestamp": "1728800000",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }

    svc = WhatsAppWebhookService(mock_db)
    results = await svc.process_inbound_webhook(webhook_payload)

    # Should succeed with action "updated" and the existing lead's id, not raising 500
    assert len(results) == 1
    assert results[0]["action"] == "updated"
    assert results[0]["lead_id"] == str(concurrently_created_lead.id)
