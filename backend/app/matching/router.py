"""
Matching router — endpoints for property match computation, retrieval, and status updates.
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import TenantContext, get_current_user
from app.matching.schemas import LeadPropertyMatchResponse, MatchStatusUpdateRequest
from app.matching.service import MatchingService

router = APIRouter(prefix="/leads/{lead_id}/matches", tags=["matching"])


@router.get("", response_model=list[LeadPropertyMatchResponse])
async def get_lead_matches(
    lead_id: uuid.UUID,
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[LeadPropertyMatchResponse]:
    """Retrieve stored property matches for a lead, sorted by score descending."""
    svc = MatchingService(db, ctx)
    matches = await svc.get_lead_matches(lead_id)
    return [LeadPropertyMatchResponse.model_validate(m) for m in matches]


@router.post("/compute", response_model=list[LeadPropertyMatchResponse])
async def compute_matches(
    lead_id: uuid.UUID,
    min_score: float = Query(default=30.0, ge=0.0, le=100.0),
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[LeadPropertyMatchResponse]:
    """Run deterministic matching against available inventory and persist scored matches."""
    svc = MatchingService(db, ctx)
    matches = await svc.compute_matches_for_lead(lead_id, min_score=min_score)
    return [LeadPropertyMatchResponse.model_validate(m) for m in matches]


@router.patch("/{match_id}", response_model=LeadPropertyMatchResponse)
async def update_match_status(
    lead_id: uuid.UUID,
    match_id: uuid.UUID,
    body: MatchStatusUpdateRequest,
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LeadPropertyMatchResponse:
    """Update property match status (e.g. SHORTLISTED, REJECTED, VISITED)."""
    svc = MatchingService(db, ctx)
    match = await svc.update_match_status(lead_id, match_id, status=body.status)
    return LeadPropertyMatchResponse.model_validate(match)
