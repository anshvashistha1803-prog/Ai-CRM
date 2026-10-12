"""
AuthService — login, token refresh, logout, and invitation management.
This service owns all authentication business logic.
No HTTP concerns here — it raises CRMException subclasses only.
"""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import (
    AuthenticationError,
    InvalidTokenError,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_password,
)
from app.users.models import RefreshToken, User

logger = structlog.get_logger()
settings = get_settings()


def _hash_token(token: str) -> str:
    """Store only the SHA-256 hash of refresh tokens, never the raw value."""
    return hashlib.sha256(token.encode()).hexdigest()


class AuthService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── Login ─────────────────────────────────────────────────────────────────
    async def login(
        self, email: str, password: str
    ) -> tuple[str, str, User]:
        """
        Authenticate user by email + password.
        Returns (access_token, refresh_token, user).
        Caller is responsible for setting the refresh token in an HttpOnly cookie.
        """
        email_normalized = email.strip().lower()

        result = await self.db.execute(
            select(User).where(
                User.email_normalized == email_normalized,
                User.is_deleted.is_(False),
            )
        )
        user = result.scalar_one_or_none()

        # Always verify (even if user not found) to prevent timing attacks
        dummy_hash = "$2b$12$placeholder.hash.to.prevent.timing.attacks.xxxxxxxxx"
        provided_hash = user.password_hash if user else dummy_hash

        if not verify_password(password, provided_hash) or user is None:
            logger.warning("login_failed", email=email_normalized)
            raise AuthenticationError("Invalid email or password.")

        if not user.is_active:
            raise AuthenticationError("Your account has been deactivated.")

        # Issue tokens
        access_token = create_access_token(
            user_id=user.id,
            tenant_id=user.tenant_id,
            role=user.role,
        )
        refresh_token, jti = create_refresh_token(
            user_id=user.id,
            tenant_id=user.tenant_id,
        )

        # Persist hashed refresh token and update user last login atomically
        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            expires_at = datetime.now(UTC) + timedelta(
                days=settings.jwt_refresh_token_expire_days
            )
            db_token = RefreshToken(
                user_id=user.id,
                token_hash=_hash_token(refresh_token),
                expires_at=expires_at,
            )
            self.db.add(db_token)

            # Update last login
            await self.db.execute(
                update(User)
                .where(User.id == user.id)
                .values(last_login_at=datetime.now(UTC))
            )
            await self.db.flush()

        logger.info("login_success", user_id=str(user.id), role=user.role)
        return access_token, refresh_token, user

    # ── Refresh ───────────────────────────────────────────────────────────────
    async def refresh_access_token(self, refresh_token: str) -> tuple[str, str, User]:
        """
        Validate refresh token, rotate it, and issue a new access token.
        Returns (new_access_token, new_refresh_token, user).
        Old refresh token is revoked immediately (rotation).
        """
        from jose import JWTError

        try:
            payload = decode_token(refresh_token)
        except JWTError as exc:
            raise InvalidTokenError("Invalid or expired refresh token.") from exc

        if payload.get("type") != "refresh":
            raise InvalidTokenError("Not a refresh token.")

        token_hash = _hash_token(refresh_token)
        result = await self.db.execute(
            select(RefreshToken)
            .where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked_at.is_(None),
            )
            .with_for_update()
        )
        db_token = result.scalar_one_or_none()
        if db_token is None or db_token.is_expired:
            raise InvalidTokenError("Refresh token not found or already used.")

        # Load user
        user_result = await self.db.execute(
            select(User).where(User.id == db_token.user_id, User.is_deleted.is_(False))
        )
        user = user_result.scalar_one_or_none()
        if user is None or not user.is_active:
            raise AuthenticationError("User account is inactive.")

        # Issue new tokens and rotate in an atomic transaction
        access_token = create_access_token(user.id, user.tenant_id, user.role)
        new_refresh_token, _ = create_refresh_token(user.id, user.tenant_id)

        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            # Revoke old token immediately (rotation)
            db_token.revoked_at = datetime.now(UTC)

            expires_at = datetime.now(UTC) + timedelta(
                days=settings.jwt_refresh_token_expire_days
            )
            self.db.add(RefreshToken(
                user_id=user.id,
                token_hash=_hash_token(new_refresh_token),
                expires_at=expires_at,
            ))
            await self.db.flush()

        return access_token, new_refresh_token, user

    # ── Logout ────────────────────────────────────────────────────────────────
    async def logout(self, refresh_token: str) -> None:
        """Revoke the given refresh token."""
        token_hash = _hash_token(refresh_token)
        result = await self.db.execute(
            select(RefreshToken).where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked_at.is_(None),
            )
        )
        db_token = result.scalar_one_or_none()
        if db_token:
            db_token.revoked_at = datetime.now(UTC)
            await self.db.flush()

    async def logout_all(self, user_id: uuid.UUID) -> None:
        """Revoke ALL refresh tokens for a user (password change, deactivation)."""
        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            await self.db.execute(
                update(RefreshToken)
                .where(
                    RefreshToken.user_id == user_id,
                    RefreshToken.revoked_at.is_(None),
                )
                .values(revoked_at=datetime.now(UTC))
            )
            await self.db.flush()
