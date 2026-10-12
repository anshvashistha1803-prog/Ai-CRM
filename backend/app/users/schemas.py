"""
User Pydantic schemas — request/response validation for user management, invitations, and profiles.
"""

import uuid
from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.core.schemas.base import BaseSchema, TimestampSchema, UUIDSchema


class UserResponse(UUIDSchema, TimestampSchema):
    tenant_id: uuid.UUID
    email: str
    full_name: str
    role: str
    is_active: bool
    last_login_at: datetime | None = None
    invited_by: uuid.UUID | None = None


class UserInviteRequest(BaseSchema):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    role: str = Field(pattern="^(AGENCY_ADMIN|MANAGER|AGENT)$")


class UserInviteResponse(BaseSchema):
    email: str
    role: str
    invitation_token: str
    expires_in_days: int = 7


class AcceptInviteRequest(BaseSchema):
    token: str
    password: str = Field(min_length=10, max_length=128)
    full_name: str | None = Field(default=None, min_length=1, max_length=255)

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


class UserUpdateRequest(BaseSchema):
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    role: str | None = Field(default=None, pattern="^(AGENCY_ADMIN|MANAGER|AGENT)$")
    is_active: bool | None = None
