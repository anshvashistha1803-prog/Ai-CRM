"""
Google OAuth 2.0 helper utilities:
- Cryptographic state token generation & validation (HMAC-SHA256)
- Authorization URL construction
- Authorization-code exchange
- Refresh token rotation
- Token revocation
"""

import base64
import contextlib
import hashlib
import hmac
import json
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from app.core.config import get_settings

logger = structlog.get_logger()
settings = get_settings()

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


class OAuthError(Exception):
    """Base exception for Google OAuth errors."""


class OAuthStateError(OAuthError):
    """Raised when an OAuth state parameter is invalid or expired."""


class OAuthConfigurationError(OAuthError):
    """Raised when required OAuth credentials or settings are missing."""


class OAuthTokenExchangeError(OAuthError):
    """Raised when Google authorization code exchange or refresh fails."""


class OAuthRevokedError(OAuthError):
    """Raised when Google credentials have been revoked by the user or expired."""


def _get_state_secret() -> bytes:
    raw = settings.google_oauth_state_secret or settings.app_encryption_key or settings.secret_key
    if not raw:
        raise OAuthConfigurationError("Missing encryption key or OAuth state secret.")
    return raw.encode("utf-8")


def generate_oauth_state(
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    ttl_seconds: int = 900,
) -> str:
    """
    Generate a tamper-proof, timestamped OAuth state parameter.
    Format: base64(json_payload).hmac_sha256_hex
    """
    secret = _get_state_secret()
    payload = {
        "tenant_id": str(tenant_id),
        "user_id": str(user_id),
        "exp": int(time.time()) + ttl_seconds,
        "nonce": uuid.uuid4().hex,
    }
    payload_json = json.dumps(payload, separators=(",", ":"))
    payload_b64 = base64.urlsafe_b64encode(payload_json.encode("utf-8")).decode("utf-8")

    signature = hmac.new(secret, payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{signature}"


def validate_oauth_state(
    state: str,
    expected_tenant_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """
    Validate an OAuth state parameter.
    Verifies signature and expiration, ensuring tenant isolation.
    """
    if not state or "." not in state:
        raise OAuthStateError("Invalid state format.")

    parts = state.split(".", 1)
    if len(parts) != 2:
        raise OAuthStateError("Malformed state token.")

    payload_b64, signature = parts
    secret = _get_state_secret()
    expected_sig = hmac.new(secret, payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(signature, expected_sig):
        raise OAuthStateError("State signature validation failed.")

    try:
        raw_json = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
        payload = json.loads(raw_json)
    except Exception as exc:
        raise OAuthStateError("Failed to decode state payload.") from exc

    exp = payload.get("exp", 0)
    if time.time() > exp:
        raise OAuthStateError("OAuth state has expired. Please initiate authorization again.")

    if expected_tenant_id and payload.get("tenant_id") != str(expected_tenant_id):
        raise OAuthStateError("State tenant mismatch.")

    return payload


def build_authorization_url(
    state: str,
    redirect_uri: str | None = None,
    scopes: list[str] | None = None,
) -> str:
    """
    Construct the Google OAuth 2.0 authorization URL.
    Requests offline access and consent prompt to ensure refresh token issuance.
    """
    if not settings.google_client_id:
        raise OAuthConfigurationError("GOOGLE_CLIENT_ID is not configured.")

    r_uri = redirect_uri or settings.google_redirect_uri
    if not r_uri:
        raise OAuthConfigurationError("GOOGLE_REDIRECT_URI is not configured.")

    scope_list = scopes or DEFAULT_SCOPES
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": r_uri,
        "response_type": "code",
        "scope": " ".join(scope_list),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def exchange_authorization_code(
    code: str,
    redirect_uri: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """
    Exchange authorization code for access and refresh tokens.
    """
    if not settings.google_client_id or not settings.google_client_secret:
        raise OAuthConfigurationError("Google OAuth client ID and secret must be configured.")

    r_uri = redirect_uri or settings.google_redirect_uri
    data = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": r_uri,
        "grant_type": "authorization_code",
    }

    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=15.0)
        should_close = True

    try:
        response = await client.post(GOOGLE_TOKEN_URL, data=data)
        if response.status_code != 200:
            error_data = {}
            with contextlib.suppress(Exception):
                error_data = response.json()
            err_desc = error_data.get("error_description") or error_data.get("error") or "Token exchange failed"
            logger.error("google_token_exchange_failed", status_code=response.status_code, error=err_desc)
            raise OAuthTokenExchangeError(f"Failed to exchange authorization code: {err_desc}")

        tokens = response.json()
        return tokens
    except httpx.RequestError as exc:
        logger.error("google_token_exchange_network_error", error=str(exc))
        raise OAuthTokenExchangeError("Network error communicating with Google OAuth service.") from exc
    finally:
        if should_close:
            await client.aclose()


async def refresh_google_access_token(
    refresh_token: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """
    Refresh an expired Google access token using the stored refresh token.
    Detects invalid_grant and raises OAuthRevokedError.
    """
    if not settings.google_client_id or not settings.google_client_secret:
        raise OAuthConfigurationError("Google OAuth client ID and secret must be configured.")

    data = {
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=15.0)
        should_close = True

    try:
        response = await client.post(GOOGLE_TOKEN_URL, data=data)
        if response.status_code == 400:
            error_data = {}
            with contextlib.suppress(Exception):
                error_data = response.json()
            if error_data.get("error") == "invalid_grant":
                logger.warning("google_token_refresh_invalid_grant", error=error_data.get("error_description"))
                raise OAuthRevokedError("Google OAuth refresh token was revoked or expired.")
            raise OAuthTokenExchangeError(f"Refresh failed: {error_data.get('error_description', 'Bad Request')}")

        if response.status_code != 200:
            raise OAuthTokenExchangeError(f"Google token refresh returned HTTP {response.status_code}")

        return response.json()
    except httpx.RequestError as exc:
        raise OAuthTokenExchangeError("Network error during token refresh.") from exc
    finally:
        if should_close:
            await client.aclose()


async def revoke_google_token(
    token: str,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """
    Revoke a token with Google. Best-effort; returns True if revoked.
    """
    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=10.0)
        should_close = True

    try:
        res = await client.post(GOOGLE_REVOKE_URL, params={"token": token})
        return res.status_code == 200
    except Exception as exc:
        logger.warning("google_token_revoke_error", error=str(exc))
        return False
    finally:
        if should_close:
            await client.aclose()
