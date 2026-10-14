"""
QualificationConversationService — manages turn-by-turn conversational lead qualification
for HAVN Realty via WhatsApp. Implements explicit state nodes, bilingual greeting,
BUY vs SELL vs RENT branching, one question per message, skip support, greedy extraction,
deterministic scoring, broker handoff, and hard automation stop.
"""

import contextlib
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.core.security import decrypt_field
from app.leads.models import ConsentRecord, Lead, LeadActivity
from app.leads.state_machine import AutomationStatus, LeadStage
from app.messaging.models import OutboxMessage
from app.messaging.outbox_service import OutboxService
from app.qualification.models import LeadQualificationProposal
from app.scoring.engine import ScoringEngine
from app.tenants.models import Tenant

logger = structlog.get_logger()

SKIP_TOKENS = frozenset({
    "skip", "pass", "next", "nahi", "no", "chodo", "leave", "leave it",
    "na", "none", "not now", "skip this", "baad mein", "later"
})

OPT_OUT_TOKENS = frozenset({
    "stop", "unsubscribe", "opt out", "optout", "cancel", "block",
    "don't message", "dont message", "do not message", "mat bhejo",
    "band karo", "stop messaging", "remove me", "do not contact"
})

HUMAN_REQUEST_TOKENS = frozenset({
    "broker", "agent", "human", "call", "call me", "talk to human",
    "talk to agent", "connect me", "broker se baat", "phone karo",
    "speak to someone", "representative", "executive"
})

HINGLISH_INDICATORS = frozenset({
    "kya", "hai", "chahiye", "kar", "karna", "batao", "bhi", "mein", "ka", "ki",
    "ke", "kothi", "gaj", "bhai", "haan", "nahi", "accha", "dijiye", "hum", "aap",
    "namaste", "kitna", "dehradun"
})



class ConversationResponse:
    def __init__(
        self,
        reply_text: str | None,
        lead_id: uuid.UUID,
        stage: str,
        automation_status: str,
        is_completed: bool = False,
        is_human_takeover: bool = False,
        summary: str | None = None,
    ) -> None:
        self.reply_text = reply_text
        self.lead_id = lead_id
        self.stage = stage
        self.automation_status = automation_status
        self.is_completed = is_completed
        self.is_human_takeover = is_human_takeover
        self.summary = summary


class QualificationConversationService:
    def __init__(self, db: AsyncSession):
        self.db = db

    def detect_language(self, text: str) -> str:
        """Detect whether message is primarily English or Hinglish/Hindi."""
        words = {w.lower().strip("?,.!") for w in text.split()}
        if words.intersection(HINGLISH_INDICATORS):
            return "HINGLISH"
        return "EN"

    def is_skip_message(self, text: str) -> bool:
        cleaned = text.strip().lower()
        return cleaned in SKIP_TOKENS or any(s == cleaned for s in SKIP_TOKENS)

    def is_human_request(self, text: str) -> bool:
        cleaned = text.strip().lower()
        return any(tok in cleaned for tok in HUMAN_REQUEST_TOKENS)

    def is_opt_out(self, text: str) -> bool:
        cleaned = text.strip().lower()
        return cleaned in OPT_OUT_TOKENS or any(tok in cleaned for tok in OPT_OUT_TOKENS)

    def extract_entities(self, text: str, existing_answers: dict[str, Any]) -> dict[str, Any]:

        """Greedily extract real estate criteria from text without overwriting confirmed answers."""
        extracted: dict[str, Any] = {}
        t_lower = text.lower()

        # 1. Intent: BUY, SELL, RENT
        if any(w in t_lower for w in ["rent", "rental", "lease", "kiraya", "renting"]):
            extracted["intent"] = "RENT"
        elif any(w in t_lower for w in ["sell", "listing", "list my", "bechna", "bech"]):
            extracted["intent"] = "SELL"
        elif any(w in t_lower for w in ["buy", "purchase", "kharidna", "kharid", "looking to buy", "want to buy"]) or any(f"{p} chahiye" in t_lower for p in ["property", "flat", "ghar", "kothi", "plot", "villa", "zameen", "land", "house"]):
            extracted["intent"] = "BUY"

        # 2. Property Type (Check PENTHOUSE before VILLA/HOUSE to avoid substring overlap with 'house')
        if any(w in t_lower for w in ["penthouse", "pent house"]):
            extracted["property_type"] = "PENTHOUSE"
        elif any(w in t_lower for w in ["villa", "kothi", "house", "bungalow", "independent house"]):
            extracted["property_type"] = "VILLA"
        elif any(w in t_lower for w in ["flat", "apartment", "luxury flat", "society flat"]):
            extracted["property_type"] = "FLAT"
        elif any(w in t_lower for w in ["plot", "land", "zameen", "residential land", "commercial land"]):
            extracted["property_type"] = "PLOT"
        elif any(w in t_lower for w in ["commercial", "office", "retail", "shop", "showroom"]):
            extracted["property_type"] = "COMMERCIAL"

        # 3. Asking price / Price for sellers
        if any(w in t_lower for w in ["asking", "price", "rate", "demand", "per gaj"]):
            p_match = re.search(r"(\d+(?:,\d+)*(?:\.\d+)?\s*(?:cr|crore|lakh|lakhs|per gaj|gaj)?)", t_lower)
            if p_match:
                extracted["asking_price"] = p_match.group(1).strip()

        # 3. BHK
        bhk_match = re.search(r"(\d)\s*(?:bhk|bedroom|bed)", t_lower)
        if bhk_match:
            extracted["bhk"] = int(bhk_match.group(1))

        # 4. Budget
        cr_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:cr|crore|crores)", t_lower)
        lakh_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:lakh|lakhs|lac|lacs|l)", t_lower)
        if cr_match:
            val = int(float(cr_match.group(1)) * 10_000_000)
            extracted["budget_max"] = val
            extracted["budget_display"] = f"Up to {cr_match.group(1)} Cr"
        elif lakh_match:
            val = int(float(lakh_match.group(1)) * 100_000)
            extracted["budget_max"] = val
            extracted["budget_display"] = f"Up to {lakh_match.group(1)} Lakhs"

        # 5. Location
        for loc in ["rajpur road", "balbir road", "jakhan", "dalanawala", "mussoorie road",
                    "sahastradhara", "dehradun", "kuanwala", "race course", "nakronda"]:
            if loc in t_lower:
                extracted["preferred_location"] = loc.title()
                break

        # 6. Timeline
        if any(w in t_lower for w in ["immediate", "ready", "asap", "jaldi", "1 month", "15 days"]):
            extracted["move_in_timeline"] = "IMMEDIATE"
        elif any(w in t_lower for w in ["2 months", "3 months", "soon", "1-3 months"]):
            extracted["move_in_timeline"] = "1-3 MONTHS"
        elif any(w in t_lower for w in ["6 months", "year", "exploring"]):
            extracted["move_in_timeline"] = "> 3 MONTHS"

        # 7. Purpose
        if any(w in t_lower for w in ["primary", "self use", "rehna", "living", "family", "residence", "self"]):
            extracted["purpose"] = "PRIMARY RESIDENCE"

        elif any(w in t_lower for w in ["second home", "vacation", "holiday home"]):
            extracted["purpose"] = "SECOND HOME"
        elif any(w in t_lower for w in ["invest", "investment", "returns"]):
            extracted["purpose"] = "INVESTMENT"

        # 8. Dimensions / Size (e.g. "40*80", "240 gaj", "3000 sq ft")
        dim_match = re.search(r"(\d+\s*[*xX]\s*\d+)", text)
        if dim_match:
            extracted["dimensions"] = dim_match.group(1)
        size_match = re.search(r"(\d+)\s*(?:gaj|guz|sq\.?yd|sqft|sq ft)", t_lower)
        if size_match:
            extracted["size"] = size_match.group(0)

        # 9. Parking
        parking_match = re.search(r"(\d+)\s*(?:car|parking)", t_lower)
        if parking_match:
            extracted["parking"] = f"{parking_match.group(1)} Cars"

        # 10. Decision Maker
        if any(w in t_lower for w in ["sole", "sole decision maker", "i am the decision maker", "individual"]):
            extracted["decision_maker"] = "SOLE"
        elif any(w in t_lower for w in ["family", "joint", "spouse", "partner"]):
            extracted["decision_maker"] = "FAMILY"

        # 11. Budget Flexibility
        if any(w in t_lower for w in ["flexible", "stretch", "negotiable", "can extend"]):
            extracted["budget_flexibility"] = "FLEXIBLE"
        elif any(w in t_lower for w in ["fixed", "strict", "firm"]):
            extracted["budget_flexibility"] = "FIXED"

        # 12. Delay Factors
        if any(w in t_lower for w in ["no delay", "no factors", "none", "no issues"]):
            extracted["delay_factors"] = "NONE"

        # 13. Built-up area / specifications
        area_match = re.search(r"(\d+)\s*(?:sq\s*ft|sqft|sq\.?\s*ft|sq\s*yd)", t_lower)
        if area_match:
            extracted["built_up_area"] = area_match.group(0)

        # Merge extracted with existing, honoring chronological corrections from latest message
        merged = dict(existing_answers)
        for k, v in extracted.items():
            if v is not None:
                merged[k] = v
        return merged


    def _resolve_recipient_phone(self, lead: Lead, sender_phone: str | None = None) -> str | None:
        """Resolve unencrypted phone number for outbound dispatch."""
        if sender_phone:
            return sender_phone
        if not lead.phone_e164_enc:
            return None
        try:
            return decrypt_field(lead.phone_e164_enc)
        except Exception:
            return lead.phone_e164_enc if not lead.phone_e164_enc.startswith("enc:") else None

    async def _seed_answers(self, lead: Lead, existing_answers: dict[str, Any] | None = None) -> dict[str, Any]:
        """Seed conversation answers from lead record and any active proposal."""
        answers = dict(existing_answers or {})

        # 1. Lead intent / purpose
        if "intent" not in answers:
            if getattr(lead, "intent", None):
                answers["intent"] = str(lead.intent).upper()
            elif lead.purpose:
                p_upper = lead.purpose.upper()
                if "SELL" in p_upper:
                    answers["intent"] = "SELL"
                elif "RENT" in p_upper:
                    answers["intent"] = "RENT"
                elif any(k in p_upper for k in ["BUY", "INVEST", "RESIDENCE", "PURCHASE"]):
                    answers["intent"] = "BUY"
            elif lead.raw_message_text:
                rmt = lead.raw_message_text.lower()
                if any(w in rmt for w in ["rent", "rental", "lease", "kiraya"]):
                    answers["intent"] = "RENT"
                elif any(w in rmt for w in ["sell", "listing", "list my", "bechna"]):
                    answers["intent"] = "SELL"
                elif any(w in rmt for w in ["buy", "purchase", "kharidna"]):
                    answers["intent"] = "BUY"

        # 2. Canonical lead attributes
        if "property_type" not in answers and lead.property_type:
            answers["property_type"] = lead.property_type.upper()
        if "bhk" not in answers and lead.bhk is not None:
            answers["bhk"] = lead.bhk
        if "preferred_location" not in answers and lead.preferred_location:
            answers["preferred_location"] = lead.preferred_location
        if "budget_max" not in answers and lead.budget_max is not None:
            answers["budget_max"] = lead.budget_max
        if "budget_min" not in answers and lead.budget_min is not None:
            answers["budget_min"] = lead.budget_min
        if "move_in_timeline" not in answers and lead.move_in_timeline:
            answers["move_in_timeline"] = lead.move_in_timeline
        if "purpose" not in answers and lead.purpose:
            answers["purpose"] = lead.purpose

        # 3. Seed from active proposal if criteria missing on lead record
        prop_stmt = (
            select(LeadQualificationProposal)
            .where(
                LeadQualificationProposal.tenant_id == lead.tenant_id,
                LeadQualificationProposal.lead_id == lead.id,
                LeadQualificationProposal.status.in_(["PENDING_REVIEW", "ACCEPTED"]),
            )
            .order_by(LeadQualificationProposal.created_at.desc())
        )
        prop_res = await self.db.execute(prop_stmt)
        proposal = prop_res.scalars().first()
        if proposal:
            if "property_type" not in answers and proposal.property_type:
                answers["property_type"] = proposal.property_type.upper()
            if "bhk" not in answers and proposal.bhk is not None:
                answers["bhk"] = proposal.bhk
            if "preferred_location" not in answers and proposal.preferred_location:
                answers["preferred_location"] = proposal.preferred_location
            if "budget_max" not in answers and proposal.budget_max is not None:
                answers["budget_max"] = proposal.budget_max
            if "budget_min" not in answers and proposal.budget_min is not None:
                answers["budget_min"] = proposal.budget_min
            if "move_in_timeline" not in answers and proposal.move_in_timeline:
                answers["move_in_timeline"] = proposal.move_in_timeline
            if "purpose" not in answers and proposal.purpose:
                answers["purpose"] = proposal.purpose

        return answers

    async def _sync_answers_to_proposal(self, lead: Lead, answers: dict[str, Any]) -> None:
        """Update any active PENDING_REVIEW proposal with extracted criteria, preserving nulls."""
        stmt = (
            select(LeadQualificationProposal)
            .where(
                LeadQualificationProposal.tenant_id == lead.tenant_id,
                LeadQualificationProposal.lead_id == lead.id,
                LeadQualificationProposal.status == "PENDING_REVIEW",
            )
            .order_by(LeadQualificationProposal.created_at.desc())
        )
        res = await self.db.execute(stmt)
        proposal = res.scalars().first()
        if not proposal:
            return

        if answers.get("property_type") and answers["property_type"] != "SKIPPED":
            proposal.property_type = answers["property_type"]
        if answers.get("bhk") and answers["bhk"] != "SKIPPED":
            with contextlib.suppress(ValueError, TypeError):
                proposal.bhk = int(answers["bhk"])
        if answers.get("preferred_location") and answers["preferred_location"] != "SKIPPED":
            proposal.preferred_location = answers["preferred_location"]
        if answers.get("budget_max") and answers["budget_max"] != "SKIPPED":
            with contextlib.suppress(ValueError, TypeError):
                proposal.budget_max = int(answers["budget_max"])
        if answers.get("budget_min") and answers["budget_min"] != "SKIPPED":
            with contextlib.suppress(ValueError, TypeError):
                proposal.budget_min = int(answers["budget_min"])
        if answers.get("purpose") and answers["purpose"] != "SKIPPED":
            proposal.purpose = answers["purpose"]
        if answers.get("move_in_timeline") and answers["move_in_timeline"] != "SKIPPED":
            proposal.move_in_timeline = answers["move_in_timeline"]

    async def check_messaging_eligibility(
        self,
        lead: Lead,
        tenant: Tenant | None = None,
    ) -> tuple[bool, str]:
        """
        Determine whether the lead has a valid WhatsApp-capable phone number,
        necessary consent, active automation status, and tenant configuration.
        Returns (is_eligible, reason).
        """
        if lead.is_deleted:
            return False, "lead_is_deleted"

        if lead.do_not_contact:
            return False, "lead_do_not_contact"

        if lead.automation_status in {
            AutomationStatus.HUMAN_TAKEOVER.value,
            AutomationStatus.PAUSED.value,
            AutomationStatus.OPTED_OUT.value,
        }:
            return False, f"lead_automation_{lead.automation_status}"

        # Inbound automated qualification is intended for WEBSITE, META_LEAD_ADS, and WHATSAPP
        if lead.source not in {"WEBSITE", "META_LEAD_ADS", "WHATSAPP"}:
            return False, f"lead_source_ineligible_{lead.source}"

        # Phone validation: decrypt and verify E.164
        phone = self._resolve_recipient_phone(lead)
        if not phone or not re.match(r"^\+[1-9]\d{6,14}$", phone):
            return False, "invalid_or_missing_e164_phone"

        # Preferred contact channel check
        if lead.preferred_contact and lead.preferred_contact.upper() not in {"WHATSAPP", "PHONE", "CALL"}:
            return False, f"preferred_contact_not_whatsapp_{lead.preferred_contact}"

        # Consent verification
        consent_stmt = (
            select(ConsentRecord)
            .where(
                ConsentRecord.tenant_id == lead.tenant_id,
                ConsentRecord.lead_id == lead.id,
            )
            .order_by(ConsentRecord.created_at.desc())
        )
        res = await self.db.execute(consent_stmt)
        consents = res.scalars().all()
        if not consents:
            return False, "missing_consent_record"

        latest_consent = consents[0]
        if not latest_consent.granted:
            return False, "consent_revoked_or_denied"

        # Verify tenant WhatsApp configuration
        t = tenant
        if not t:
            t_stmt = select(Tenant).where(Tenant.id == lead.tenant_id, Tenant.is_deleted.is_(False))
            t_res = await self.db.execute(t_stmt)
            t = t_res.scalars().first()

        if not t or not t.is_active:
            return False, "tenant_inactive_or_not_found"

        has_waba = bool(t.whatsapp_phone_number_id) or bool((t.settings or {}).get("whatsapp_enabled"))
        if not has_waba:
            return False, "tenant_whatsapp_not_configured"

        return True, "eligible"

    async def schedule_initial_contact(self, lead_id: uuid.UUID) -> OutboxMessage | None:
        """
        Evaluate eligibility, seed initial answers, determine the first missing question,
        and enqueue initial WhatsApp outreach template via transactional outbox.
        """
        stmt = select(Lead).where(Lead.id == lead_id, Lead.is_deleted.is_(False))
        res = await self.db.execute(stmt)
        lead = res.scalars().first()
        if not lead:
            logger.warning("schedule_initial_contact_lead_not_found", lead_id=str(lead_id))
            return None

        is_eligible, reason = await self.check_messaging_eligibility(lead)
        if not is_eligible:
            logger.info(
                "whatsapp_qualification_initial_contact_ineligible",
                lead_id=str(lead.id),
                reason=reason,
            )
            return None

        # Pre-seed answers from lead attributes and active proposals
        answers = await self._seed_answers(lead)

        # Sync pre-seeded criteria to existing proposal if any
        await self._sync_answers_to_proposal(lead, answers)

        lang = "EN"
        next_node, first_question, is_complete = self._determine_next_step(answers, lang)
        if is_complete:
            logger.info("qualification_already_complete_no_initial_outreach_needed", lead_id=str(lead.id))
            return None

        recipient_phone = self._resolve_recipient_phone(lead)
        if not recipient_phone:
            logger.warning("missing_recipient_phone_for_initial_contact", lead_id=str(lead.id))
            return None

        now = datetime.now(UTC)

        breakdown = lead.score_breakdown or {}
        state_data = {
            "current_node": next_node,
            "answers": answers,
            "turns_count": 0,
            "language": lang,
        }
        breakdown["conversation_state"] = state_data
        lead.score_breakdown = breakdown
        lead.stage = LeadStage.QUALIFYING.value
        lead.last_activity_at = now

        activity = LeadActivity(
            id=uuid.uuid4(),
            tenant_id=lead.tenant_id,
            lead_id=lead.id,
            actor_type="SYSTEM",
            activity_type="QUALIFICATION_STEP",
            description=f"Scheduled initial WhatsApp outreach: {first_question[:100]}",
            metadata_={"node": next_node, "template": "lead_qualification_intro"},
            created_at=now,
        )
        self.db.add(activity)

        outbox_svc = OutboxService(self.db)
        outbox_msg = await outbox_svc.enqueue_message(
            tenant_id=lead.tenant_id,
            event_type="WHATSAPP_OUTBOUND_DISPATCH",
            aggregate_type="LEAD",
            aggregate_id=lead.id,
            payload={
                "recipient_phone": recipient_phone,
                "template_name": "lead_qualification_intro",
                "message_text": first_question,
                "first_question": first_question,
                "lead_name": lead.full_name,
            },
            idempotency_key=f"wa-init-{lead.id}",
        )
        await self.db.flush()
        logger.info(
            "whatsapp_qualification_initial_contact_scheduled",
            lead_id=str(lead.id),
            node=next_node,
        )
        return outbox_msg


    async def process_inbound_message(
        self,
        lead_id: uuid.UUID,
        message_text: str,
        sender_phone: str | None = None,
    ) -> ConversationResponse:
        """
        Process an inbound message through the HAVN qualification state machine.
        """
        stmt = select(Lead).where(Lead.id == lead_id, Lead.is_deleted.is_(False))
        res = await self.db.execute(stmt)
        lead = res.scalars().first()
        if not lead:
            raise NotFoundError(f"Lead {lead_id} not found.")

        # Hard guard: Stop automated qualification if already handed off, paused, or opted out
        if lead.automation_status in {
            AutomationStatus.HUMAN_TAKEOVER.value,
            AutomationStatus.PAUSED.value,
            AutomationStatus.OPTED_OUT.value,
        }:
            logger.info(
                "qualification_skipped_due_to_automation_status",
                lead_id=str(lead.id),
                status=lead.automation_status,
            )
            return ConversationResponse(
                reply_text=None,
                lead_id=lead.id,
                stage=lead.stage,
                automation_status=lead.automation_status,
            )

        now = datetime.now(UTC)
        lang = self.detect_language(message_text)

        # 1. Check for opt-out request
        if self.is_opt_out(message_text):
            return await self._handle_opt_out(lead, lang, message_text, now)

        # 2. Check for immediate human takeover request
        if self.is_human_request(message_text):
            return await self._handle_human_takeover(lead, lang, message_text, now)

        # Retrieve saved conversation state from score_breakdown, seeded with lead attributes
        breakdown = lead.score_breakdown or {}
        state_data = breakdown.get("conversation_state") or {
            "current_node": "ENTRY",
            "answers": {},
            "turns_count": 0,
            "language": lang,
        }
        answers: dict[str, Any] = await self._seed_answers(lead, state_data.get("answers", {}))
        turns_count = state_data.get("turns_count", 0) + 1

        # Check skip
        is_skip = self.is_skip_message(message_text)

        # Greedy entity extraction
        if not is_skip:
            answers = self.extract_entities(message_text, answers)

        current_node = state_data.get("current_node", "ENTRY")

        # Handle specific answer capture based on current node
        if is_skip:
            if current_node == "AWAIT_SPECS":
                answers["specs"] = "SKIPPED"
            elif current_node == "AWAIT_LOCATION":
                answers["preferred_location"] = "SKIPPED"
            elif current_node == "AWAIT_BUDGET":
                answers["budget_raw"] = "SKIPPED"
            elif current_node == "AWAIT_TIMELINE":
                answers["move_in_timeline"] = "SKIPPED"
            elif current_node == "AWAIT_PURPOSE":
                answers["purpose"] = "SKIPPED"
            elif current_node == "AWAIT_ASKING_PRICE":
                answers["asking_price"] = "SKIPPED"
        else:
            if current_node == "AWAIT_INTENT":
                if "intent" not in answers:
                    if "buy" in message_text.lower():
                        answers["intent"] = "BUY"
                    elif "sell" in message_text.lower():
                        answers["intent"] = "SELL"
                    elif "rent" in message_text.lower():
                        answers["intent"] = "RENT"
            elif current_node == "AWAIT_LOCATION":
                if "preferred_location" not in answers:
                    answers["preferred_location"] = message_text.strip()
            elif current_node == "AWAIT_BUDGET":
                if "budget_max" not in answers:
                    answers["budget_raw"] = message_text.strip()
            elif current_node == "AWAIT_TIMELINE":
                if "move_in_timeline" not in answers:
                    answers["move_in_timeline"] = message_text.strip()
            elif current_node == "AWAIT_PURPOSE":
                if "purpose" not in answers:
                    answers["purpose"] = message_text.strip()
            elif current_node == "AWAIT_SPECS":
                if "specs" not in answers:
                    answers["specs"] = message_text.strip()
            elif current_node == "AWAIT_ASKING_PRICE" and "asking_price" not in answers:
                answers["asking_price"] = message_text.strip()

        # Update Lead model scalar columns from answers
        self._sync_answers_to_lead(lead, answers)

        # Sync updated answers to active qualification proposal (preserving nulls for skipped fields)
        await self._sync_answers_to_proposal(lead, answers)

        # 2. Check for Rental Intent -> Politely Decline
        if answers.get("intent") == "RENT":
            return await self._handle_rental_declination(lead, lang, now)

        # 3. Determine next node and question
        next_node, question_text, is_complete = self._determine_next_step(answers, lang)

        state_data["current_node"] = next_node
        state_data["answers"] = answers
        state_data["turns_count"] = turns_count
        state_data["language"] = lang

        if is_complete:
            return await self._handle_qualification_completion(lead, answers, lang, now, sender_phone)

        # Intermediate turn: persist state and enqueue WhatsApp question
        lead.stage = LeadStage.QUALIFYING.value
        lead.last_activity_at = now
        score_out = ScoringEngine.evaluate(lead, answers)
        lead.intent_score = score_out.intent_score
        lead.lead_health = score_out.lead_health

        score_breakdown_payload = score_out.breakdown
        score_breakdown_payload["conversation_state"] = state_data
        lead.score_breakdown = score_breakdown_payload

        activity = LeadActivity(
            id=uuid.uuid4(),
            tenant_id=lead.tenant_id,
            lead_id=lead.id,
            actor_type="SYSTEM",
            activity_type="QUALIFICATION_STEP",
            description=f"Asked: {question_text[:100]}",
            metadata_={"node": next_node, "answers": answers},
            created_at=now,
        )
        self.db.add(activity)

        # Enqueue outbound WhatsApp reply
        outbox_svc = OutboxService(self.db)
        recipient_phone = self._resolve_recipient_phone(lead, sender_phone)
        if recipient_phone:
            await outbox_svc.enqueue_message(
                tenant_id=lead.tenant_id,
                event_type="WHATSAPP_OUTBOUND_DISPATCH",
                aggregate_type="LEAD",
                aggregate_id=lead.id,
                payload={"recipient_phone": recipient_phone, "message_text": question_text},
                idempotency_key=f"ai-reply-{lead.id}-{turns_count}",
            )

        await self.db.flush()

        return ConversationResponse(
            reply_text=question_text,
            lead_id=lead.id,
            stage=lead.stage,
            automation_status=lead.automation_status,
            is_completed=False,
        )


    def _determine_next_step(self, answers: dict[str, Any], lang: str) -> tuple[str, str, bool]:
        """
        Traverse the HAVN decision tree based on current answers.
        Returns (next_node, question_text, is_complete).
        """
        is_hinglish = lang == "HINGLISH"

        # Step 1: Intent (Buy vs Sell)
        if not answers.get("intent"):
            q = (
                "Welcome to HAVN Estates! Are you looking to BUY a luxury property or SELL/LIST your property?"
                if not is_hinglish
                else "HAVN Estates में आपका स्वागत है! क्या आप luxury property BUY करना चाहते हैं या SELL/LIST करना चाहते हैं?"
            )
            return "AWAIT_INTENT", q, False

        intent = answers["intent"]

        # ── BUYER BRANCH ──────────────────────────────────────────────────────
        if intent == "BUY":
            # 1. Property Type
            if not answers.get("property_type"):
                q = (
                    "Which type of property are you looking for?\n"
                    "1. House / Villa\n"
                    "2. Luxury Flat / Apartment\n"
                    "3. Penthouse\n"
                    "4. Residential / Commercial Land\n"
                    "5. Commercial Space"
                    if not is_hinglish
                    else "आप किस प्रकार की property तलाश रहे हैं?\n"
                         "1. House / Villa\n"
                         "2. Luxury Flat\n"
                         "3. Penthouse\n"
                         "4. Plot / Land\n"
                         "5. Commercial Space"
                )
                return "AWAIT_PROP_TYPE", q, False

            p_type = answers["property_type"]

            # 2. Property Specifics
            if p_type in ["VILLA", "HOUSE"] and not (answers.get("bhk") or answers.get("built_up_area") or answers.get("specs")):
                q = (
                    "What configuration (BHK) and approximate built-up area (sq ft / gaj) are you looking for?"
                    if not is_hinglish
                    else "आपको कितने BHK और लगभग कितना built-up area (sq ft / gaj) चाहिए?"
                )
                return "AWAIT_SPECS", q, False

            if p_type in ["FLAT", "APARTMENT"] and not (answers.get("bhk") or answers.get("specs")):
                q = (
                    "How many bedrooms (BHK) and preferred floor/view are you looking for in the flat/apartment?"
                    if not is_hinglish
                    else "Flat/Apartment में आपको कितने BHK और कौन सा floor preference रहेगा?"
                )
                return "AWAIT_SPECS", q, False


            if p_type == "PENTHOUSE" and not (answers.get("specs") or answers.get("terrace")):
                q = (
                    "What indoor area and terrace view preferences do you have for the penthouse?"
                    if not is_hinglish
                    else "Penthouse के लिए indoor area और terrace/view की क्या requirements हैं?"
                )
                return "AWAIT_SPECS", q, False

            if p_type == "PLOT" and not (answers.get("dimensions") or answers.get("size") or answers.get("specs")):
                q = (
                    "What plot dimensions or area (in gaj / sq yards) and road width are you seeking?"
                    if not is_hinglish
                    else "Plot का approximate size (gaj/sq yd) और road width क्या होनी चाहिए?"
                )
                return "AWAIT_SPECS", q, False

            if p_type == "COMMERCIAL" and not (answers.get("business_use") or answers.get("specs")):
                q = (
                    "What is your intended business use (retail, office, showroom) and required carpet area?"
                    if not is_hinglish
                    else "Commercial space का intended use (retail, office, showroom) और area क्या रहेगा?"
                )
                return "AWAIT_SPECS", q, False

            # 3. Preferred Location
            if not answers.get("preferred_location"):
                q = (
                    "Which specific location or localities in the city do you prefer (e.g. Rajpur Road, Balbir Road, Jakhan)?"
                    if not is_hinglish
                    else "आपकी preferred location या area कौन सा है (जैसे Rajpur Road, Balbir Road, Jakhan)?"
                )
                return "AWAIT_LOCATION", q, False

            # 4. Budget
            if not (answers.get("budget_max") or answers.get("budget_raw")):
                q = (
                    "What is your budget range (e.g. 1.5 Cr to 2.5 Cr) and flexibility?"
                    if not is_hinglish
                    else "आपका approximate budget range क्या है (जैसे 1.5 Cr से 2.5 Cr)?"
                )
                return "AWAIT_BUDGET", q, False

            # 5. Timeline & Purpose
            if not answers.get("move_in_timeline"):
                q = (
                    "What is your purchase timeline (e.g. Immediate, 1-3 months) and is this for primary residence, second home, or investment?"
                    if not is_hinglish
                    else "आप कब तक purchase complete करना चाहते हैं (Immediate, 1-3 months), और यह primary residence के लिए है या investment?"
                )
                return "AWAIT_TIMELINE", q, False

            # All core questions answered -> Complete!
            return "COMPLETED", "", True

        # ── SELLER BRANCH ──────────────────────────────────────────────────────
        elif intent == "SELL":
            if not answers.get("property_type"):
                q = (
                    "What type of property would you like to list with HAVN Estates (Flat, House/Villa, Plot, Commercial)?"
                    if not is_hinglish
                    else "आप HAVN Estates पर किस प्रकार की property list करना चाहते हैं (Flat, House/Villa, Plot, Commercial)?"
                )
                return "AWAIT_PROP_TYPE", q, False

            if not answers.get("preferred_location"):
                q = (
                    "Where is the property located? Please share the locality, address, or Google Maps pin link."
                    if not is_hinglish
                    else "Property की location क्या है? कृपया locality, address या Google Maps link share करें।"
                )
                return "AWAIT_LOCATION", q, False

            if not (answers.get("dimensions") or answers.get("size") or answers.get("bhk") or answers.get("specs")):
                q = (
                    "What are the specifications (configuration, dimensions, or total area in gaj/sq ft)?"
                    if not is_hinglish
                    else "Property के specifications (BHK, dimensions या area in gaj/sq ft) क्या हैं?"
                )
                return "AWAIT_SPECS", q, False

            if not (answers.get("asking_price") or answers.get("budget_max")):
                q = (
                    "What is your expected asking price for this property?"
                    if not is_hinglish
                    else "Property की expected asking price क्या है?"
                )
                return "AWAIT_ASKING_PRICE", q, False

            # Seller flow complete!
            return "COMPLETED", "", True

        return "COMPLETED", "", True

    async def _handle_human_takeover(
        self,
        lead: Lead,
        lang: str,
        message_text: str,
        now: datetime,
    ) -> ConversationResponse:
        """Immediately pause bot automation, notify assigned broker, and reply to client."""
        lead.automation_status = AutomationStatus.HUMAN_TAKEOVER.value
        lead.last_activity_at = now

        is_hinglish = lang == "HINGLISH"
        reply = (
            "Certainly! I have paused our automated assistant and connected you directly with our senior luxury property advisor. "
            "They will call you shortly."
            if not is_hinglish
            else "बिल्कुल! मैंने automated bot को pause कर दिया है और आपकी request हमारे senior luxury advisor को forward कर दी है। "
                 "वे शीघ्र ही आपसे संपर्क करेंगे।"
        )

        activity = LeadActivity(
            id=uuid.uuid4(),
            tenant_id=lead.tenant_id,
            lead_id=lead.id,
            actor_type="USER",
            activity_type="HUMAN_TAKEOVER_REQUESTED",
            description="Client requested live broker assistance",
            metadata_={"trigger_text": message_text},
            created_at=now,
        )
        self.db.add(activity)

        outbox_svc = OutboxService(self.db)
        await outbox_svc.enqueue_message(
            tenant_id=lead.tenant_id,
            event_type="BROKER_HANDOFF_NOTIFICATION",
            aggregate_type="LEAD",
            aggregate_id=lead.id,
            payload={
                "type": "IMMEDIATE_HUMAN_REQUEST",
                "message": f"Urgent: Client {lead.full_name} requested a human broker via WhatsApp.",
                "lead_id": str(lead.id),
                "client_message": message_text,
            },
            idempotency_key=f"handoff-urgent-{lead.id}-{int(now.timestamp())}",
        )

        await self.db.flush()

        return ConversationResponse(
            reply_text=reply,
            lead_id=lead.id,
            stage=lead.stage,
            automation_status=lead.automation_status,
            is_human_takeover=True,
        )

    async def _handle_rental_declination(
        self,
        lead: Lead,
        lang: str,
        now: datetime,
    ) -> ConversationResponse:
        """Politely decline rental/leasing inquiries and stop automation."""
        lead.stage = LeadStage.DISQUALIFIED.value
        lead.automation_status = AutomationStatus.PAUSED.value
        lead.last_activity_at = now

        is_hinglish = lang == "HINGLISH"
        reply = (
            "Thank you for contacting HAVN Estates. We deal exclusively in luxury property sales, purchases, "
            "and development investments. Currently, we do not handle rental or leasing inquiries. "
            "Should you wish to buy or sell a luxury property in the future, we would be delighted to assist!"
            if not is_hinglish
            else "HAVN Estates से संपर्क करने के लिए धन्यवाद। हम केवल luxury property sales, purchases और investments "
                 "में deal करते हैं, rentals या leasing नहीं लेते। "
                 "यदि भविष्य में आप property buy या sell करना चाहें, तो हम सहर्ष आपकी सेवा में उपस्थित होंगे!"
        )

        activity = LeadActivity(
            id=uuid.uuid4(),
            tenant_id=lead.tenant_id,
            lead_id=lead.id,
            actor_type="SYSTEM",
            activity_type="RENTAL_DECLINED",
            description="Rental/leasing enquiry politely declined per business policy",
            metadata_={},
            created_at=now,
        )
        self.db.add(activity)
        await self.db.flush()

        return ConversationResponse(
            reply_text=reply,
            lead_id=lead.id,
            stage=lead.stage,
            automation_status=lead.automation_status,
            is_completed=True,
        )

    async def _handle_opt_out(
        self,
        lead: Lead,
        lang: str,
        message_text: str,
        now: datetime,
    ) -> ConversationResponse:
        """Immediately stop automated communication, revoke consent, and record opt-out."""
        lead.do_not_contact = True
        lead.stage = LeadStage.DO_NOT_CONTACT.value
        lead.automation_status = AutomationStatus.OPTED_OUT.value
        lead.last_activity_at = now

        consent = ConsentRecord(
            id=uuid.uuid4(),
            tenant_id=lead.tenant_id,
            lead_id=lead.id,
            consent_type="WHATSAPP",
            granted=False,
            channel="WHATSAPP",
            consent_text=f"User opted out via conversation: '{message_text}'",
            created_at=now,
        )
        self.db.add(consent)

        activity = LeadActivity(
            id=uuid.uuid4(),
            tenant_id=lead.tenant_id,
            lead_id=lead.id,
            actor_type="USER",
            activity_type="CONSENT_REVOKED",
            description="WhatsApp consent revoked by user opt-out in conversation",
            metadata_={"trigger_text": message_text},
            created_at=now,
        )
        self.db.add(activity)

        is_hinglish = lang == "HINGLISH"
        reply = (
            "You have been successfully unsubscribed from automated messages. "
            "We will no longer contact you via WhatsApp. Have a great day!"
            if not is_hinglish
            else "आपको automated messages से unsubscribe कर दिया गया है। "
                 "हम आगे WhatsApp पर आपसे संपर्क नहीं करेंगे।"
        )

        outbox_svc = OutboxService(self.db)
        recipient_phone = self._resolve_recipient_phone(lead)
        if recipient_phone:
            await outbox_svc.enqueue_message(
                tenant_id=lead.tenant_id,
                event_type="WHATSAPP_OUTBOUND_DISPATCH",
                aggregate_type="LEAD",
                aggregate_id=lead.id,
                payload={"recipient_phone": recipient_phone, "message_text": reply},
                idempotency_key=f"ai-optout-{lead.id}",
            )

        await self.db.flush()

        return ConversationResponse(
            reply_text=reply,
            lead_id=lead.id,
            stage=lead.stage,
            automation_status=lead.automation_status,
            is_completed=True,
        )

    async def _handle_qualification_completion(
        self,
        lead: Lead,
        answers: dict[str, Any],
        lang: str,
        now: datetime,
        sender_phone: str | None = None,
    ) -> ConversationResponse:
        """
        Generate structured summary, compute deterministic score, notify broker,
        trigger Google Sheets sync, update proposal in PENDING_REVIEW, and hand off to human.
        Preserves the broker-review gate (stage stays QUALIFYING until broker reviews).
        """
        is_hinglish = lang == "HINGLISH"
        intent = answers.get("intent", "BUY")

        # 1. Format Summaries (Client Confirmation vs Internal Broker Briefing)
        if intent == "BUY":
            client_summary = (
                f"Thank you! Your requirements for a luxury {answers.get('property_type', 'Property')} "
                f"in {answers.get('preferred_location', 'the city')} have been verified and recorded. "
                "Our senior luxury real estate advisor will review this and share handpicked options shortly."
                if not is_hinglish
                else f"धन्यवाद! {answers.get('preferred_location', 'city')} में luxury {answers.get('property_type', 'Property')} "
                     "की आपकी requirements record कर ली गई हैं। "
                     "हमारे senior advisor जल्द ही आपके लिए curated options लेकर संपर्क करेंगे।"
            )
            broker_summary = (
                f"HAVN BUYER QUALIFICATION SUMMARY:\n"
                f"• Client: {lead.full_name}\n"
                f"• Type: {answers.get('property_type')}\n"
                f"• Specs/BHK: {answers.get('bhk') or answers.get('specs')}\n"
                f"• Location: {answers.get('preferred_location')}\n"
                f"• Budget: {answers.get('budget_display') or answers.get('budget_raw') or answers.get('budget_max')}\n"
                f"• Timeline: {answers.get('move_in_timeline')}\n"
                f"• Purpose: {answers.get('purpose')}\n"
                f"• Parking: {answers.get('parking', 'Not specified')}"
            )
        else:
            client_summary = (
                f"Thank you for listing your {answers.get('property_type', 'property')} with HAVN Estates. "
                "Our valuation executive will contact you to verify details and arrange an on-site visit."
                if not is_hinglish
                else f"HAVN Estates पर अपनी {answers.get('property_type', 'property')} list करने के लिए धन्यवाद। "
                     "हमारे valuation executive details verify करने के लिए जल्द ही आपसे संपर्क करेंगे।"
            )
            broker_summary = (
                f"HAVN SELLER LISTING SUMMARY:\n"
                f"• Owner: {lead.full_name}\n"
                f"• Property Type: {answers.get('property_type')}\n"
                f"• Location: {answers.get('preferred_location')}\n"
                f"• Dimensions/Size: {answers.get('dimensions') or answers.get('size')}\n"
                f"• Asking Price: {answers.get('asking_price')}\n"
                f"• Verification: UNVERIFIED (Owner Provided)"
            )

        # 2. Score Lead and maintain QUALIFYING stage (Broker Review Gate)
        score_out = ScoringEngine.evaluate(lead, answers)
        lead.stage = LeadStage.QUALIFYING.value
        lead.intent_score = score_out.intent_score
        lead.lead_health = score_out.lead_health
        lead.notes = broker_summary
        lead.last_activity_at = now

        # 3. Stop Bot Automation (Handoff to Human)
        lead.automation_status = AutomationStatus.HUMAN_TAKEOVER.value

        score_breakdown_payload = score_out.breakdown
        score_breakdown_payload["summary"] = broker_summary
        score_breakdown_payload["answers"] = answers
        score_breakdown_payload["conversation_state"] = {
            "current_node": "COMPLETED",
            "answers": answers,
            "language": lang,
        }
        lead.score_breakdown = score_breakdown_payload

        # Sync final answers to active proposal in PENDING_REVIEW status
        await self._sync_answers_to_proposal(lead, answers)

        # 4. Activity Record
        activity = LeadActivity(
            id=uuid.uuid4(),
            tenant_id=lead.tenant_id,
            lead_id=lead.id,
            actor_type="SYSTEM",
            activity_type="QUALIFICATION_COMPLETED",
            description=f"Qualification completed. Score: {score_out.intent_score} ({score_out.lead_health})",
            metadata_={"summary": broker_summary, "score": score_out.intent_score, "health": score_out.lead_health},
            created_at=now,
        )
        self.db.add(activity)

        # 5. Outbox: Broker Notification Event
        outbox_svc = OutboxService(self.db)
        await outbox_svc.enqueue_message(
            tenant_id=lead.tenant_id,
            event_type="BROKER_HANDOFF_NOTIFICATION",
            aggregate_type="LEAD",
            aggregate_id=lead.id,
            payload={
                "lead_id": str(lead.id),
                "lead_name": lead.full_name,
                "score": score_out.intent_score,
                "health": score_out.lead_health,
                "summary": broker_summary,
                "answers": answers,
            },
            idempotency_key=f"broker-notify-{lead.id}",
        )

        # 6. Outbox: Google Sheets Synchronization Event
        await outbox_svc.enqueue_message(
            tenant_id=lead.tenant_id,
            event_type="SHEETS_SYNC_REQUESTED",
            aggregate_type="LEAD",
            aggregate_id=lead.id,
            payload={
                "lead_id": str(lead.id),
                "intent": intent,
                "answers": answers,
                "summary": broker_summary,
                "score": score_out.intent_score,
                "health": score_out.lead_health,
            },
            idempotency_key=f"sheets-sync-{lead.id}",
        )

        # 7. Outbox: Final WhatsApp Confirmation to Client
        recipient_phone = self._resolve_recipient_phone(lead, sender_phone)
        if recipient_phone:
            await outbox_svc.enqueue_message(
                tenant_id=lead.tenant_id,
                event_type="WHATSAPP_OUTBOUND_DISPATCH",
                aggregate_type="LEAD",
                aggregate_id=lead.id,
                payload={"recipient_phone": recipient_phone, "message_text": client_summary},
                idempotency_key=f"ai-reply-final-{lead.id}",
            )

        # Note: MATCH_COMPUTATION_REQUESTED is NOT triggered here.
        # It is strictly reserved for when a human broker reviews and accepts the proposal.

        await self.db.flush()

        logger.info(
            "qualification_completed_and_handed_off",
            lead_id=str(lead.id),
            score=score_out.intent_score,
            health=score_out.lead_health,
        )

        return ConversationResponse(
            reply_text=client_summary,
            lead_id=lead.id,
            stage=lead.stage,
            automation_status=lead.automation_status,
            is_completed=True,
            summary=broker_summary,
        )

    def _sync_answers_to_lead(self, lead: Lead, answers: dict[str, Any]) -> None:
        """Map extracted answers to canonical scalar columns on Lead model."""
        if answers.get("property_type") and answers["property_type"] != "SKIPPED":
            lead.property_type = answers["property_type"]
        if answers.get("bhk") and answers["bhk"] != "SKIPPED":
            with contextlib.suppress(ValueError, TypeError):
                lead.bhk = int(answers["bhk"])
        if answers.get("budget_max") and answers["budget_max"] != "SKIPPED":
            with contextlib.suppress(ValueError, TypeError):
                lead.budget_max = int(answers["budget_max"])
        if answers.get("budget_min") and answers["budget_min"] != "SKIPPED":
            with contextlib.suppress(ValueError, TypeError):
                lead.budget_min = int(answers["budget_min"])
        if answers.get("preferred_location") and answers["preferred_location"] != "SKIPPED":
            lead.preferred_location = answers["preferred_location"]
        if answers.get("purpose") and answers["purpose"] != "SKIPPED":
            lead.purpose = answers["purpose"]
        if answers.get("move_in_timeline") and answers["move_in_timeline"] != "SKIPPED":
            lead.move_in_timeline = answers["move_in_timeline"]

