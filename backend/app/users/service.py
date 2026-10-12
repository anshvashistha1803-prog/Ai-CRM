"""
UserService — user management, role assignments, team member invitations, and profile updates.
Enforces multi-tenant isolation and role hierarchies.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import structlog
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import (
    ConflictError,
    InvalidTokenError,
    NotFoundError,
    PermissionDeniedError,
)
from app.core.security import (
    create_invitation_token,
    decode_token,
    hash_password,
)
from app.users.models import User
from app.users.schemas import AcceptInviteRequest, UserInviteRequest, UserUpdateRequest

logger = structlog.get_logger()


class UserService:
    def __init__(self, db: AsyncSession, context: TenantContext | None = None):
        self.db = db
        self.context = context

    async def invite_user(self, payload: UserInviteRequest) -> tuple[str, str, str]:
        """
        Create a secure invitation token for onboarding a team member.
        Returns (invitation_token, email, role).
        """
        if not self.context:
            raise PermissionDeniedError("Authentication required.")

        self.context.require_role(UserRole.MANAGER)

        # Privilege checks: MANAGER can only invite AGENT
        target_role = UserRole(payload.role)
        if self.context.role == UserRole.MANAGER and target_role != UserRole.AGENT:
            raise PermissionDeniedError("Managers can only invite Agents.")

        norm_email = payload.email.strip().lower()

        # Check if user already exists in this agency
        existing = await self.db.execute(
            select(User).where(
                User.tenant_id == self.context.tenant_id,
                User.email_normalized == norm_email,
                User.is_deleted.is_(False),
            )
        )
        if existing.scalar_one_or_none():
            raise ConflictError(f"A user with email '{payload.email}' already exists in this agency.")

        invitation_token = create_invitation_token(
            email=norm_email,
            tenant_id=self.context.tenant_id,
            role=target_role.value,
            invited_by=self.context.user_id,
        )

        logger.info(
            "user_invited",
            invited_email=norm_email,
            role=target_role.value,
            tenant_id=str(self.context.tenant_id),
            invited_by=str(self.context.user_id),
        )
        return invitation_token, norm_email, target_role.value

    async def accept_invitation(self, payload: AcceptInviteRequest) -> User:
        """
        Accept an invitation token and register the new user account.
        Public endpoint (no pre-existing session required).
        """
        try:
            claims = decode_token(payload.token)
        except JWTError as exc:
            raise InvalidTokenError("Invalid or expired invitation token.") from exc

        if claims.get("type") != "invitation":
            raise InvalidTokenError("Not an invitation token.")

        email = claims["sub"]
        tenant_id = uuid.UUID(claims["tenant_id"])
        role = claims["role"]
        invited_by = uuid.UUID(claims["invited_by"]) if claims.get("invited_by") else None

        now = datetime.now(UTC)

        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            # Check if user already exists
            existing_res = await self.db.execute(
                select(User).where(
                    User.tenant_id == tenant_id,
                    User.email_normalized == email,
                )
            )
            existing_user = existing_res.scalar_one_or_none()

            if existing_user and existing_user.is_active:
                raise ConflictError("User has already registered.")

            if existing_user and not existing_user.is_active:
                # Reactivate
                existing_user.password_hash = hash_password(payload.password)
                if payload.full_name:
                    existing_user.full_name = payload.full_name
                existing_user.is_active = True
                existing_user.deleted_at = None
                existing_user.updated_at = now
                user = existing_user
            else:
                user = User(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    email=email,
                    email_normalized=email,
                    password_hash=hash_password(payload.password),
                    full_name=payload.full_name or email.split("@")[0].capitalize(),
                    role=role,
                    is_active=True,
                    invited_by=invited_by,
                    created_at=now,
                    updated_at=now,
                    is_deleted=False,
                )
                self.db.add(user)

            await self.db.flush()

        logger.info("invitation_accepted", user_id=str(user.id), tenant_id=str(tenant_id))
        return user

    async def list_users(self) -> Sequence[User]:
        """List all active users for the current agency."""
        if not self.context:
            raise PermissionDeniedError("Authentication required.")

        stmt = (
            select(User)
            .where(
                User.tenant_id == self.context.tenant_id,
                User.is_deleted.is_(False),
            )
            .order_by(User.full_name.asc())
        )
        res = await self.db.execute(stmt)
        return res.scalars().all()

    async def get_user(self, user_id: uuid.UUID) -> User:
        """Get a single user by ID, enforcing tenant isolation."""
        if not self.context:
            raise PermissionDeniedError("Authentication required.")

        stmt = select(User).where(User.id == user_id, User.is_deleted.is_(False))
        if not self.context.is_super_admin():
            stmt = stmt.where(User.tenant_id == self.context.tenant_id)

        res = await self.db.execute(stmt)
        user = res.scalars().first()
        if not user:
            raise NotFoundError("User not found.")
        return user

    async def update_user(self, user_id: uuid.UUID, payload: UserUpdateRequest) -> User:
        """Update user role, status, or name. Enforces role security."""
        if not self.context:
            raise PermissionDeniedError("Authentication required.")

        user = await self.get_user(user_id)

        # Modifying roles or active status requires AGENCY_ADMIN
        if payload.role is not None or payload.is_active is not None:
            self.context.require_role(UserRole.AGENCY_ADMIN)

        # Prevent deactivating yourself
        if payload.is_active is False and user.id == self.context.user_id:
            raise PermissionDeniedError("Cannot deactivate your own administrator account.")

        now = datetime.now(UTC)
        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            if payload.full_name is not None:
                user.full_name = payload.full_name
            if payload.role is not None:
                user.role = payload.role
            if payload.is_active is not None:
                user.is_active = payload.is_active

            user.updated_at = now
            await self.db.flush()

        logger.info("user_updated", user_id=str(user.id))
        return user

    async def soft_delete_user(self, user_id: uuid.UUID) -> None:
        """Soft delete a user (requires AGENCY_ADMIN or SUPER_ADMIN)."""
        if not self.context:
            raise PermissionDeniedError("Authentication required.")
        self.context.require_role(UserRole.AGENCY_ADMIN)
        user = await self.get_user(user_id)
        if user.id == self.context.user_id:
            raise PermissionDeniedError("Cannot delete your own administrator account.")

        now = datetime.now(UTC)
        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            user.is_deleted = True
            user.deleted_at = now
            user.is_active = False
            user.updated_at = now
            await self.db.flush()

        logger.info("user_soft_deleted", user_id=str(user.id))

