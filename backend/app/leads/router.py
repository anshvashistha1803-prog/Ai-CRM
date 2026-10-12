"""
FastAPI router for Leads API endpoints (/api/v1/leads).
All endpoints enforce Bearer authentication, tenant isolation, and RBAC.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import TenantContext, UserRole, get_current_user, require_role
from app.leads.schemas import (
    LeadActivityResponse,
    LeadAssignRequest,
    LeadCreateRequest,
    LeadListResponse,
    LeadResponse,
    LeadStageTransitionRequest,
    LeadUpdateRequest,
)
from app.leads.service import LeadService

router = APIRouter(prefix="/leads", tags=["Leads"])


@router.post(
    "",
    response_model=LeadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new lead",
)
async def create_lead(
    payload: LeadCreateRequest,
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LeadResponse:
    """Create a new lead with E.164 normalization, dedup check, PII encryption, and consent logging."""
    service = LeadService(db, context)
    return await service.create_lead(payload)


@router.get(
    "",
    response_model=LeadListResponse,
    summary="List leads with pagination and filters",
)
async def list_leads(
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    stage: str | None = Query(default=None),
    assigned_agent_id: uuid.UUID | None = Query(default=None),
    lead_health: str | None = Query(default=None),
    search: str | None = Query(default=None),
) -> LeadListResponse:
    """Retrieve paginated leads filtered by stage, agent, health, or text search."""
    service = LeadService(db, context)
    return await service.list_leads(
        page=page,
        page_size=page_size,
        stage=stage,
        assigned_agent_id=assigned_agent_id,
        lead_health=lead_health,
        search=search,
    )


@router.get(
    "/{lead_id}",
    response_model=LeadResponse,
    summary="Get single lead by ID",
)
async def get_lead(
    lead_id: uuid.UUID,
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LeadResponse:
    """Fetch lead by ID within current tenant scope."""
    service = LeadService(db, context)
    return await service.get_lead(lead_id)


@router.patch(
    "/{lead_id}",
    response_model=LeadResponse,
    summary="Update lead details",
)
async def update_lead(
    lead_id: uuid.UUID,
    payload: LeadUpdateRequest,
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LeadResponse:
    """Update lead attributes and log changes in audit timeline."""
    service = LeadService(db, context)
    return await service.update_lead(lead_id, payload)


@router.post(
    "/{lead_id}/stage",
    response_model=LeadResponse,
    summary="Transition lead stage",
)
async def transition_lead_stage(
    lead_id: uuid.UUID,
    payload: LeadStageTransitionRequest,
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LeadResponse:
    """Execute a validated state machine stage transition."""
    service = LeadService(db, context)
    return await service.transition_stage(
        lead_id=lead_id,
        target_stage=payload.target_stage,
        reason=payload.reason,
    )


@router.get(
    "/{lead_id}/activities",
    response_model=list[LeadActivityResponse],
    summary="Get lead activity timeline",
)
async def get_lead_activities(
    lead_id: uuid.UUID,
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[LeadActivityResponse]:
    """Retrieve chronological audit timeline for a lead."""
    service = LeadService(db, context)
    return await service.get_activities(lead_id)


@router.post(
    "/{lead_id}/assign",
    response_model=LeadResponse,
    summary="Assign or reassign lead to an agent",
)
async def assign_lead(
    lead_id: uuid.UUID,
    payload: LeadAssignRequest,
    context: Annotated[TenantContext, Depends(require_role(UserRole.MANAGER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LeadResponse:
    """Assign or reassign lead to an agent in this agency (requires MANAGER or higher role)."""
    service = LeadService(db, context)
    return await service.assign_lead(lead_id, payload.assigned_agent_id)


@router.delete(
    "/{lead_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft delete a lead",
)
async def delete_lead(
    lead_id: uuid.UUID,
    context: Annotated[TenantContext, Depends(require_role(UserRole.MANAGER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Soft delete lead (requires MANAGER or higher role)."""
    service = LeadService(db, context)
    await service.soft_delete_lead(lead_id)
