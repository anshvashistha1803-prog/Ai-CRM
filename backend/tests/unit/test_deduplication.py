"""
Unit tests for phone number normalization and deduplication utilities.
Pure logic — no DB required.
"""

import pytest

from app.core.exceptions import ValidationError
from app.leads.deduplication import dedup_candidates, normalize_email, normalize_phone


class TestNormalizePhone:
    """Phone normalization to E.164 format."""

    def test_indian_number_without_country_code(self):
        assert normalize_phone("9876543210") == "+919876543210"

    def test_indian_number_with_plus(self):
        assert normalize_phone("+919876543210") == "+919876543210"

    def test_number_with_spaces(self):
        assert normalize_phone("98765 43210") == "+919876543210"

    def test_number_with_dashes(self):
        assert normalize_phone("98765-43210") == "+919876543210"

    def test_number_with_parentheses(self):
        assert normalize_phone("(+91) 98765-43210") == "+919876543210"

    def test_number_with_country_code_00(self):
        assert normalize_phone("00919876543210") == "+919876543210"

    def test_invalid_too_short(self):
        with pytest.raises(ValidationError):
            normalize_phone("12345")

    def test_invalid_letters(self):
        with pytest.raises(ValidationError):
            normalize_phone("not-a-number")

    def test_empty_string(self):
        with pytest.raises(ValidationError):
            normalize_phone("")

    def test_us_number(self):
        result = normalize_phone("+12025550123")
        assert result == "+12025550123"

    def test_idempotent(self):
        """Normalizing an already-normalized number returns the same result."""
        normalized = normalize_phone("+919876543210")
        assert normalize_phone(normalized) == normalized


class TestNormalizeEmail:
    def test_lowercases(self):
        assert normalize_email("USER@EXAMPLE.COM") == "user@example.com"

    def test_strips_whitespace(self):
        assert normalize_email("  user@example.com  ") == "user@example.com"

    def test_mixed_case_and_spaces(self):
        assert normalize_email("  User@Example.COM  ") == "user@example.com"


class TestDedupCandidates:
    def test_phone_only(self):
        result = dedup_candidates(phone_e164="+919876543210", email=None)
        assert result["phone_e164"] == "+919876543210"
        assert result["email_normalized"] is None

    def test_email_only(self):
        result = dedup_candidates(phone_e164=None, email="User@Example.com")
        assert result["email_normalized"] == "user@example.com"
        assert result["phone_e164"] is None

    def test_both(self):
        result = dedup_candidates("+919876543210", "user@example.com")
        assert result["phone_e164"] == "+919876543210"
        assert result["email_normalized"] == "user@example.com"

    def test_neither_raises(self):
        with pytest.raises(ValidationError):
            dedup_candidates(phone_e164=None, email=None)
