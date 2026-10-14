"""
FastAPI dependencies: tenant context extraction, current user, RBAC guards.

These are the security boundary. Every protected endpoint must use at least
`get_current_user`. Tenant-scoped endpoints must use `get_tenant_context`.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError

from app.core.exceptions import (
    AuthenticationError,
    InvalidTokenError,
    PermissionDeniedError,
    TokenExpiredError,
)
from app.core.security import decode_token

_bearer = HTTPBearer(auto_error=False)


class UserRole(StrEnum):
    SUPER_ADMIN = "SUPER_ADMIN"
    AGENCY_ADMIN = "AGENCY_ADMIN"
    MANAGER = "MANAGER"
    AGENT = "AGENT"


# Role hierarchy: index = privilege level (higher = more powerful)
_ROLE_HIERARCHY: list[UserRole] = [
    UserRole.AGENT,
    UserRole.MANAGER,
    UserRole.AGENCY_ADMIN,
    UserRole.SUPER_ADMIN,
]


@dataclass(frozen=True)
class TenantContext:
    """Immutable context injected into every service call."""

    user_id: uuid.UUID | None
    tenant_id: uuid.UUID
    role: UserRole

    def has_role(self, minimum: UserRole) -> bool:
        """True if the user's role is at or above the minimum required."""
        user_level = _ROLE_HIERARCHY.index(self.role)
        min_level = _ROLE_HIERARCHY.index(minimum)
        return user_level >= min_level

    def require_role(self, minimum: UserRole) -> None:
        """Raise PermissionDeniedError if role is below minimum."""
        if not self.has_role(minimum):
            raise PermissionDeniedError(
                f"This action requires {minimum} role or higher. "
                f"Your role: {self.role}"
            )

    def is_super_admin(self) -> bool:
        return self.role == UserRole.SUPER_ADMIN


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> TenantContext:
    """
    Decode the Bearer JWT and return an immutable TenantContext.
    Raises 401 if token is missing, expired, or invalid.
    """
    if credentials is None:
        raise AuthenticationError("Authorization header is required.")

    try:
        payload = decode_token(credentials.credentials)
    except JWTError as exc:
        msg = str(exc)
        if "expired" in msg.lower():
            raise TokenExpiredError() from exc
        raise InvalidTokenError(detail=msg) from exc

    if payload.get("type") != "access":
        raise InvalidTokenError("Not an access token.")

    return TenantContext(
        user_id=uuid.UUID(payload["sub"]),
        tenant_id=uuid.UUID(payload["tenant_id"]),
        role=UserRole(payload["role"]),
    )


# Convenience aliases for route dependencies
def get_tenant_context(ctx: TenantContext = Depends(get_current_user)) -> TenantContext:
    """Alias — use this on all tenant-scoped endpoints."""
    return ctx


def require_role(minimum: UserRole):
    """
    Factory that returns a FastAPI dependency enforcing a minimum role.

    Usage:
        @router.get("/...", dependencies=[Depends(require_role(UserRole.MANAGER))])
    """

    async def _check(ctx: TenantContext = Depends(get_current_user)) -> TenantContext:
        ctx.require_role(minimum)
        return ctx

    return _check


# ── Super Admin bypass ────────────────────────────────────────────────────────
async def get_super_admin(ctx: TenantContext = Depends(get_current_user)) -> TenantContext:
    """Only SUPER_ADMINs may access. Used on platform-level endpoints."""
    if not ctx.is_super_admin():
        raise PermissionDeniedError("Super admin access required.")
    return ctx
