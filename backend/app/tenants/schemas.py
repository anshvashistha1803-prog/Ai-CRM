"""
Tenant Pydantic schemas — request/response validation for agency onboarding and settings.
"""

from pydantic import EmailStr, Field, field_validator

from app.core.schemas.base import BaseSchema, TimestampSchema, UUIDSchema


class TenantResponse(UUIDSchema, TimestampSchema):
    name: str
    slug: str
    timezone: str
    currency_code: str
    is_active: bool
    whatsapp_phone_number_id: str | None = None
    email_from: str | None = None
    settings: dict = Field(default_factory=dict)


class TenantUpdateRequest(BaseSchema):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    timezone: str | None = Field(default=None, max_length=50)
    currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    whatsapp_phone_number_id: str | None = Field(default=None, max_length=50)
    email_from: str | None = Field(default=None, max_length=255)
    settings: dict | None = None


class TenantOnboardRequest(BaseSchema):
    agency_name: str = Field(min_length=2, max_length=255)
    agency_slug: str = Field(min_length=2, max_length=100, pattern="^[a-z0-9-]+$")
    timezone: str = Field(default="UTC", max_length=50)
    currency_code: str = Field(default="INR", min_length=3, max_length=3)

    admin_email: EmailStr
    admin_name: str = Field(min_length=2, max_length=255)
    admin_password: str = Field(min_length=10, max_length=128)

    @field_validator("admin_password")
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
