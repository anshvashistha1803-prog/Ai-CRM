"""
WhatsAppWebhookService — inbound WhatsApp message processing, subscription verification,
signature validation, tenant routing, consent opt-out enforcement, and deduplicated lead creation.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError
from app.core.security import compute_blind_hash, encrypt_field, verify_whatsapp_signature
from app.leads.deduplication import normalize_phone
from app.leads.models import ConsentRecord, Lead, LeadActivity
from app.leads.state_machine import LeadStage
from app.messaging.outbox_service import OutboxService
from app.tenants.models import Tenant

logger = structlog.get_logger()
settings = get_settings()

OPT_OUT_KEYWORDS = frozenset({"STOP", "UNSUBSCRIBE", "OPT OUT", "OPTOUT", "CANCEL", "DO NOT CONTACT"})


class WhatsAppWebhookService:
    def __init__(self, db: AsyncSession):
        self.db = db

    def verify_subscription(
        self,
        mode: str,
        token: str,
        challenge: str,
        tenant_verify_token: str | None = None,
    ) -> str:
        """
        Verify the Meta webhook handshake.
        Returns the challenge string if token matches; raises 401 if invalid.
        """
        expected_token = tenant_verify_token or settings.whatsapp_verify_token
        if mode == "subscribe" and token == expected_token:
            logger.info("whatsapp_webhook_verified")
            return challenge

        logger.warning("whatsapp_webhook_verification_failed", mode=mode)
        raise AuthenticationError("Invalid webhook verification token.")

    def verify_signature(self, payload_bytes: bytes, signature_header: str) -> bool:
        """Verify HMAC-SHA256 signature from Meta."""
        if not settings.whatsapp_app_secret:
            # When app secret is not configured in dev/testing, allow requests with structured log
            logger.warning("whatsapp_app_secret_not_configured_skipping_signature_verification")
            return True
        return verify_whatsapp_signature(payload_bytes, signature_header)

    async def _resolve_tenant(self, phone_number_id: str | None) -> Tenant | None:
        """Map Meta phone_number_id to the owning Tenant."""
        if phone_number_id:
            res = await self.db.execute(
                select(Tenant).where(
                    Tenant.whatsapp_phone_number_id == phone_number_id,
                    Tenant.is_deleted.is_(False),
                )
            )
            tenant = res.scalars().first()
            if tenant:
                return tenant

        # Fallback to the first active tenant if single-tenant or mock
        res = await self.db.execute(
            select(Tenant).where(Tenant.is_deleted.is_(False)).limit(1)
        )
        return res.scalars().first()

    async def process_inbound_webhook(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Parse and persist inbound WhatsApp messages and status events.
        Enforces tenant isolation, consent opt-out, and deduping.
        """
        processed_events: list[dict[str, Any]] = []
        entries = payload.get("entry", [])

        now = datetime.now(UTC)
        outbox_svc = OutboxService(self.db)

        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                metadata = value.get("metadata", {})
                phone_number_id = metadata.get("phone_number_id")

                tenant = await self._resolve_tenant(phone_number_id)
                if not tenant:
                    logger.warning("whatsapp_no_tenant_resolved", phone_number_id=phone_number_id)
                    continue

                # 1. Handle Inbound Messages
                messages = value.get("messages", [])
                contacts = {
                    c.get("wa_id"): c.get("profile", {}).get("name")
                    for c in value.get("contacts", [])
                }

                for msg in messages:
                    sender_wa_id = msg.get("from")
                    if not sender_wa_id:
                        continue

                    # Extract body text or button reply
                    text = ""
                    if msg.get("type") == "text":
                        text = msg.get("text", {}).get("body", "").strip()
                    elif msg.get("type") == "button":
                        text = msg.get("button", {}).get("text", "").strip()
                    elif msg.get("type") == "interactive":
                        interactive = msg.get("interactive", {})
                        text = (
                            interactive.get("button_reply", {}).get("title")
                            or interactive.get("list_reply", {}).get("title")
                            or ""
                        ).strip()

                    try:
                        norm_phone = normalize_phone("+" + sender_wa_id.lstrip("+"))
                    except Exception:
                        norm_phone = "+" + sender_wa_id.lstrip("+")

                    phone_hash = compute_blind_hash(norm_phone)
                    sender_name = contacts.get(sender_wa_id) or f"WhatsApp Lead {norm_phone[-4:]}"

                    # Opt-out check
                    is_opt_out = text.upper() in OPT_OUT_KEYWORDS

                    # Find existing lead in agency
                    lead_res = await self.db.execute(
                        select(Lead).where(
                            Lead.tenant_id == tenant.id,
                            Lead.phone_hash == phone_hash,
                            Lead.is_deleted.is_(False),
                        )
                    )
                    lead = lead_res.scalars().first()

                    async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
                        if is_opt_out:
                            if lead:
                                lead.do_not_contact = True
                                lead.stage = LeadStage.DO_NOT_CONTACT.value
                                lead.automation_status = "OPTED_OUT"
                                lead.last_activity_at = now

                                consent = ConsentRecord(
                                    id=uuid.uuid4(),
                                    tenant_id=tenant.id,
                                    lead_id=lead.id,
                                    consent_type="WHATSAPP",
                                    granted=False,
                                    channel="WHATSAPP",
                                    consent_text=f"User texted opt-out keyword: '{text}'",
                                    created_at=now,
                                )
                                self.db.add(consent)

                                activity = LeadActivity(
                                    id=uuid.uuid4(),
                                    tenant_id=tenant.id,
                                    lead_id=lead.id,
                                    actor_type="SYSTEM",
                                    activity_type="CONSENT_REVOKED",
                                    description="WhatsApp consent revoked by user opt-out keyword",
                                    metadata_={"keyword": text},
                                    created_at=now,
                                )
                                self.db.add(activity)

                            logger.info("whatsapp_lead_opted_out", phone_hash=phone_hash, tenant_id=str(tenant.id))
                            processed_events.append({"action": "opt_out", "lead_id": str(lead.id) if lead else None})
                            continue

                        # Active lead creation or update
                        if lead:
                            lead.raw_message_text = f"{lead.raw_message_text or ''}\nClient: {text}".strip()
                            lead.last_activity_at = now
                            activity = LeadActivity(
                                id=uuid.uuid4(),
                                tenant_id=tenant.id,
                                lead_id=lead.id,
                                actor_type="USER",
                                activity_type="WHATSAPP_MESSAGE_RECEIVED",
                                description=f"Inbound message: {text[:100]}",
                                metadata_={"wamid": msg.get("id"), "text": text},
                                created_at=now,
                            )
                            self.db.add(activity)
                            action = "updated"
                        else:
                            lead_id = uuid.uuid4()
                            lead = Lead(
                                id=lead_id,
                                tenant_id=tenant.id,
                                source="WHATSAPP",
                                full_name=sender_name,
                                phone_e164_enc=encrypt_field(norm_phone),
                                phone_hash=phone_hash,
                                preferred_contact="WHATSAPP",
                                stage=LeadStage.NEW.value,
                                raw_message_text=f"Client: {text}",
                                last_activity_at=now,
                                created_at=now,
                                updated_at=now,
                                is_deleted=False,
                            )
                            self.db.add(lead)

                            consent = ConsentRecord(
                                id=uuid.uuid4(),
                                tenant_id=tenant.id,
                                lead_id=lead_id,
                                consent_type="WHATSAPP",
                                granted=True,
                                channel="WHATSAPP",
                                consent_text="Inbound WhatsApp message initiated by user",
                                created_at=now,
                            )
                            self.db.add(consent)

                            activity = LeadActivity(
                                id=uuid.uuid4(),
                                tenant_id=tenant.id,
                                lead_id=lead_id,
                                actor_type="SYSTEM",
                                activity_type="LEAD_CREATED",
                                description="Lead automatically created from inbound WhatsApp message",
                                metadata_={"wamid": msg.get("id")},
                                created_at=now,
                            )
                            self.db.add(activity)
                            action = "created"

                        # Enqueue background outbox task for AI qualification
                        await outbox_svc.enqueue_message(
                            tenant_id=tenant.id,
                            event_type="AI_QUALIFICATION_REQUESTED",
                            aggregate_type="LEAD",
                            aggregate_id=lead.id,
                            payload={
                                "message_text": text,
                                "source": "WHATSAPP",
                                "sender_phone": norm_phone,
                            },
                            idempotency_key=f"qual-{msg.get('id')}",
                        )

                        await self.db.flush()

                    processed_events.append({"action": action, "lead_id": str(lead.id)})

                # 2. Handle Message Status Updates (sent, delivered, read, failed)
                statuses = value.get("statuses", [])
                for status_obj in statuses:
                    wamid = status_obj.get("id")
                    st = status_obj.get("status")
                    logger.info("whatsapp_status_update", wamid=wamid, status=st)
                    processed_events.append({"action": "status_update", "wamid": wamid, "status": st})

        return processed_events
