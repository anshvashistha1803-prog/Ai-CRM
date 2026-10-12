"""
Site visits router — endpoints for visit scheduling, rescheduling, cancellations, and calendar listings.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import TenantContext, get_current_user
from app.visits.schemas import (
    SiteVisitCancelRequest,
    SiteVisitCreateRequest,
    SiteVisitRescheduleRequest,
    SiteVisitResponse,
)
from app.visits.service import SiteVisitService

router = APIRouter(prefix="/visits", tags=["visits"])


@router.post("", response_model=SiteVisitResponse, status_code=201)
async def schedule_visit(
    body: SiteVisitCreateRequest,
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SiteVisitResponse:
    """Schedule a new site visit with calendar conflict and duplicate checks."""
    svc = SiteVisitService(db, ctx)
    visit = await svc.schedule_visit(body)
    return SiteVisitResponse.model_validate(visit)


@router.get("", response_model=list[SiteVisitResponse])
async def list_visits(
    lead_id: uuid.UUID | None = Query(default=None),
    property_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[SiteVisitResponse]:
    """List site visits for the agency with optional filters."""
    svc = SiteVisitService(db, ctx)
    visits = await svc.list_visits(lead_id=lead_id, property_id=property_id, status=status)
    return [SiteVisitResponse.model_validate(v) for v in visits]


@router.get("/{visit_id}", response_model=SiteVisitResponse)
async def get_visit(
    visit_id: uuid.UUID,
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SiteVisitResponse:
    """Retrieve details of a site visit."""
    svc = SiteVisitService(db, ctx)
    visit = await svc.get_visit(visit_id)
    return SiteVisitResponse.model_validate(visit)


@router.post("/{visit_id}/reschedule", response_model=SiteVisitResponse)
async def reschedule_visit(
    visit_id: uuid.UUID,
    body: SiteVisitRescheduleRequest,
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SiteVisitResponse:
    """Reschedule an active site visit to a new conflict-free time window."""
    svc = SiteVisitService(db, ctx)
    new_visit = await svc.reschedule_visit(visit_id, body)
    return SiteVisitResponse.model_validate(new_visit)


@router.post("/{visit_id}/cancel", response_model=SiteVisitResponse)
async def cancel_visit(
    visit_id: uuid.UUID,
    body: SiteVisitCancelRequest,
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SiteVisitResponse:
    """Cancel a scheduled site visit with a documented cancellation reason."""
    svc = SiteVisitService(db, ctx)
    visit = await svc.cancel_visit(visit_id, reason=body.cancellation_reason)
    return SiteVisitResponse.model_validate(visit)


@router.post("/{visit_id}/complete", response_model=SiteVisitResponse)
async def complete_visit(
    visit_id: uuid.UUID,
    notes: str | None = Query(default=None),
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SiteVisitResponse:
    """Mark a completed site visit with agent notes."""
    svc = SiteVisitService(db, ctx)
    visit = await svc.complete_visit(visit_id, notes=notes)
    return SiteVisitResponse.model_validate(visit)
