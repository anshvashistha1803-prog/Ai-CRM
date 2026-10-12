"""
Tenant router — endpoints for agency onboarding, profile retrieval, and settings management.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import TenantContext, get_current_user
from app.tenants.schemas import TenantOnboardRequest, TenantResponse, TenantUpdateRequest
from app.tenants.service import TenantService

router = APIRouter(prefix="/tenants", tags=["tenants"])


@router.post("/onboard", response_model=TenantResponse, status_code=201)
async def onboard_agency(
    body: TenantOnboardRequest,
    db: AsyncSession = Depends(get_db),
) -> TenantResponse:
    """
    Onboard a new real estate agency and create its initial agency administrator.
    This endpoint is public for agency registration.
    """
    svc = TenantService(db)
    tenant, _ = await svc.onboard_agency(body)
    return TenantResponse.model_validate(tenant)


@router.get("/me", response_model=TenantResponse)
async def get_current_agency(
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TenantResponse:
    """Retrieve settings and configuration for the currently authenticated agency."""
    svc = TenantService(db, ctx)
    tenant = await svc.get_tenant(ctx.tenant_id)
    return TenantResponse.model_validate(tenant)


@router.patch("/me", response_model=TenantResponse)
async def update_current_agency(
    body: TenantUpdateRequest,
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TenantResponse:
    """Update settings and configuration for the agency (requires AGENCY_ADMIN role)."""
    svc = TenantService(db, ctx)
    tenant = await svc.update_tenant(ctx.tenant_id, body)
    return TenantResponse.model_validate(tenant)
