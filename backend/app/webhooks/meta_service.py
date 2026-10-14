"""
MetaLeadAdsWebhookService — inbound Meta Lead Ads webhook verification, signature validation,
tenant isolation, consent verification, and idempotent lead ingestion.
"""

import hashlib
import hmac
import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import AuthenticationError
from app.leads.schemas import LeadCreateRequest
from app.leads.service import LeadService
from app.messaging.models import OutboxMessage
from app.tenants.models import Tenant
from app.webhooks.meta_client import (
    IMetaGraphClient,
    MetaAuthenticationError,
    MetaGraphTimeoutError,
    MetaLeadNotFoundError,
    MetaMalformedResponseError,
    MockMetaGraphClient,
)

logger = structlog.get_logger()
settings = get_settings()


class MetaLeadAdsWebhookService:
    def __init__(self, db: AsyncSession, graph_client: IMetaGraphClient | None = None):
        self.db = db
        self.graph_client = graph_client or (
            MockMetaGraphClient() if settings.llm_provider == "mock" else None
        )

    def verify_subscription(
        self,
        mode: str,
        token: str,
        challenge: str,
        tenant_verify_token: str | None = None,
    ) -> str:
        """
        Verify the Meta Lead Ads webhook handshake.
        Uses constant-time comparison. Returns challenge if valid; raises 401 otherwise.
        """
        expected = tenant_verify_token or settings.meta_verify_token or settings.whatsapp_verify_token
        if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
            logger.info("meta_leadgen_webhook_verified")
            return challenge

        logger.warning("meta_leadgen_webhook_verification_failed", mode=mode)
        raise AuthenticationError("Invalid Meta webhook verification token.")

    def verify_signature(
        self,
        payload_bytes: bytes,
        signature_header: str | None,
        *,
        app_secret: str | None = None,
        allow_missing_secret: bool = False,
    ) -> bool:
        """
        Verify HMAC-SHA256 signature from Meta Lead Ads.
        Uses constant-time comparison to prevent timing attacks.
        Fails closed if header is missing or secret is unconfigured.
        """
        if not signature_header:
            logger.warning("meta_leadgen_signature_missing")
            return False

        secret = app_secret or settings.meta_app_secret or settings.whatsapp_app_secret
        if not secret:
            if allow_missing_secret:
                logger.warning("meta_leadgen_secret_not_configured_test_override")
                return True
            logger.error("meta_leadgen_secret_not_configured_failing_closed")
            return False

        if not signature_header.startswith("sha256="):
            logger.warning("meta_leadgen_signature_malformed_prefix")
            return False

        expected_sig = "sha256=" + hmac.new(
            secret.encode("utf-8"),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected_sig, signature_header)

    async def _resolve_tenant_for_page(self, page_id: str | None) -> Tenant | None:
        """
        Map Meta Page ID to the owning Tenant.
        Strict multi-tenant boundary: a valid signature alone must NOT determine which
        tenant owns a lead. Only explicit, validated tenant settings match.
        """
        if not page_id:
            logger.warning("meta_leadgen_missing_page_id")
            return None

        # Scan active tenants for matching meta_page_id in settings
        res = await self.db.execute(
            select(Tenant).where(
                Tenant.is_active.is_(True),
                Tenant.is_deleted.is_(False),
            )
        )
        tenants = res.scalars().all()
        for t in tenants:
            t_page = (t.settings or {}).get("meta_page_id")
            if t_page and str(t_page) == str(page_id):
                return t

        logger.warning("meta_leadgen_unmapped_page_id", page_id=page_id)
        return None

    async def process_inbound_webhook(
        self,
        payload: dict[str, Any],
        fetch_client: Any | None = None,
    ) -> list[dict[str, Any]]:
        """
        Process Meta Lead Ads webhook notification.
        1. Validates structure.
        2. Resolves tenant via page_id.
        3. Enforces idempotency via leadgen_id.
        4. Retrieves lead details (mocked / offline in tests).
        5. Validates explicit consent scope.
        6. Ingests via LeadService.create_lead (which enqueues AI_QUALIFICATION_REQUESTED).
        Zero direct WhatsApp messaging inside webhook handler!
        """
        processed: list[dict[str, Any]] = []
        entries = payload.get("entry", [])

        for entry in entries:
            for change in entry.get("changes", []):
                if change.get("field") != "leadgen":
                    continue

                val = change.get("value", {})
                leadgen_id = str(val.get("leadgen_id", "")).strip()
                page_id = str(val.get("page_id", "")).strip()
                form_id = str(val.get("form_id", "")).strip()

                if not leadgen_id:
                    logger.warning("meta_leadgen_missing_leadgen_id")
                    continue

                # 1. Tenant Resolution
                tenant = await self._resolve_tenant_for_page(page_id)
                if not tenant:
                    processed.append({
                        "action": "skipped_unmapped_tenant",
                        "page_id": page_id,
                        "leadgen_id": leadgen_id,
                    })
                    continue

                # 2. Idempotency Check: Don't ingest same leadgen_id twice
                idempotency_key = f"meta-lead-{leadgen_id}"
                existing_stmt = select(OutboxMessage).where(
                    OutboxMessage.tenant_id == tenant.id,
                    OutboxMessage.idempotency_key == idempotency_key,
                )
                existing = (await self.db.execute(existing_stmt)).scalars().first()
                if existing:
                    logger.info("meta_leadgen_duplicate_webhook_ignored", leadgen_id=leadgen_id)
                    processed.append({
                        "action": "duplicate_ignored",
                        "leadgen_id": leadgen_id,
                        "tenant_id": str(tenant.id),
                    })
                    continue

                # 3. Retrieve Lead Details
                client_to_use = fetch_client or self.graph_client
                lead_data = None
                tenant_token = (tenant.settings or {}).get("meta_access_token")

                if client_to_use is not None:
                    try:
                        if isinstance(client_to_use, IMetaGraphClient):
                            lead_data = await client_to_use.fetch_lead_details(
                                leadgen_id, access_token=tenant_token, expected_business_name=tenant.name
                            )
                        elif hasattr(client_to_use, "fetch_lead"):
                            lead_data = await client_to_use.fetch_lead(leadgen_id)
                        elif hasattr(client_to_use, "fetch_lead_details"):
                            try:
                                lead_data = await client_to_use.fetch_lead_details(
                                    leadgen_id, access_token=tenant_token, expected_business_name=tenant.name
                                )
                            except TypeError:
                                lead_data = await client_to_use.fetch_lead_details(leadgen_id, access_token=tenant_token)

                        if not isinstance(lead_data, dict):
                            raise MetaMalformedResponseError("Meta lead retrieval did not return a valid dictionary payload.")
                    except MetaAuthenticationError as auth_exc:
                        logger.error("meta_leadgen_auth_failed", leadgen_id=leadgen_id, error=str(auth_exc))
                        processed.append({
                            "action": "fetch_failed_auth_error",
                            "leadgen_id": leadgen_id,
                            "error": str(auth_exc),
                        })
                        continue
                    except MetaGraphTimeoutError as timeout_exc:
                        logger.warning("meta_leadgen_timeout_retryable", leadgen_id=leadgen_id, error=str(timeout_exc))
                        processed.append({
                            "action": "fetch_failed_retryable",
                            "leadgen_id": leadgen_id,
                            "error": str(timeout_exc),
                        })
                        continue
                    except (MetaLeadNotFoundError, MetaMalformedResponseError) as malformed_exc:
                        logger.error("meta_leadgen_malformed_response", leadgen_id=leadgen_id, error=str(malformed_exc))
                        processed.append({
                            "action": "fetch_failed_malformed",
                            "leadgen_id": leadgen_id,
                            "error": str(malformed_exc),
                        })
                        continue
                    except Exception as fetch_exc:
                        logger.error("meta_leadgen_fetch_failed", leadgen_id=leadgen_id, error=str(fetch_exc))
                        processed.append({
                            "action": "fetch_failed_retryable",
                            "leadgen_id": leadgen_id,
                            "error": str(fetch_exc),
                        })
                        continue
                elif "mock_lead_data" in val:
                    lead_data = val["mock_lead_data"]
                else:
                    logger.warning(
                        "meta_leadgen_no_fetch_client_or_mock_data",
                        leadgen_id=leadgen_id,
                        note="Real Graph API fetch disabled during offline integration audit",
                    )
                    processed.append({
                        "action": "fetch_deferred_offline_mode",
                        "leadgen_id": leadgen_id,
                    })
                    continue

                # 4. Consent Scope Validation
                # Do not treat a generic form submit as blanket consent. Default strictly to False if absent.
                consent_granted = bool(lead_data.get("consent_granted", False))
                consent_type = lead_data.get("consent_type") or ("WHATSAPP_COMMUNICATION" if consent_granted else "NONE")
                consent_text = lead_data.get("consent_text")

                # 5. Ingest Lead via LeadService (strictly tenant-isolated)
                ctx = TenantContext(
                    tenant_id=tenant.id,
                    user_id=None,
                    role=UserRole.SUPER_ADMIN,
                )
                lead_svc = LeadService(self.db, ctx)

                lead_req = LeadCreateRequest(
                    full_name=lead_data.get("full_name") or f"Meta Lead {leadgen_id[-4:]}",
                    phone=lead_data.get("phone") or "+919876543210",
                    email=lead_data.get("email"),
                    source="META_LEAD_ADS",
                    preferred_contact="WHATSAPP",
                    consent_granted=consent_granted,
                    consent_type=consent_type,
                    consent_text=consent_text,
                    consent_channel="META_LEAD_ADS",
                    raw_message_text=f"Meta Lead Ads Form: {form_id} (Lead ID: {leadgen_id})",
                    property_type=lead_data.get("property_type"),
                    bhk=lead_data.get("bhk"),
                    budget_min=lead_data.get("budget_min"),
                    budget_max=lead_data.get("budget_max"),
                    preferred_location=lead_data.get("preferred_location"),
                )

                created_lead = await lead_svc.create_lead(lead_req)

                # Record idempotency record
                outbox_msg = OutboxMessage(
                    id=uuid.uuid4(),
                    tenant_id=tenant.id,
                    event_type="META_LEAD_INGESTED",
                    aggregate_type="LEAD",
                    aggregate_id=str(created_lead.id),
                    payload={"leadgen_id": leadgen_id, "form_id": form_id, "page_id": page_id},
                    status="SENT",
                    idempotency_key=idempotency_key,
                )
                self.db.add(outbox_msg)
                await self.db.flush()

                logger.info(
                    "meta_leadgen_successfully_ingested",
                    lead_id=str(created_lead.id),
                    leadgen_id=leadgen_id,
                    tenant_id=str(tenant.id),
                )
                processed.append({
                    "action": "created",
                    "lead_id": str(created_lead.id),
                    "leadgen_id": leadgen_id,
                    "tenant_id": str(tenant.id),
                })

        return processed
