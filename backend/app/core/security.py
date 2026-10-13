"""
Security utilities: JWT encode/decode (RS256), password hashing (bcrypt),
and field-level encryption (AES-256-GCM) for PII columns.
"""

import base64
import hashlib
import hmac
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import bcrypt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from jose import jwt

from app.core.config import get_settings

settings = get_settings()

# ── Password hashing (bcrypt) ─────────────────────────────────────────────────
def hash_password(plain: str) -> str:
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(plain.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


# ── JWT (RS256) ───────────────────────────────────────────────────────────────
def _find_key_path(configured_path: str) -> Path:
    p = Path(configured_path)
    if p.exists():
        return p
    # Look under backend/
    backend_p = Path("backend") / configured_path.lstrip("./")
    if backend_p.exists():
        return backend_p
    # Look relative to project root from this file
    root_keys_p = Path(__file__).resolve().parent.parent.parent / "keys" / p.name
    if root_keys_p.exists():
        return root_keys_p
    return p


def _load_private_key() -> str:
    path = _find_key_path(settings.jwt_private_key_path)
    if not path.exists():
        raise FileNotFoundError(
            f"JWT private key not found at {path}. "
            "Run: python scripts/generate_keys.py"
        )
    return path.read_text(encoding="utf-8")


def _load_public_key() -> str:
    path = _find_key_path(settings.jwt_public_key_path)
    if not path.exists():
        raise FileNotFoundError(
            f"JWT public key not found at {path}. "
            "Run: python scripts/generate_keys.py"
        )
    return path.read_text(encoding="utf-8")


def create_access_token(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    role: str,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Issue a short-lived access token (RS256)."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "role": role,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_token_expire_minutes),
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, _load_private_key(), algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: uuid.UUID, tenant_id: uuid.UUID) -> tuple[str, str]:
    """
    Issue a refresh token. Returns (token_string, jti).
    jti should be stored (hashed) in the DB for revocation.
    """
    jti = str(uuid.uuid4())
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "jti": jti,
        "iat": now,
        "exp": now + timedelta(days=settings.jwt_refresh_token_expire_days),
        "type": "refresh",
    }
    token = jwt.encode(payload, _load_private_key(), algorithm=settings.jwt_algorithm)
    return token, jti


def create_invitation_token(
    email: str,
    tenant_id: uuid.UUID,
    role: str,
    invited_by: uuid.UUID,
    expires_days: int = 7,
) -> str:
    """Issue a signed invitation token for new agency staff."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": email.strip().lower(),
        "tenant_id": str(tenant_id),
        "role": role,
        "invited_by": str(invited_by),
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": now + timedelta(days=expires_days),
        "type": "invitation",
    }
    return jwt.encode(payload, _load_private_key(), algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """
    Decode and verify a JWT. Raises JWTError on invalid/expired tokens.
    Callers should catch JWTError and raise HTTPException 401.
    """
    return jwt.decode(token, _load_public_key(), algorithms=[settings.jwt_algorithm])


# ── Field-level encryption (AES-256-GCM) for PII ─────────────────────────────
def _get_aes_key() -> bytes:
    """Derive a 32-byte AES key from the app encryption key."""
    raw = settings.app_encryption_key.encode()
    return hashlib.sha256(raw).digest()  # always 32 bytes


def encrypt_field(plaintext: str | None) -> str | None:
    """Encrypt a string field. Returns base64-encoded ciphertext or None."""
    if plaintext is None or plaintext == "":
        return plaintext
    key = _get_aes_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)  # 96-bit nonce for GCM
    ct = aesgcm.encrypt(nonce, plaintext.encode(), None)
    # Store nonce + ciphertext together, base64 encoded
    return base64.b64encode(nonce + ct).decode()


def decrypt_field(ciphertext: str | None) -> str | None:
    """Decrypt a field encrypted with encrypt_field(). Returns plaintext or None."""
    if ciphertext is None or ciphertext == "":
        return ciphertext
    key = _get_aes_key()
    aesgcm = AESGCM(key)
    raw = base64.b64decode(ciphertext)
    nonce, ct = raw[:12], raw[12:]
    return aesgcm.decrypt(nonce, ct, None).decode()


# ── Blind indexing (HMAC-SHA256) for exact-match search over encrypted PII ──
def compute_blind_hash(value: str | None) -> str | None:
    """
    Compute a deterministic HMAC-SHA256 blind index for exact-match searching
    over encrypted PII without compromising AES-GCM ciphertext randomness.
    """
    if not value:
        return None
    return hmac.new(
        settings.app_encryption_key.encode("utf-8"),
        value.strip().lower().encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# ── HMAC verification (WhatsApp webhook) ──────────────────────────────────────
def verify_whatsapp_signature(payload_bytes: bytes, signature_header: str | None) -> bool:
    """
    Verify X-Hub-Signature-256 header from Meta.
    Uses constant-time comparison to prevent timing attacks.
    Fails closed if secret is not configured or header is invalid.
    """
    if not settings.whatsapp_app_secret:
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = signature_header[len("sha256="):]
    actual = hmac.new(
        settings.whatsapp_app_secret.encode(),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, actual)
