"""
Qualification router — endpoints for AI qualification proposal generation, listing, and review.
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import TenantContext, get_current_user
from app.qualification.schemas import (
    GenerateProposalRequest,
    LeadQualificationProposalResponse,
    ProposalReviewRequest,
)
from app.qualification.service import QualificationService

router = APIRouter(prefix="/leads/{lead_id}/proposals", tags=["qualification"])


@router.post("/generate", response_model=LeadQualificationProposalResponse)
async def generate_proposal(
    lead_id: uuid.UUID,
    body: GenerateProposalRequest | None = None,
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LeadQualificationProposalResponse:
    """Generate an AI qualification proposal for the lead based on conversation text."""
    svc = QualificationService(db, ctx)
    messages = body.messages if body else None
    proposal = await svc.generate_proposal(lead_id=lead_id, messages=messages)
    return LeadQualificationProposalResponse.model_validate(proposal)


@router.get("", response_model=list[LeadQualificationProposalResponse])
async def list_proposals(
    lead_id: uuid.UUID,
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[LeadQualificationProposalResponse]:
    """List all AI qualification proposals for the lead, newest first."""
    svc = QualificationService(db, ctx)
    proposals = await svc.list_proposals(lead_id=lead_id)
    return [LeadQualificationProposalResponse.model_validate(p) for p in proposals]


@router.get("/{proposal_id}", response_model=LeadQualificationProposalResponse)
async def get_proposal(
    lead_id: uuid.UUID,
    proposal_id: uuid.UUID,
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LeadQualificationProposalResponse:
    """Retrieve a single qualification proposal."""
    svc = QualificationService(db, ctx)
    proposal = await svc.get_proposal(lead_id=lead_id, proposal_id=proposal_id)
    return LeadQualificationProposalResponse.model_validate(proposal)


@router.post("/{proposal_id}/review", response_model=LeadQualificationProposalResponse)
async def review_proposal(
    lead_id: uuid.UUID,
    proposal_id: uuid.UUID,
    body: ProposalReviewRequest,
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LeadQualificationProposalResponse:
    """Accept or reject an AI qualification proposal with human review notes."""
    svc = QualificationService(db, ctx)
    proposal = await svc.review_proposal(
        lead_id=lead_id,
        proposal_id=proposal_id,
        action=body.action,
        review_notes=body.review_notes,
    )
    return LeadQualificationProposalResponse.model_validate(proposal)
