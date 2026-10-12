"""
Unit tests for core/security.py — password hashing, AES-256-GCM encryption,
and RSA-signed JWT issuance/verification.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError

from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    decrypt_field,
    encrypt_field,
    hash_password,
    verify_password,
)


class TestPasswordHashing:
    def test_hash_and_verify_success(self):
        plain = "SuperSecret123!"
        hashed = hash_password(plain)
        assert hashed != plain
        assert verify_password(plain, hashed) is True

    def test_verify_failure_wrong_password(self):
        plain = "SuperSecret123!"
        hashed = hash_password(plain)
        assert verify_password("WrongPassword123!", hashed) is False

    def test_different_hashes_for_same_password(self):
        # Bcrypt uses a random salt each time
        plain = "SuperSecret123!"
        h1 = hash_password(plain)
        h2 = hash_password(plain)
        assert h1 != h2
        assert verify_password(plain, h1) is True
        assert verify_password(plain, h2) is True


class TestFieldEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        secret = "+919876543210"
        encrypted = encrypt_field(secret)
        assert encrypted != secret
        decrypted = decrypt_field(encrypted)
        assert decrypted == secret

    def test_none_input_returns_none(self):
        assert encrypt_field(None) is None
        assert decrypt_field(None) is None

    def test_empty_string_returns_empty_string(self):
        assert encrypt_field("") == ""
        assert decrypt_field("") == ""

    def test_different_iv_produces_different_ciphertext(self):
        secret = "buyer@example.com"
        enc1 = encrypt_field(secret)
        enc2 = encrypt_field(secret)
        assert enc1 != enc2
        assert decrypt_field(enc1) == secret
        assert decrypt_field(enc2) == secret

    def test_decrypt_invalid_ciphertext_raises_error(self):
        with pytest.raises((ValueError, Exception)):  # noqa: B017
            decrypt_field("not-valid-base64-or-aes-gcm")


class TestJWTTokens:
    def test_create_and_decode_token(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        role = "AGENT"

        token = create_access_token(user_id=user_id, tenant_id=tenant_id, role=role)
        assert isinstance(token, str)

        payload = decode_token(token)
        assert payload["sub"] == str(user_id)
        assert payload["tenant_id"] == str(tenant_id)
        assert payload["role"] == role
        assert "exp" in payload
        assert "jti" in payload
        assert payload["type"] == "access"

    def test_create_and_decode_refresh_token(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()

        token, jti = create_refresh_token(user_id=user_id, tenant_id=tenant_id)
        assert isinstance(token, str)
        assert isinstance(jti, str)

        payload = decode_token(token)
        assert payload["sub"] == str(user_id)
        assert payload["tenant_id"] == str(tenant_id)
        assert payload["jti"] == jti
        assert payload["type"] == "refresh"

    def test_token_with_extra_claims(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        extra = {"department": "sales", "custom_flag": True}

        token = create_access_token(
            user_id=user_id,
            tenant_id=tenant_id,
            role="MANAGER",
            extra_claims=extra,
        )
        payload = decode_token(token)
        assert payload["department"] == "sales"
        assert payload["custom_flag"] is True

    def test_tampered_token_raises_invalid_token_error(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        token = create_access_token(user_id=user_id, tenant_id=tenant_id, role="AGENT")

        # Corrupt the token signature
        tampered = token[:-6] + "xxxxxx"
        with pytest.raises(JWTError):
            decode_token(tampered)

    def test_expired_token_raises_token_expired_error(self):
        from app.core.config import get_settings
        from app.core.security import _load_private_key

        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        settings = get_settings()

        # Generate an expired token payload
        now = datetime.now(UTC)
        payload = {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "role": "AGENT",
            "iat": int((now - timedelta(hours=2)).timestamp()),
            "exp": int((now - timedelta(hours=1)).timestamp()),
            "jti": uuid.uuid4().hex,
            "type": "access",
        }
        private_key = _load_private_key()
        expired_token = jwt.encode(payload, private_key, algorithm=settings.jwt_algorithm)

        with pytest.raises(ExpiredSignatureError):
            decode_token(expired_token)
