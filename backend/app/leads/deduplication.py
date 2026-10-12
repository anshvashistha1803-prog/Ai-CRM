"""
Phone number deduplication utilities.
Normalization ensures consistent storage regardless of how the user typed it.
"""

import re

import phonenumbers
from phonenumbers import NumberParseException

from app.core.exceptions import ValidationError


def normalize_phone(raw: str, default_region: str = "IN") -> str:
    """
    Normalize a phone number to E.164 format.

    Args:
        raw: Raw phone string from user input (any format).
        default_region: ISO 3166-1 alpha-2 country code for numbers without IDD prefix.
                        Defaults to 'IN' (India) — configurable per tenant in future.

    Returns:
        E.164 formatted phone string (e.g. "+919876543210").

    Raises:
        ValidationError: If the number is invalid or unparseable.
    """
    if not raw:
        raise ValidationError("Phone number cannot be empty.")

    # Strip common noise
    cleaned = re.sub(r"[\s\-\(\)\.]+", "", raw.strip())

    try:
        parsed = phonenumbers.parse(cleaned, default_region)
    except NumberParseException as exc:
        raise ValidationError(
            f"Invalid phone number: {raw!r}",
            detail=str(exc),
        ) from exc

    if not phonenumbers.is_valid_number(parsed):
        raise ValidationError(f"Phone number is not valid: {raw!r}")

    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_email(raw: str) -> str:
    """Normalize email to lowercase + stripped. Does not validate format (use Pydantic for that)."""
    return raw.strip().lower()


# ── Deduplication query helpers ───────────────────────────────────────────────
# Actual DB lookup is in LeadService to keep DB concerns out of this module.

def dedup_candidates(phone_e164: str | None, email: str | None) -> dict:
    """
    Return a dict of normalized deduplication keys for use in DB queries.
    At least one of phone or email must be provided.
    """
    if not phone_e164 and not email:
        raise ValidationError("At least one of phone or email is required for deduplication.")
    return {
        "phone_e164": phone_e164,
        "email_normalized": normalize_email(email) if email else None,
    }
