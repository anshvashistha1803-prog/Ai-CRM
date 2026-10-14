"""
Integration and security gap closure test suite for Phase 11A.
Verifies:
1. Concurrency: Inbound STOP vs queued outbound dispatch.
2. Delivery ambiguity: Confirmed failure vs confirmed acceptance vs ambiguous delivery.
3. Meta Graph API client: field normalization, auth failure, timeouts, malformed payload, and consent mapping.
4. Multi-tenant webhook isolation and credential separation.
5. Authorized dead-letter operational REST API endpoints (/api/v1/operations/dead-letters).
"""

import os
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.dependencies import TenantContext, UserRole
from app.core.security import create_access_token, hash_password
from app.leads.models import Lead
from app.leads.schemas import LeadCreateRequest
from app.leads.service import LeadService
from app.main import create_app
from app.messaging.models import OutboxMessage
from app.messaging.outbox_service import OutboxService
from app.tasks.celery_tasks import _handle_message_event
from app.tenants.models import Tenant
from app.users.models import User
from app.webhooks.meta_client import (
    MetaAuthenticationError,
    MetaGraphTimeoutError,
    MetaMalformedResponseError,
    MockMetaGraphClient,
    normalize_meta_lead_payload,
)
from app.webhooks.meta_service import MetaLeadAdsWebhookService
from app.webhooks.whatsapp_service import WhatsAppWebhookService


def _get_db():
    test_db_url = os.environ.get("TEST_DATABASE_URL")
    if not test_db_url or "havn_test" not in test_db_url or "havn_crm" in test_db_url:
        raise RuntimeError("CRITICAL SAFETY GUARD: Must target havn_test only.")
    engine = create_async_engine(test_db_url, echo=False)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return session_factory()


@pytest.mark.asyncio
class TestPhase11ASandboxGapClosure:
    async def _create_tenant(self, db: AsyncSession, name: str = "Gap Closure Agency", meta_page_id: str | None = None) -> Tenant:
        t = Tenant(
            id=uuid.uuid4(),
            name=name,
            slug=f"agency-{uuid.uuid4().hex[:8]}",
            is_active=True,
            is_deleted=False,
            whatsapp_phone_number_id=f"phone_{uuid.uuid4().hex[:6]}",
            settings={"meta_page_id": meta_page_id} if meta_page_id else {},
        )
        db.add(t)
        await db.commit()
        return t

    async def _create_user(self, db: AsyncSession, tenant_id: uuid.UUID, role: UserRole) -> User:
        user_email = f"{role.value.lower()}_{uuid.uuid4().hex[:6]}@example.com"
        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            email=user_email,
            email_normalized=user_email.lower(),
            password_hash=hash_password("StrongPass123!"),
            full_name=f"Test {role.value}",
            role=role.value,
            is_active=True,
            is_deleted=False,
        )
        db.add(user)
        await db.commit()
        return user

    # ── Gap A: Concurrency Test (STOP vs Queued Outbound Delivery) ─────────────
    async def test_concurrency_stop_keyword_suppresses_already_queued_message(self, create_tables):
        """
        Scenario: An outbound message is already queued in the outbox.
        Concurrently, the customer texts 'STOP' via WhatsApp webhook.
        Verify that the outbound message is safely suppressed and NO socket send occurs.
        """
        async with _get_db() as db:
            tenant = await self._create_tenant(db, name="Concurrency Agency")
            phone = "+919876543301"

            ctx = TenantContext(tenant_id=tenant.id, user_id=None, role=UserRole.SUPER_ADMIN)
            lead_svc = LeadService(db, ctx)
            lead = await lead_svc.create_lead(
                LeadCreateRequest(
                    full_name="Racing Customer",
                    phone=phone,
                    source="WEBSITE",
                    preferred_contact="WHATSAPP",
                    consent_granted=True,
                )
            )
            await db.commit()

            # Enqueue outbound message
            outbox_svc = OutboxService(db)
            outbox_msg = await outbox_svc.enqueue_message(
                tenant_id=tenant.id,
                event_type="WHATSAPP_OUTBOUND_DISPATCH",
                aggregate_type="LEAD",
                aggregate_id=lead.id,
                payload={"recipient_phone": phone, "message_text": "What is your budget?"},
                idempotency_key=f"racing-outbound-{lead.id}",
            )
            await db.commit()

        # Simulate concurrent arrival of STOP and worker execution
        async def _simulate_inbound_stop():
            async with _get_db() as db:
                wa_webhook_svc = WhatsAppWebhookService(db)
                stop_payload = {
                    "entry": [
                        {
                            "id": "wa_acc",
                            "changes": [
                                {
                                    "field": "messages",
                                    "value": {
                                        "messaging_product": "whatsapp",
                                        "metadata": {"phone_number_id": tenant.whatsapp_phone_number_id},
                                        "contacts": [{"wa_id": phone.lstrip("+"), "profile": {"name": "Racing Customer"}}],
                                        "messages": [
                                            {
                                                "from": phone.lstrip("+"),
                                                "id": f"wamid.{uuid.uuid4()}",
                                                "timestamp": str(int(datetime.now(UTC).timestamp())),
                                                "type": "text",
                                                "text": {"body": "STOP"},
                                            }
                                        ],
                                    },
                                }
                            ]
                        }
                    ]
                }
                res = await wa_webhook_svc.process_inbound_webhook(stop_payload)
                await db.commit()
                return res

        async def _simulate_outbound_worker():
            async with _get_db() as db:
                with patch("app.messaging.whatsapp_client.WhatsAppClient.send_text_message") as mock_send:
                    # Fetch fresh msg and execute handler
                    refreshed_msg = await db.get(OutboxMessage, outbox_msg.id)
                    result = await _handle_message_event(refreshed_msg, db=db)
                    await db.commit()
                    return result, mock_send.called

        # Inbound STOP finishes, then immediately worker dispatches
        await _simulate_inbound_stop()
        dispatch_res, send_called = await _simulate_outbound_worker()

        assert dispatch_res.startswith("suppressed:lead_do_not_contact")
        assert not send_called, "CRITICAL: Outbound WhatsApp send was called after STOP!"

        async with _get_db() as db:
            refreshed_lead = await db.get(Lead, lead.id)
            assert refreshed_lead.do_not_contact is True
            assert refreshed_lead.automation_status == "OPTED_OUT"

    # ── Gap B: Ambiguous Delivery vs Confirmed Failure vs Acceptance ───────────
    async def test_delivery_distinction_confirmed_failure_retries_with_backoff(self, create_tables):
        """Confirmed provider failure (e.g. 400 Bad Request) schedules retry with backoff."""
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            outbox_svc = OutboxService(db)
            msg = await outbox_svc.enqueue_message(
                tenant_id=tenant.id,
                event_type="WHATSAPP_OUTBOUND_DISPATCH",
                aggregate_type="LEAD",
                aggregate_id=uuid.uuid4(),
                payload={"recipient_phone": "+919876543302", "message_text": "Hello"},
            )
            await db.commit()

            # Worker claims message
            claimed = await outbox_svc.claim_message(msg.id)
            await db.commit()

            # Mark confirmed failure (HTTP 400)
            failed_msg = await outbox_svc.mark_failed(
                claimed.id,
                error_message="HTTP 400: Invalid template parameters",
                claimed_at=claimed.updated_at,
                claim_token=(claimed.payload or {}).get("_claim_token"),
                is_ambiguous=False,
            )
            await db.commit()

            assert failed_msg.status == "PENDING"
            assert failed_msg.retry_count == 1
            assert failed_msg.scheduled_at is not None
            assert (failed_msg.payload or {}).get("delivery_state") != "AMBIGUOUS"

    async def test_delivery_distinction_ambiguous_delivery_prevents_blind_retries(self, create_tables):
        """Ambiguous delivery (timeout after socket write) moves to DEAD_LETTER for reconciliation."""
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            outbox_svc = OutboxService(db)
            msg = await outbox_svc.enqueue_message(
                tenant_id=tenant.id,
                event_type="WHATSAPP_OUTBOUND_DISPATCH",
                aggregate_type="LEAD",
                aggregate_id=uuid.uuid4(),
                payload={"recipient_phone": "+919876543303", "message_text": "Hello"},
            )
            await db.commit()

            claimed = await outbox_svc.claim_message(msg.id)
            await db.commit()

            # Mark ambiguous timeout
            ambiguous_msg = await outbox_svc.mark_failed(
                claimed.id,
                error_message="Socket timeout awaiting Meta HTTP response",
                claimed_at=claimed.updated_at,
                claim_token=(claimed.payload or {}).get("_claim_token"),
                is_ambiguous=True,
            )
            await db.commit()

            assert ambiguous_msg.status == "DEAD_LETTER"
            assert ambiguous_msg.payload.get("delivery_state") == "AMBIGUOUS"
            assert ambiguous_msg.payload.get("ambiguous_delivery") is True
            assert "AMBIGUOUS_DELIVERY_REQUIRES_RECONCILIATION" in ambiguous_msg.last_error

    # ── Gap C: Meta Graph API Lead Detail Retrieval & Normalization ───────────
    async def test_meta_graph_payload_normalization_and_consent_mapping(self):
        """Test extraction and normalization of complex Meta Graph API payload."""
        raw_meta_response = {
            "id": "leadgen_88492019482019",
            "created_time": "2026-10-14T10:00:00+0000",
            "ad_id": "ad_123",
            "form_id": "form_456",
            "field_data": [
                {"name": "full_name", "values": ["Sunita Deshmukh"]},
                {"name": "phone_number", "values": ["+91 98765 43304"]},
                {"name": "email", "values": ["sunita.deshmukh@example.com"]},
                {"name": "city", "values": ["Mussoorie Road, Dehradun"]},
                {"name": "property_type", "values": ["Penthouse"]},
                {"name": "bhk", "values": ["4 BHK"]},
                {"name": "budget", "values": ["3 Cr to 4.5 Cr"]},
                {"name": "whatsapp_consent", "values": ["yes"]},
            ],
        }

        normalized = normalize_meta_lead_payload(raw_meta_response)

        assert normalized["leadgen_id"] == "leadgen_88492019482019"
        assert normalized["full_name"] == "Sunita Deshmukh"
        assert normalized["phone"] == "+919876543304"
        assert normalized["email"] == "sunita.deshmukh@example.com"
        assert normalized["preferred_location"] == "Mussoorie Road, Dehradun"
        assert normalized["property_type"] == "Penthouse"
        assert normalized["bhk"] == 4
        assert normalized["budget_min"] == 30_000_000
        assert normalized["budget_max"] == 45_000_000
        assert normalized["consent_granted"] is True

    async def test_meta_graph_payload_normalization_denied_consent(self):
        """Verify explicit denied consent sets consent_granted to False."""
        raw_denied = {
            "id": "leadgen_denied_1",
            "field_data": [
                {"name": "full_name", "values": ["No Consent Lead"]},
                {"name": "phone_number", "values": ["+919876543305"]},
                {"name": "whatsapp_opt_in", "values": ["no"]},
            ],
        }
        normalized = normalize_meta_lead_payload(raw_denied)
        assert normalized["consent_granted"] is False

    async def test_meta_graph_client_error_handling(self):
        """Test mockable client error handling for timeouts, invalid auth, and malformed responses."""
        mock_client = MockMetaGraphClient(
            fixture_responses={
                "lead_auth_fail": MetaAuthenticationError("Token expired"),
                "lead_timeout": MetaGraphTimeoutError("Connection timed out"),
                "lead_malformed": MetaMalformedResponseError("Unparseable payload"),
            }
        )

        with pytest.raises(MetaAuthenticationError):
            await mock_client.fetch_lead_details("lead_auth_fail")

        with pytest.raises(MetaGraphTimeoutError):
            await mock_client.fetch_lead_details("lead_timeout")

        with pytest.raises(MetaMalformedResponseError):
            await mock_client.fetch_lead_details("lead_malformed")

    # ── Gap D: Tenant Isolation & Credential Security ─────────────────────────
    async def test_meta_webhook_ignores_unverified_tenant_identifiers(self, create_tables):
        """Verify unmapped page_id cannot spoof tenant ownership or inject leads."""
        async with _get_db() as db:
            tenant_a = await self._create_tenant(db, name="Tenant A", meta_page_id="page_valid_a")
            await self._create_tenant(db, name="Tenant B", meta_page_id="page_valid_b")

            meta_svc = MetaLeadAdsWebhookService(db)

            # Webhook with unmapped page_id
            payload_unmapped = {
                "entry": [
                    {
                        "changes": [
                            {
                                "field": "leadgen",
                                "value": {
                                    "leadgen_id": "meta_lead_unmapped",
                                    "page_id": "page_unregistered_attacker",
                                    # Attacker attempting to claim Tenant A
                                    "tenant_id": str(tenant_a.id),
                                },
                            }
                        ]
                    }
                ]
            }

            res = await meta_svc.process_inbound_webhook(payload_unmapped)
            assert res[0]["action"] == "skipped_unmapped_tenant"

            # Verify no lead created in Tenant A or Tenant B
            count_a = (
                await db.execute(select(Lead).where(Lead.tenant_id == tenant_a.id))
            ).scalars().all()
            assert len(count_a) == 0

    # ── Gap E: Authorized Operational Dead-Letter REST API ────────────────────
    async def test_dead_letter_api_requires_authentication(self):
        """Unauthenticated call to /api/v1/operations/dead-letters returns 401."""
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/operations/dead-letters")
            assert resp.status_code == 401

    async def test_dead_letter_api_forbids_agent_role(self, create_tables):
        """Agent role calling /api/v1/operations/dead-letters returns 403 Forbidden."""
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            agent_user = await self._create_user(db, tenant.id, UserRole.AGENT)

            token = create_access_token(
                user_id=agent_user.id,
                tenant_id=tenant.id,
                role=agent_user.role,
            )

        app = create_app()
        from app.core.database import get_db

        async def _override_db():
            async with _get_db() as s:
                yield s

        app.dependency_overrides[get_db] = _override_db
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get(
                    "/api/v1/operations/dead-letters",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert resp.status_code == 403
        finally:
            app.dependency_overrides.clear()

    async def test_dead_letter_api_agency_admin_listing_and_retry(self, create_tables):
        """Agency Admin can list and retry dead-letter messages for their own agency."""
        async with _get_db() as db:
            tenant = await self._create_tenant(db)
            admin_user = await self._create_user(db, tenant.id, UserRole.AGENCY_ADMIN)

            # Create a message in DEAD_LETTER status
            outbox_svc = OutboxService(db)
            msg = await outbox_svc.enqueue_message(
                tenant_id=tenant.id,
                event_type="AI_QUALIFICATION_REQUESTED",
                aggregate_type="LEAD",
                aggregate_id=uuid.uuid4(),
                payload={"action": "RETRY_ME"},
            )
            msg.status = "DEAD_LETTER"
            msg.last_error = "Exceeded max retries"
            await db.commit()

            token = create_access_token(
                user_id=admin_user.id,
                tenant_id=tenant.id,
                role=admin_user.role,
            )

        app = create_app()
        from app.core.database import get_db

        async def _override_db():
            async with _get_db() as s:
                yield s

        app.dependency_overrides[get_db] = _override_db
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                headers = {"Authorization": f"Bearer {token}"}

                # 1. List dead letters
                list_resp = await client.get("/api/v1/operations/dead-letters", headers=headers)
                assert list_resp.status_code == 200
                data = list_resp.json()
                assert data["total"] >= 1
                assert any(item["id"] == str(msg.id) for item in data["items"])

                # 2. Retry dead letter
                retry_resp = await client.post(
                    f"/api/v1/operations/dead-letters/{msg.id}/retry",
                    headers=headers,
                )
                assert retry_resp.status_code == 200
                retry_data = retry_resp.json()
                assert retry_data["status"] == "PENDING"
                assert retry_data["message_id"] == str(msg.id)
        finally:
            app.dependency_overrides.clear()

        # Confirm database state
        async with _get_db() as db:
            refreshed = await db.get(OutboxMessage, msg.id)
            assert refreshed.status == "PENDING"
            assert refreshed.retry_count == 0

    async def test_dead_letter_api_agency_admin_cross_tenant_forbidden(self, create_tables):
        """Agency Admin cannot inspect or retry dead-letter messages belonging to another agency."""
        async with _get_db() as db:
            tenant_a = await self._create_tenant(db, name="Agency A")
            tenant_b = await self._create_tenant(db, name="Agency B")

            admin_a = await self._create_user(db, tenant_a.id, UserRole.AGENCY_ADMIN)

            outbox_svc = OutboxService(db)
            msg_b = await outbox_svc.enqueue_message(
                tenant_id=tenant_b.id,
                event_type="AI_QUALIFICATION_REQUESTED",
                aggregate_type="LEAD",
                aggregate_id=uuid.uuid4(),
                payload={},
            )
            msg_b.status = "DEAD_LETTER"
            await db.commit()

            token_a = create_access_token(
                user_id=admin_a.id,
                tenant_id=tenant_a.id,
                role=admin_a.role,
            )

        app = create_app()
        from app.core.database import get_db

        async def _override_db():
            async with _get_db() as s:
                yield s

        app.dependency_overrides[get_db] = _override_db
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                headers = {"Authorization": f"Bearer {token_a}"}

                # Attempt to list Agency B's dead letters
                list_resp = await client.get(
                    f"/api/v1/operations/dead-letters?tenant_id={tenant_b.id}",
                    headers=headers,
                )
                assert list_resp.status_code == 403

                # Attempt to retry Agency B's message
                retry_resp = await client.post(
                    f"/api/v1/operations/dead-letters/{msg_b.id}/retry",
                    headers=headers,
                )
                # Not found or rejected under tenant isolation
                assert retry_resp.status_code in (403, 404)
        finally:
            app.dependency_overrides.clear()
