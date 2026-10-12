"""
Auth Pydantic schemas — request/response validation for login, token refresh,
user registration, and invitation flows.
"""

import uuid

from pydantic import EmailStr, Field, field_validator

from app.core.schemas.base import BaseSchema, TimestampSchema, UUIDSchema

# ── Request schemas ───────────────────────────────────────────────────────────

class LoginRequest(BaseSchema):
    email: EmailStr
    password: str = Field(min_length=1)


class RefreshRequest(BaseSchema):
    """Body not needed — refresh token is in HttpOnly cookie."""
    pass


class InviteUserRequest(BaseSchema):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    role: str = Field(pattern="^(AGENCY_ADMIN|MANAGER|AGENT)$")


class AcceptInviteRequest(BaseSchema):
    token: str
    password: str = Field(min_length=10, max_length=128)
    full_name: str = Field(min_length=1, max_length=255)

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        errors = []
        if not any(c.isupper() for c in v):
            errors.append("at least one uppercase letter")
        if not any(c.isdigit() for c in v):
            errors.append("at least one digit")
        if not any(c in "!@#$%^&*()_+-=[]{}|;:,.<>?" for c in v):
            errors.append("at least one special character")
        if errors:
            raise ValueError(f"Password must contain: {', '.join(errors)}")
        return v


# ── Response schemas ──────────────────────────────────────────────────────────

class TokenResponse(BaseSchema):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class UserSummary(UUIDSchema, TimestampSchema):
    email: str
    full_name: str
    role: str
    is_active: bool
    tenant_id: uuid.UUID


class LoginResponse(BaseSchema):
    token: TokenResponse
    user: UserSummary
