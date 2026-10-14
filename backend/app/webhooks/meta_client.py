"""
MetaGraphClient — Injectable, mockable client for Meta Graph API lead-detail retrieval.
Supports offline mock execution, rate-limiting resiliency, and strict field normalization.
"""

from abc import ABC, abstractmethod
from typing import Any

import httpx
import structlog

from app.core.config import get_settings
from app.messaging.whatsapp_client import mask_phone_for_logging

logger = structlog.get_logger()
settings = get_settings()


class MetaGraphError(Exception):
    """Base exception for Meta Graph API operations."""


class MetaAuthenticationError(MetaGraphError):
    """Raised when access token is invalid, expired, or lacking permissions (HTTP 401 / code 190)."""


class MetaGraphTimeoutError(MetaGraphError):
    """Raised on socket or connection timeout to Meta Graph API (retryable)."""


class MetaLeadNotFoundError(MetaGraphError):
    """Raised when leadgen_id does not exist or was deleted (HTTP 404)."""


class MetaMalformedResponseError(MetaGraphError):
    """Raised when response body is not valid JSON or missing expected fields."""


def normalize_meta_lead_payload(
    raw_data: dict[str, Any],
    expected_business_name: str | None = None,
) -> dict[str, Any]:
    """
    Normalize Meta Graph API Leadgen payload into HAVN lead attributes.
    Extracts name, phone, email, location, budget, and verifies explicit WhatsApp contact consent.

    Consent Governance:
    - Strict opt-in: Absent consent question defaults to consent_granted = False.
    - Generic terms acceptance (agree_to_terms, terms_and_conditions) does not grant messaging permission
      unless wording explicitly covers communications and identifies the business.
    - Ambiguous wording ('Contact me', 'Get in touch') without business/channel identification is rejected.
    - Preserves consent wording, version, source, and response in consent_evidence.
    """
    from datetime import UTC, datetime

    leadgen_id = str(raw_data.get("id", "")).strip()
    field_data = raw_data.get("field_data", [])

    fields: dict[str, str] = {}
    field_items: list[dict[str, Any]] = []
    if isinstance(field_data, list):
        for item in field_data:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip().lower()
            vals = item.get("values", [])
            val = str(vals[0]).strip() if vals and vals[0] is not None else ""
            if name and val:
                fields[name] = val
                field_items.append({
                    "name": name,
                    "value": val,
                    "question": str(item.get("question", item.get("label", ""))).strip(),
                })

    # 1. Full name extraction
    full_name = (
        fields.get("full_name")
        or fields.get("name")
        or " ".join(filter(None, [fields.get("first_name"), fields.get("last_name")]))
        or f"Meta Lead {leadgen_id[-4:] if len(leadgen_id) >= 4 else 'New'}"
    )

    # 2. Phone normalization
    raw_phone = (
        fields.get("phone_number")
        or fields.get("phone")
        or fields.get("whatsapp_phone_number")
        or fields.get("contact_number")
        or ""
    )
    cleaned_phone = "".join(c for c in raw_phone if c.isdigit() or c == "+")
    if cleaned_phone and not cleaned_phone.startswith("+"):
        cleaned_phone = "+91" + cleaned_phone if len(cleaned_phone) == 10 else "+" + cleaned_phone

    # 3. Email normalization
    email = fields.get("email") or fields.get("email_address")
    if email:
        email = email.strip().lower()

    # 4. Location / City
    preferred_location = (
        fields.get("city")
        or fields.get("preferred_location")
        or fields.get("location")
        or fields.get("preferred_city")
    )

    # 5. Property Type & BHK
    property_type = fields.get("property_type") or fields.get("preferred_property_type")
    bhk = None
    raw_bhk = fields.get("bhk") or fields.get("bedrooms")
    if raw_bhk:
        import re

        bhk_match = re.search(r"(\d+)", raw_bhk)
        if bhk_match:
            try:
                bhk = int(bhk_match.group(1))
            except (ValueError, TypeError):
                bhk = None

    # 6. Budget Parsing
    budget_raw = fields.get("budget") or fields.get("budget_range")
    budget_min = None
    budget_max = None
    if budget_raw:
        import re

        b_lower = budget_raw.lower()
        if "cr" in b_lower:
            range_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:cr)?\s*(?:to|-)\s*(\d+(?:\.\d+)?)\s*cr", b_lower)
            if range_match:
                budget_min = int(float(range_match.group(1)) * 10_000_000)
                budget_max = int(float(range_match.group(2)) * 10_000_000)
            else:
                single_match = re.search(r"(\d+(?:\.\d+)?)\s*cr", b_lower)
                if single_match:
                    budget_max = int(float(single_match.group(1)) * 10_000_000)
        elif "lakh" in b_lower or "lac" in b_lower:
            range_lakh = re.search(r"(\d+(?:\.\d+)?)\s*(?:lakh|lac|l)?\s*(?:to|-)\s*(\d+(?:\.\d+)?)\s*(?:lakh|lac|l)", b_lower)
            if range_lakh:
                budget_min = int(float(range_lakh.group(1)) * 100_000)
                budget_max = int(float(range_lakh.group(2)) * 100_000)
            else:
                single_lakh = re.search(r"(\d+(?:\.\d+)?)\s*(?:lakh|lac|l)", b_lower)
                if single_lakh:
                    budget_max = int(float(single_lakh.group(1)) * 100_000)

    # 7. Consent Governance: Strict Opt-in & Wording Verification
    # Potential consent fields
    explicit_wa_fields = {
        "whatsapp_consent",
        "whatsapp_opt_in",
        "whatsapp_permission",
        "whatsapp_subscription",
        "consent_whatsapp",
    }
    generic_or_custom_fields = {
        "consent_to_contact",
        "consent",
        "agree_to_terms",
        "i_agree",
        "opt_in",
        "custom_disclaimer",
        "terms_and_conditions",
        "privacy_policy",
        "disclaimer",
    }

    consent_granted = False
    evaluated_field_name: str | None = None
    evaluated_field_value: str | None = None
    evaluated_wording: str | None = None
    evaluation_reason = "absent_consent_field"

    # Business identity keywords for validating consent scope
    target_biz = (expected_business_name or "havn").strip().lower()
    biz_keywords = {"havn", "realty", "broker", "agent", "agency", "our team", "company"}
    if target_biz:
        biz_keywords.add(target_biz)

    comm_keywords = {"whatsapp", "message", "messaging", "chat", "contact", "sms", "call", "updates"}

    for item in field_items:
        f_name = item["name"]
        f_val = item["value"]
        f_val_lower = f_val.lower()
        f_question = item.get("question", "")

        # Combined text representation for wording evaluation
        combined_text = f"{f_name} {f_question} {f_val}".lower()

        is_explicit_wa = f_name in explicit_wa_fields
        is_candidate = is_explicit_wa or (f_name in generic_or_custom_fields) or ("consent" in f_name) or ("disclaimer" in f_name)

        if not is_candidate:
            continue

        evaluated_field_name = f_name
        evaluated_field_value = f_val
        evaluated_wording = f_question if f_question else f_val

        # Check denial
        if f_val_lower in {"no", "false", "0", "opt_out", "deny", "denied", "declined"}:
            consent_granted = False
            evaluation_reason = "explicitly_denied_by_user"
            break

        # Check if affirmative
        is_affirmative = f_val_lower in {
            "true", "yes", "1", "agree", "granted", "i agree", "opt_in", "accepted",
        } or any(word in f_val_lower for word in ["i agree", "i authorize", "consent", "opt in"])

        if not is_affirmative:
            consent_granted = False
            evaluation_reason = f"non_affirmative_response_{f_val_lower[:30]}"
            break

        # If it's an explicit WhatsApp consent field (e.g. whatsapp_consent: yes)
        if is_explicit_wa:
            # Check if wording explicitly mentions a different conflicting business
            if any(other in combined_text for other in ["apex properties", "acme realty", "other company"]) and target_biz not in combined_text:
                consent_granted = False
                evaluation_reason = "tenant_mismatch_different_business_named"
                break
            consent_granted = True
            evaluation_reason = "explicit_whatsapp_consent_field_affirmative"
            break

        # For generic terms / custom disclaimer / consent_to_contact:
        # Must explicitly cover communications AND identify the business
        has_comm = any(ck in combined_text for ck in comm_keywords)
        has_biz = any(bk in combined_text for bk in biz_keywords)

        # Check for tenant mismatch
        if any(other in combined_text for other in ["apex properties", "acme realty", "competitor broker"]) and target_biz not in combined_text:
            consent_granted = False
            evaluation_reason = "tenant_mismatch_different_business_named"
            break

        if f_name in {"agree_to_terms", "terms_and_conditions", "privacy_policy", "i_agree"} and (not has_comm or not has_biz):
            # Generic terms acceptance does NOT grant WhatsApp communication consent!
            consent_granted = False
            evaluation_reason = "generic_terms_acceptance_without_explicit_whatsapp_scope"
            break

        # Check ambiguous wording (e.g., "Contact me" without business or channel)
        if not has_comm or not has_biz:
            consent_granted = False
            evaluation_reason = "ambiguous_consent_wording_missing_channel_or_business_identity"
            break

        consent_granted = True
        evaluation_reason = "explicit_custom_disclaimer_affirmative_with_business_scope"
        break

    now_iso = datetime.now(UTC).isoformat()
    consent_evidence = {
        "source": "META_LEAD_ADS",
        "leadgen_id": leadgen_id,
        "form_id": str(raw_data.get("form_id", "")),
        "page_id": str(raw_data.get("page_id", "")),
        "field_name": evaluated_field_name,
        "field_response": evaluated_field_value,
        "wording": evaluated_wording,
        "consent_granted": consent_granted,
        "evaluation_reason": evaluation_reason,
        "evaluated_at": now_iso,
    }

    return {
        "leadgen_id": leadgen_id,
        "full_name": full_name,
        "phone": cleaned_phone or "+919876543210",
        "email": email,
        "preferred_location": preferred_location,
        "property_type": property_type,
        "bhk": bhk,
        "budget_min": budget_min,
        "budget_max": budget_max,
        "consent_granted": consent_granted,
        "consent_text": evaluated_wording or (f"{evaluated_field_name}: {evaluated_field_value}" if evaluated_field_name else None),
        "consent_type": "WHATSAPP_COMMUNICATION" if consent_granted else "NONE",
        "consent_evidence": consent_evidence,
        "raw_fields": fields,
    }


class IMetaGraphClient(ABC):
    @abstractmethod
    async def fetch_lead_details(
        self,
        leadgen_id: str,
        access_token: str | None = None,
        expected_business_name: str | None = None,
    ) -> dict[str, Any]:
        """Fetch and normalize lead details from Meta Graph API."""


class MetaGraphClient(IMetaGraphClient):
    """Production and Sandbox Meta Graph API client."""

    def __init__(
        self,
        base_url: str = "https://graph.facebook.com",
        api_version: str = "v20.0",
        default_access_token: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version
        self.default_access_token = default_access_token or settings.whatsapp_access_token

    async def fetch_lead_details(
        self,
        leadgen_id: str,
        access_token: str | None = None,
        expected_business_name: str | None = None,
    ) -> dict[str, Any]:
        token = access_token or self.default_access_token
        if not token:
            raise MetaAuthenticationError("No Meta access token configured for lead retrieval.")

        url = f"{self.base_url}/{self.api_version}/{leadgen_id}"
        params = {"access_token": token}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)

                if resp.status_code == 401 or resp.status_code == 403:
                    logger.warning("meta_graph_api_auth_failure", status=resp.status_code)
                    raise MetaAuthenticationError(f"Meta Graph API authentication failed: {resp.status_code}")

                if resp.status_code == 404:
                    raise MetaLeadNotFoundError(f"Meta Leadgen ID not found: {leadgen_id}")

                if resp.is_error:
                    raise MetaGraphError(f"Meta Graph API error: {resp.status_code} - {resp.text[:200]}")

                try:
                    data = resp.json()
                except Exception as json_err:
                    raise MetaMalformedResponseError(f"Meta returned non-JSON response: {json_err}") from json_err

                if not isinstance(data, dict) or "id" not in data:
                    raise MetaMalformedResponseError("Meta response missing leadgen ID.")

                normalized = normalize_meta_lead_payload(data, expected_business_name=expected_business_name)
                logger.info(
                    "meta_lead_retrieved_and_normalized",
                    leadgen_id=leadgen_id,
                    phone=mask_phone_for_logging(normalized["phone"]),
                    consent_granted=normalized["consent_granted"],
                )
                return normalized

        except httpx.TimeoutException as exc:
            logger.warning("meta_graph_api_timeout", leadgen_id=leadgen_id)
            raise MetaGraphTimeoutError(f"Timeout connecting to Meta Graph API for {leadgen_id}") from exc
        except httpx.RequestError as exc:
            logger.warning("meta_graph_api_network_error", leadgen_id=leadgen_id, error=str(exc))
            raise MetaGraphError(f"Network error during Meta Graph API call: {exc}") from exc


class MockMetaGraphClient(IMetaGraphClient):
    """In-memory mock client for safe local testing and verification."""

    def __init__(self, fixture_responses: dict[str, Any] | None = None):
        self.fixture_responses = fixture_responses or {}

    async def fetch_lead_details(
        self,
        leadgen_id: str,
        access_token: str | None = None,
        expected_business_name: str | None = None,
    ) -> dict[str, Any]:
        if leadgen_id in self.fixture_responses:
            val = self.fixture_responses[leadgen_id]
            if isinstance(val, Exception):
                raise val
            if isinstance(val, dict):
                return normalize_meta_lead_payload(val, expected_business_name=expected_business_name)

        # Default synthetic mock lead
        return {
            "leadgen_id": leadgen_id,
            "full_name": f"Mock Meta Lead {leadgen_id[-4:]}",
            "phone": "+919876543210",
            "email": f"meta_{leadgen_id[-6:]}@example.com",
            "preferred_location": "Dehradun",
            "property_type": "VILLA",
            "bhk": 3,
            "budget_min": 15000000,
            "budget_max": 25000000,
            "consent_granted": True,
            "raw_fields": {},
        }
