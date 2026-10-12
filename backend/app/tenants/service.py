"""
TenantService — agency onboarding, configuration management, and tenant isolation enforcement.
"""

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.security import hash_password
from app.tenants.models import Tenant
from app.tenants.schemas import TenantOnboardRequest, TenantUpdateRequest
from app.users.models import User

logger = structlog.get_logger()


class TenantService:
    def __init__(self, db: AsyncSession, context: TenantContext | None = None):
        self.db = db
        self.context = context

    async def onboard_agency(self, payload: TenantOnboardRequest) -> tuple[Tenant, User]:
        """
        Onboard a new real estate agency.
        Atomically creates the Tenant and its first AGENCY_ADMIN user.
        """
        # Check slug availability
        slug_check = await self.db.execute(
            select(Tenant.id).where(Tenant.slug == payload.agency_slug.lower())
        )
        if slug_check.scalar_one_or_none():
            raise ConflictError(f"Agency slug '{payload.agency_slug}' is already taken.")

        # Check global email conflict if normalized email is used
        norm_email = payload.admin_email.strip().lower()

        now = datetime.now(UTC)
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            tenant = Tenant(
                id=tenant_id,
                name=payload.agency_name,
                slug=payload.agency_slug.lower(),
                timezone=payload.timezone,
                currency_code=payload.currency_code,
                is_active=True,
                settings={},
                created_at=now,
                updated_at=now,
                is_deleted=False,
            )
            self.db.add(tenant)

            admin_user = User(
                id=user_id,
                tenant_id=tenant_id,
                email=payload.admin_email,
                email_normalized=norm_email,
                password_hash=hash_password(payload.admin_password),
                full_name=payload.admin_name,
                role=UserRole.AGENCY_ADMIN.value,
                is_active=True,
                created_at=now,
                updated_at=now,
                is_deleted=False,
            )
            self.db.add(admin_user)
            await self.db.flush()

        logger.info(
            "agency_onboarded",
            tenant_id=str(tenant_id),
            agency_slug=payload.agency_slug,
            admin_user_id=str(user_id),
        )
        return tenant, admin_user

    async def get_tenant(self, tenant_id: uuid.UUID) -> Tenant:
        """Retrieve tenant details, enforcing isolation."""
        if (
            self.context
            and not self.context.is_super_admin()
            and self.context.tenant_id != tenant_id
        ):
            raise PermissionDeniedError("Cannot access foreign agency settings.")

        res = await self.db.execute(
            select(Tenant).where(Tenant.id == tenant_id, Tenant.is_deleted.is_(False))
        )
        tenant = res.scalar_one_or_none()
        if not tenant:
            raise NotFoundError("Agency not found.")
        return tenant

    async def update_tenant(self, tenant_id: uuid.UUID, payload: TenantUpdateRequest) -> Tenant:
        """Update agency settings. Requires AGENCY_ADMIN or SUPER_ADMIN role."""
        if self.context:
            self.context.require_role(UserRole.AGENCY_ADMIN)
            if not self.context.is_super_admin() and self.context.tenant_id != tenant_id:
                raise PermissionDeniedError("Cannot modify foreign agency settings.")

        tenant = await self.get_tenant(tenant_id)
        now = datetime.now(UTC)

        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            if payload.name is not None:
                tenant.name = payload.name
            if payload.timezone is not None:
                tenant.timezone = payload.timezone
            if payload.currency_code is not None:
                tenant.currency_code = payload.currency_code
            if payload.whatsapp_phone_number_id is not None:
                tenant.whatsapp_phone_number_id = payload.whatsapp_phone_number_id
            if payload.email_from is not None:
                tenant.email_from = payload.email_from
            if payload.settings is not None:
                tenant.settings = {**tenant.settings, **payload.settings}

            tenant.updated_at = now
            await self.db.flush()

        logger.info("agency_updated", tenant_id=str(tenant_id))
        return tenant
