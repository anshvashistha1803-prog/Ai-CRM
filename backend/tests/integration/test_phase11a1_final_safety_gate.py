"""
Integration and security test suite for Phase 11A.1: Final Sandbox Safety Gate.

Verifies:
1. Consent Governance:
   - Explicit permission grants WhatsApp communication consent.
   - Denied permission sets consent_granted to False.
   - Absent permission defaults strictly to False (never inferred from phone number or form submission).
   - Generic terms acceptance (agree_to_terms) without communication/business scope is rejected.
   - Ambiguous consent wording ('Contact me') without business/channel identification is rejected.
   - Tenant-specific contact permission mismatch (naming another business) is rejected.
   - Ineligible leads are saved in CRM with automation_status=PAUSED and can be scored/qualified internally without automated contact.
   - ConsentRecord preserves exact wording, consent_type, channel, and evidence metadata.

2. Ambiguous Delivery Recovery Protection:
   - HTTP 4xx / 429 classified as confirmed provider rejections.
   - HTTP 502/504 gateway errors classified as ambiguous deliveries.
   - HTTP 500 with structured Meta error JSON classified as confirmed failure.
   - HTTP 500 without structured Meta error JSON classified as ambiguous delivery.
   - Blind retries of ambiguous deliveries are strictly rejected (raises ConflictError / HTTP 409).
   - Ambiguous delivery retry succeeds when verified reconciliation outcome (CONFIRMED_UNDELIVERED) is provided.
   - Ambiguous delivery retry succeeds when explicit duplicate-risk override with valid justification is provided.
   - Dedicated reconciliation workflow (CONFIRMED_DELIVERED vs CONFIRMED_UNDELIVERED).

3. Meta Webhook Signature Architecture:
   - HMAC-SHA256 signature verification over raw request body before parsing JSON.
   - Rejection on missing signature (401) and invalid signature (403).
   - Rejection on altered request body bytes (403).
   - Tenant resolution strictly via verified page_id; caller-supplied tenant identifiers ignored.
   - Unknown page_id is rejected/skipped without ingesting into any tenant.

4. Celery Queue Alignment:
   - Offline verification that active workers in docker-compose.yml consume all task and beat queues.
"""

import hashlib
import hmac
import json
import uuid
from unittest.mock import patch

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import ConflictError
from app.leads.models import ConsentRecord
from app.leads.schemas import LeadCreateRequest
from app.leads.service import LeadService
from app.messaging.outbox_service import OutboxService
from app.messaging.whatsapp_client import (
    WhatsAppAmbiguousDeliveryError,
    WhatsAppConfirmedFailureError,
    classify_provider_error,
)
from app.tenants.models import Tenant
from app.webhooks import meta_service
from app.webhooks.meta_client import normalize_meta_lead_payload

settings = get_settings()


# ═════════════════════════════════════════════════════════════════════════════
# TASK 1: CONSENT MAPPING & GOVERNANCE TESTS
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_consent_mapping_explicit_permission():
    """Explicit permission via whatsapp_consent or custom disclaimer authorizes communication."""
    # 1. Direct explicit field
    raw_1 = {
        "id": "leadgen_explicit_1",
        "field_data": [
            {"name": "full_name", "values": ["Kavita Verma"]},
            {"name": "phone_number", "values": ["+91 98765 43210"]},
            {"name": "whatsapp_consent", "values": ["yes"]},
        ],
    }
    norm_1 = normalize_meta_lead_payload(raw_1, expected_business_name="HAVN Realty")
    assert norm_1["consent_granted"] is True
    assert norm_1["consent_type"] == "WHATSAPP_COMMUNICATION"
    assert norm_1["consent_evidence"]["consent_granted"] is True
    assert norm_1["consent_evidence"]["field_name"] == "whatsapp_consent"

    # 2. Custom disclaimer wording authorizing HAVN
    raw_2 = {
        "id": "leadgen_explicit_2",
        "field_data": [
            {"name": "full_name", "values": ["Rajesh Khanna"]},
            {"name": "phone_number", "values": ["+91 98765 43211"]},
            {
                "name": "custom_disclaimer",
                "question": "I authorize HAVN Realty to contact me on WhatsApp regarding luxury properties.",
                "values": ["I agree"],
            },
        ],
    }
    norm_2 = normalize_meta_lead_payload(raw_2, expected_business_name="HAVN Realty")
    assert norm_2["consent_granted"] is True
    assert "HAVN Realty" in norm_2["consent_text"]


@pytest.mark.asyncio
async def test_consent_mapping_denied_permission():
    """Explicit negative response denies communication permission."""
    raw_denied = {
        "id": "leadgen_denied_1",
        "field_data": [
            {"name": "full_name", "values": ["Aman Gupta"]},
            {"name": "phone_number", "values": ["+91 98765 43212"]},
            {"name": "whatsapp_opt_in", "values": ["no"]},
        ],
    }
    norm = normalize_meta_lead_payload(raw_denied)
    assert norm["consent_granted"] is False
    assert norm["consent_type"] == "NONE"
    assert norm["consent_evidence"]["evaluation_reason"] == "explicitly_denied_by_user"


@pytest.mark.asyncio
async def test_consent_mapping_absent_permission_never_inferred():
    """Absent consent field strictly defaults to False and is never inferred from phone or submission."""
    raw_absent = {
        "id": "leadgen_absent_1",
        "field_data": [
            {"name": "full_name", "values": ["Priya Sharma"]},
            {"name": "phone_number", "values": ["+91 98765 43213"]},
            {"name": "email", "values": ["priya@example.com"]},
            {"name": "city", "values": ["Dehradun"]},
            {"name": "budget", "values": ["2 Cr"]},
        ],
    }
    norm = normalize_meta_lead_payload(raw_absent)
    assert norm["consent_granted"] is False
    assert norm["consent_evidence"]["evaluation_reason"] == "absent_consent_field"


@pytest.mark.asyncio
async def test_consent_mapping_generic_terms_acceptance_rejected():
    """Generic agree_to_terms or terms_and_conditions does not grant WhatsApp messaging consent."""
    raw_generic = {
        "id": "leadgen_generic_terms_1",
        "field_data": [
            {"name": "full_name", "values": ["Vikram Batra"]},
            {"name": "phone_number", "values": ["+91 98765 43214"]},
            {"name": "agree_to_terms", "values": ["yes"]},
            {"name": "terms_and_conditions", "values": ["I agree to website terms of service and privacy policy."]},
        ],
    }
    norm = normalize_meta_lead_payload(raw_generic, expected_business_name="HAVN Realty")
    assert norm["consent_granted"] is False
    assert norm["consent_evidence"]["evaluation_reason"] == "generic_terms_acceptance_without_explicit_whatsapp_scope"


@pytest.mark.asyncio
async def test_consent_mapping_ambiguous_wording_rejected():
    """Ambiguous wording ('Contact me') without business/channel identity is rejected."""
    raw_ambiguous = {
        "id": "leadgen_ambiguous_1",
        "field_data": [
            {"name": "full_name", "values": ["Sunil Chettri"]},
            {"name": "phone_number", "values": ["+91 98765 43215"]},
            {"name": "consent_to_contact", "question": "Contact me", "values": ["yes"]},
        ],
    }
    norm = normalize_meta_lead_payload(raw_ambiguous, expected_business_name="HAVN Realty")
    assert norm["consent_granted"] is False
    assert norm["consent_evidence"]["evaluation_reason"] == "ambiguous_consent_wording_missing_channel_or_business_identity"


@pytest.mark.asyncio
async def test_consent_mapping_tenant_specific_permission_mismatch_rejected():
    """Consent explicitly granted to another business (Apex Properties) is rejected for HAVN Realty."""
    raw_other_biz = {
        "id": "leadgen_mismatch_1",
        "field_data": [
            {"name": "full_name", "values": ["Deepak Chahar"]},
            {"name": "phone_number", "values": ["+91 98765 43216"]},
            {
                "name": "custom_disclaimer",
                "question": "I authorize Apex Properties to contact me on WhatsApp.",
                "values": ["yes"],
            },
        ],
    }
    norm = normalize_meta_lead_payload(raw_other_biz, expected_business_name="HAVN Realty")
    assert norm["consent_granted"] is False
    assert norm["consent_evidence"]["evaluation_reason"] == "tenant_mismatch_different_business_named"


@pytest.mark.asyncio
async def test_ineligible_lead_persisted_with_automation_paused(
    db: AsyncSession,
    test_tenant: Tenant,
):
    """Ineligible leads must still be saved, visible in CRM, and qualified internally with automation_status=PAUSED."""
    ctx = TenantContext(tenant_id=test_tenant.id, user_id=None, role=UserRole.SUPER_ADMIN)
    lead_svc = LeadService(db, ctx)

    # Lead without consent
    lead_req = LeadCreateRequest(
        full_name="Rohit Sharma",
        phone="+919876543217",
        email="rohit.sharma@example.com",
        source="META_LEAD_ADS",
        preferred_contact="WHATSAPP",
        consent_granted=False,
        consent_type="NONE",
        consent_text="Generic terms only - no WhatsApp consent",
        raw_message_text="Interested in 4 BHK Penthouse",
        property_type="PENTHOUSE",
        bhk=4,
        budget_min=30_000_000,
        budget_max=50_000_000,
    )

    created_lead = await lead_svc.create_lead(lead_req)
    await db.flush()

    assert created_lead.id is not None
    assert created_lead.automation_status == "PAUSED"
    assert created_lead.stage == "NEW"

    # Verify ConsentRecord was saved with granted=False
    c_stmt = select(ConsentRecord).where(
        ConsentRecord.tenant_id == test_tenant.id,
        ConsentRecord.lead_id == created_lead.id,
    )
    consent = (await db.execute(c_stmt)).scalars().first()
    assert consent is not None
    assert consent.granted is False
    assert consent.consent_type == "NONE"
    assert "Generic terms only" in consent.consent_text

    # Internal scoring rules can still qualify the lead internally
    from app.scoring.engine import ScoringEngine

    result = ScoringEngine.evaluate(created_lead)
    assert result is not None
    assert result.intent_score > 0  # Lead is scoreable and qualifiable internally!


# ═════════════════════════════════════════════════════════════════════════════
# TASK 2: AMBIGUOUS WHATSAPP DELIVERY RECOVERY TESTS
# ═════════════════════════════════════════════════════════════════════════════


def test_provider_error_classification_semantics():
    """Verify classification of HTTP status codes, timeouts, and structured Meta JSON against semantics."""
    # 1. HTTP 400 Bad Request -> Confirmed Failure
    req = httpx.Request("POST", "https://graph.facebook.com/messages")
    resp_400 = httpx.Response(400, request=req, text='{"error": {"code": 100, "message": "Invalid parameter"}}')
    err_400 = httpx.HTTPStatusError("400 Bad Request", request=req, response=resp_400)
    classified_400 = classify_provider_error(err_400)
    assert isinstance(classified_400, WhatsAppConfirmedFailureError)
    assert classified_400.is_ambiguous is False

    # 2. HTTP 429 Rate Limit -> Confirmed Failure
    resp_429 = httpx.Response(429, request=req, text='{"error": {"code": 80007, "message": "Rate limit hit"}}')
    err_429 = httpx.HTTPStatusError("429 Rate Limited", request=req, response=resp_429)
    classified_429 = classify_provider_error(err_429)
    assert isinstance(classified_429, WhatsAppConfirmedFailureError)
    assert classified_429.is_ambiguous is False

    # 3. HTTP 502 Bad Gateway -> Ambiguous Delivery
    resp_502 = httpx.Response(502, request=req, text="Bad Gateway from Cloudflare")
    err_502 = httpx.HTTPStatusError("502 Bad Gateway", request=req, response=resp_502)
    classified_502 = classify_provider_error(err_502)
    assert isinstance(classified_502, WhatsAppAmbiguousDeliveryError)
    assert classified_502.is_ambiguous is True

    # 4. HTTP 504 Gateway Timeout -> Ambiguous Delivery
    resp_504 = httpx.Response(504, request=req, text="Gateway Timeout")
    err_504 = httpx.HTTPStatusError("504 Gateway Timeout", request=req, response=resp_504)
    classified_504 = classify_provider_error(err_504)
    assert isinstance(classified_504, WhatsAppAmbiguousDeliveryError)
    assert classified_504.is_ambiguous is True

    # 5. HTTP 500 with structured Meta error JSON -> Confirmed Failure
    resp_500_meta = httpx.Response(
        500,
        request=req,
        text='{"error": {"code": 131031, "message": "All template instances blocked", "type": "OAuthException"}}',
    )
    err_500_meta = httpx.HTTPStatusError("500 Meta Error", request=req, response=resp_500_meta)
    classified_500_meta = classify_provider_error(err_500_meta)
    assert isinstance(classified_500_meta, WhatsAppConfirmedFailureError)
    assert classified_500_meta.is_ambiguous is False

    # 6. HTTP 500 unstructured / HTML crash -> Ambiguous Delivery
    resp_500_unstructured = httpx.Response(500, request=req, text="<html><body>Internal Server Error</body></html>")
    err_500_unstr = httpx.HTTPStatusError("500 Unstructured", request=req, response=resp_500_unstructured)
    classified_500_unstr = classify_provider_error(err_500_unstr)
    assert isinstance(classified_500_unstr, WhatsAppAmbiguousDeliveryError)
    assert classified_500_unstr.is_ambiguous is True


@pytest.mark.asyncio
async def test_ambiguous_dead_letter_blind_retry_rejected(
    db: AsyncSession,
    test_tenant: Tenant,
):
    """An ambiguous delivery outbox message cannot be blindly resent without reconciliation or override."""
    svc = OutboxService(db)
    msg = await svc.enqueue_message(
        tenant_id=test_tenant.id,
        event_type="WHATSAPP_OUTBOUND_DISPATCH",
        aggregate_type="LEAD",
        aggregate_id=uuid.uuid4(),
        payload={"recipient_phone": "+919876543210", "message_text": "Hello"},
        idempotency_key=f"ambig-{uuid.uuid4()}",
    )
    await svc.mark_processing(msg.id)
    await svc.mark_failed(msg.id, "Socket timeout after write", is_ambiguous=True)
    await db.flush()

    # Attempting a blind retry must raise ConflictError
    with pytest.raises(ConflictError) as exc_info:
        await svc.retry_dead_letter(msg.id, test_tenant.id)
    assert "Cannot retry ambiguous delivery outbox message" in str(exc_info.value)
    assert "without verified reconciliation" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ambiguous_dead_letter_retry_with_reconciliation_outcome(
    db: AsyncSession,
    test_tenant: Tenant,
):
    """Providing verified reconciliation outcome CONFIRMED_UNDELIVERED allows safe retry."""
    svc = OutboxService(db)
    msg = await svc.enqueue_message(
        tenant_id=test_tenant.id,
        event_type="WHATSAPP_OUTBOUND_DISPATCH",
        aggregate_type="LEAD",
        aggregate_id=uuid.uuid4(),
        payload={"recipient_phone": "+919876543210", "message_text": "Hello"},
        idempotency_key=f"ambig-{uuid.uuid4()}",
    )
    await svc.mark_processing(msg.id)
    await svc.mark_failed(msg.id, "Gateway timeout 504", is_ambiguous=True)
    await db.flush()

    # Retry with verified reconciliation outcome
    retried = await svc.retry_dead_letter(
        msg.id,
        test_tenant.id,
        reconciliation_outcome="CONFIRMED_UNDELIVERED",
    )
    await db.flush()
    assert retried.status == "PENDING"
    assert retried.payload["reconciliation_status"] == "CONFIRMED_UNDELIVERED"


@pytest.mark.asyncio
async def test_ambiguous_dead_letter_retry_with_audited_override(
    db: AsyncSession,
    test_tenant: Tenant,
):
    """An audited duplicate-risk override with valid justification allows resending with clear warning."""
    svc = OutboxService(db)
    msg = await svc.enqueue_message(
        tenant_id=test_tenant.id,
        event_type="WHATSAPP_OUTBOUND_DISPATCH",
        aggregate_type="LEAD",
        aggregate_id=uuid.uuid4(),
        payload={"recipient_phone": "+919876543210", "message_text": "Hello"},
        idempotency_key=f"ambig-{uuid.uuid4()}",
    )
    await svc.mark_processing(msg.id)
    await svc.mark_failed(msg.id, "Socket connection dropped", is_ambiguous=True)
    await db.flush()

    # Retry with audited override
    override_reason = "Customer confirmed via phone call message was not received"
    retried = await svc.retry_dead_letter(
        msg.id,
        test_tenant.id,
        allow_duplicate_risk_override=True,
        override_reason=override_reason,
    )
    await db.flush()
    assert retried.status == "PENDING"
    assert retried.payload["duplicate_risk_override"] is True
    assert retried.payload["override_reason"] == override_reason
    assert "OPERATOR_ACKNOWLEDGED_DUPLICATE_MESSAGE_RISK" in retried.payload["duplicate_risk_warning"]


@pytest.mark.asyncio
async def test_reconcile_dead_letter_workflow_confirmed_delivered(
    db: AsyncSession,
    test_tenant: Tenant,
):
    """Reconciling as CONFIRMED_DELIVERED marks status=SENT and prevents duplicate message resend."""
    svc = OutboxService(db)
    msg = await svc.enqueue_message(
        tenant_id=test_tenant.id,
        event_type="WHATSAPP_OUTBOUND_DISPATCH",
        aggregate_type="LEAD",
        aggregate_id=uuid.uuid4(),
        payload={"recipient_phone": "+919876543210", "message_text": "Hello"},
        idempotency_key=f"ambig-{uuid.uuid4()}",
    )
    await svc.mark_processing(msg.id)
    await svc.mark_failed(msg.id, "Network drop awaiting ACK", is_ambiguous=True)
    await db.flush()

    # Operator reconciles message as already delivered
    reconciled = await svc.reconcile_dead_letter(
        message_id=msg.id,
        tenant_id=test_tenant.id,
        reconciliation_outcome="CONFIRMED_DELIVERED",
        provider_message_id="wamid.HBgLMTIzNDU2",
        notes="Customer responded to this exact message",
    )
    await db.flush()
    assert reconciled.status == "SENT"
    assert reconciled.payload["provider_message_id"] == "wamid.HBgLMTIzNDU2"

    # Attempting to retry now fails because it is already SENT / business action succeeded
    with pytest.raises(ConflictError):
        await svc.retry_dead_letter(msg.id, test_tenant.id)


@pytest.mark.asyncio
async def test_dead_letter_rest_api_recovery_protection(
    client: AsyncClient,
    super_admin_token: str,
    test_tenant: Tenant,
    db: AsyncSession,
):
    """REST endpoint /api/v1/operations/dead-letters/{id}/retry enforces ambiguous delivery safety gate."""
    svc = OutboxService(db)
    msg = await svc.enqueue_message(
        tenant_id=test_tenant.id,
        event_type="WHATSAPP_OUTBOUND_DISPATCH",
        aggregate_type="LEAD",
        aggregate_id=uuid.uuid4(),
        payload={"recipient_phone": "+919876543210", "message_text": "Hello"},
        idempotency_key=f"ambig-{uuid.uuid4()}",
    )
    await svc.mark_processing(msg.id)
    await svc.mark_failed(msg.id, "Gateway 504 Timeout", is_ambiguous=True)
    await db.flush()

    headers = {"Authorization": f"Bearer {super_admin_token}"}

    # 1. Blind retry without body -> 409 Conflict
    res_blind = await client.post(
        f"/api/v1/operations/dead-letters/{msg.id}/retry",
        headers=headers,
    )
    assert res_blind.status_code == 409
    err_msg = res_blind.json().get("message") or res_blind.json().get("detail", "")
    assert "Cannot retry ambiguous delivery outbox message" in err_msg

    # 2. Retry with duplicate-risk override -> 200 OK
    res_override = await client.post(
        f"/api/v1/operations/dead-letters/{msg.id}/retry",
        json={
            "allow_duplicate_risk_override": True,
            "override_reason": "Verified manually via telecom carrier delivery report",
        },
        headers=headers,
    )
    assert res_override.status_code == 200
    assert res_override.json()["status"] == "PENDING"


# ═════════════════════════════════════════════════════════════════════════════
# TASK 3: META WEBHOOK SIGNATURE ARCHITECTURE TESTS
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_meta_webhook_missing_signature_rejected(client: AsyncClient):
    """Missing X-Hub-Signature-256 header returns HTTP 401."""
    resp = await client.post(
        "/api/v1/webhooks/meta-leadgen",
        json={"entry": []},
        headers={},
    )
    assert resp.status_code == 401
    assert "Missing X-Hub-Signature-256 header" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_meta_webhook_invalid_signature_rejected(client: AsyncClient):
    """Invalid signature returns HTTP 403."""
    body = json.dumps({"entry": []}).encode("utf-8")
    resp = await client.post(
        "/api/v1/webhooks/meta-leadgen",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=invalid_forged_signature_hash_value",
        },
    )
    assert resp.status_code == 403
    assert "Invalid HMAC-SHA256 signature" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_meta_webhook_altered_body_rejected(client: AsyncClient):
    """Tampered or altered body bytes invalidate HMAC signature and return HTTP 403."""
    secret = "test_meta_app_secret_12345"
    original_body = json.dumps({"entry": [{"id": "1"}]}).encode("utf-8")
    sig = "sha256=" + hmac.new(secret.encode("utf-8"), original_body, hashlib.sha256).hexdigest()

    tampered_body = json.dumps({"entry": [{"id": "1", "injected": "malicious"}]}).encode("utf-8")

    with patch.object(meta_service.settings, "meta_app_secret", secret):
        resp = await client.post(
            "/api/v1/webhooks/meta-leadgen",
            content=tampered_body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_meta_webhook_unknown_page_not_ingested(client: AsyncClient):
    """A validly signed payload for an unmapped page_id is skipped and not ingested into any tenant."""
    secret = "test_secret_for_unknown_page"
    unknown_page_id = "page_completely_unknown_9999"
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "field": "leadgen",
                        "value": {
                            "leadgen_id": "leadgen_unmapped_123",
                            "page_id": unknown_page_id,
                            "form_id": "form_123",
                        },
                    }
                ]
            }
        ]
    }
    body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    with patch.object(meta_service.settings, "meta_app_secret", secret):
        resp = await client.post(
            "/api/v1/webhooks/meta-leadgen",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["processed_events"] == 1
        assert data["details"][0]["action"] == "skipped_unmapped_tenant"


@pytest.mark.asyncio
async def test_meta_webhook_caller_supplied_tenant_ignored_page_mapping_enforced(
    client: AsyncClient,
    test_tenant: Tenant,
    db: AsyncSession,
):
    """Caller-supplied tenant_id query/body parameters are ignored; tenant is resolved strictly via page_id."""
    mapped_page_id = f"page_{test_tenant.id.hex[:10]}"
    test_tenant.settings = {"meta_page_id": mapped_page_id, "meta_access_token": "mock_token_123"}
    await db.flush()

    secret = "test_secret_tenant_isolation"
    spoofed_other_tenant_id = str(uuid.uuid4())

    mock_lead = {
        "id": "leadgen_spoof_test_1",
        "field_data": [
            {"name": "full_name", "values": ["Shubman Gill"]},
            {"name": "phone_number", "values": ["+91 98765 43218"]},
            {"name": "whatsapp_consent", "values": ["yes"]},
        ],
    }
    payload = {
        "tenant_id": spoofed_other_tenant_id,  # Injected by attacker
        "entry": [
            {
                "changes": [
                    {
                        "field": "leadgen",
                        "value": {
                            "leadgen_id": "leadgen_spoof_test_1",
                            "page_id": mapped_page_id,
                            "form_id": "form_987",
                            "mock_lead_data": mock_lead,
                        },
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    sig = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    with patch.object(meta_service.settings, "meta_app_secret", secret):
        resp = await client.post(
            f"/api/v1/webhooks/meta-leadgen?tenant_id={spoofed_other_tenant_id}",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
        assert resp.status_code == 200
        details = resp.json()["details"]
        assert details[0]["action"] == "created"
        # Crucial: Lead MUST belong to tenant, NOT the spoofed tenant!
        assert details[0]["tenant_id"] == str(test_tenant.id)


# ═════════════════════════════════════════════════════════════════════════════
# TASK 4: CELERY QUEUE CONFIGURATION & ALIGNMENT TEST
# ═════════════════════════════════════════════════════════════════════════════


def test_queue_configuration_and_worker_subscription_alignment():
    """Verify that all tasks and Beat schedules route to queues the active worker consumes."""
    import re
    from pathlib import Path

    from app.tasks.celery_app import celery_app
    from app.tasks.celery_tasks import (
        detect_stalled_leads_batch,
        dispatch_outbox_message,
        process_outbox_batch,
        sync_all_active_google_sheets_inventory,
    )

    # 1. Parse docker-compose.yml
    compose_path = Path(__file__).resolve().parents[3] / "docker-compose.yml"
    assert compose_path.exists()
    content = compose_path.read_text(encoding="utf-8")

    match = re.search(r"-Q\s+([a-zA-Z0-9_,]+)", content)
    assert match is not None
    worker_queues = {q.strip() for q in match.group(1).split(",") if q.strip()}

    # All required queues must be actively consumed by workers
    required_queues = {"default", "messages", "qualification", "outbox", "conversation", "sync"}
    assert required_queues.issubset(worker_queues)

    # 2. Verify all Celery Beat periodic schedules publish to worker queues
    for name, beat_cfg in celery_app.conf.beat_schedule.items():
        q = (
            beat_cfg.get("options", {}).get("queue")
            or celery_app.conf.task_routes.get(beat_cfg["task"], {}).get("queue")
            or celery_app.conf.task_default_queue
        )
        assert q in worker_queues, f"Beat task {name} routed to unconsumed queue {q}"

    # 3. Verify task routing
    for task in [process_outbox_batch, dispatch_outbox_message, detect_stalled_leads_batch, sync_all_active_google_sheets_inventory]:
        q = (
            task.queue
            or celery_app.conf.task_routes.get(task.name, {}).get("queue")
            or celery_app.conf.task_default_queue
        )
        assert q in worker_queues, f"Task {task.name} routed to unconsumed queue {q}"
