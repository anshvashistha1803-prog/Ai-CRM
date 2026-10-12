"""
QualificationService — owns AI lead qualification proposals generation and human agent review.
Enforces tenant isolation, agent assignment boundaries, and atomic database state transitions.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.leads.models import Lead, LeadActivity
from app.leads.state_machine import LeadStage
from app.qualification.llm_adapter import LLMAdapter, get_llm_adapter
from app.qualification.models import LeadQualificationProposal

logger = structlog.get_logger()


class QualificationService:
    def __init__(
        self,
        db: AsyncSession,
        context: TenantContext,
        llm_adapter: LLMAdapter | None = None,
    ):
        self.db = db
        self.context = context
        self.llm_adapter = llm_adapter or get_llm_adapter()

    async def _find_and_authorize_lead(self, lead_id: uuid.UUID) -> Lead:
        """Fetch lead and ensure the current user is authorized to access it."""
        stmt = select(Lead).where(Lead.id == lead_id, Lead.is_deleted.is_(False))

        if not self.context.is_super_admin():
            stmt = stmt.where(Lead.tenant_id == self.context.tenant_id)

        res = await self.db.execute(stmt)
        lead = res.scalars().first()
        if not lead:
            raise NotFoundError("Lead not found.")

        # Super-admin access logging
        if self.context.is_super_admin():
            logger.info(
                "super_admin_lead_access",
                lead_id=str(lead.id),
                lead_tenant=str(lead.tenant_id),
                user_id=str(self.context.user_id),
            )

        # Agent role restriction: agents can only access their assigned leads
        if (
            self.context.role == UserRole.AGENT
            and lead.assigned_agent_id != self.context.user_id
        ):
            logger.warning(
                "agent_lead_access_denied",
                user_id=str(self.context.user_id),
                lead_id=str(lead.id),
                assigned_agent_id=str(lead.assigned_agent_id),
            )
            raise PermissionDeniedError(
                "Agents can only view or manage qualification proposals for their assigned leads."
            )

        return lead

    async def generate_proposal(
        self,
        lead_id: uuid.UUID,
        messages: list[str] | None = None,
    ) -> LeadQualificationProposal:
        """
        Extract qualification criteria via LLM adapter, mark previous pending proposals as SUPERSEDED,
        and persist a new proposal for agent review.
        """
        lead = await self._find_and_authorize_lead(lead_id)

        # Collect text to extract from
        extraction_messages = messages
        if not extraction_messages:
            if lead.raw_message_text:
                extraction_messages = [lead.raw_message_text]
            else:
                raise ValidationError("No conversation messages provided or recorded for this lead.")

        extraction, model_name, prompt_hash = await self.llm_adapter.extract_qualification(
            extraction_messages
        )

        now = datetime.now(UTC)
        proposal_id = uuid.uuid4()

        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            # Mark existing pending proposals as SUPERSEDED
            await self.db.execute(
                update(LeadQualificationProposal)
                .where(
                    LeadQualificationProposal.lead_id == lead.id,
                    LeadQualificationProposal.tenant_id == lead.tenant_id,
                    LeadQualificationProposal.status == "PENDING_REVIEW",
                )
                .values(status="SUPERSEDED")
            )

            proposal = LeadQualificationProposal(
                id=proposal_id,
                tenant_id=lead.tenant_id,
                lead_id=lead.id,
                proposed_by="AI",
                status="PENDING_REVIEW",
                budget_min=extraction.budget_min,
                budget_max=extraction.budget_max,
                property_type=extraction.property_type,
                bhk=extraction.bhk,
                preferred_location=extraction.preferred_location,
                purpose=extraction.purpose,
                move_in_timeline=extraction.move_in_timeline,
                confidence=extraction.confidence_scores,
                llm_model=model_name,
                llm_prompt_hash=prompt_hash,
                raw_llm_output=extraction.raw_response,
            )
            self.db.add(proposal)

            # Advance stage from NEW to QUALIFYING if applicable
            if lead.stage == LeadStage.NEW.value:
                lead.stage = LeadStage.QUALIFYING.value

            lead.last_activity_at = now

            activity = LeadActivity(
                id=uuid.uuid4(),
                tenant_id=lead.tenant_id,
                lead_id=lead.id,
                actor_id=self.context.user_id,
                actor_type="AI",
                activity_type="AI_QUALIFICATION_PROPOSED",
                description=f"AI qualification proposal created (model: {model_name})",
                metadata_={
                    "proposal_id": str(proposal_id),
                    "confidence": extraction.confidence_scores,
                    "summary": extraction.summary,
                },
                created_at=now,
            )
            self.db.add(activity)
            await self.db.flush()

        logger.info(
            "ai_proposal_generated",
            lead_id=str(lead.id),
            proposal_id=str(proposal_id),
            tenant_id=str(lead.tenant_id),
        )
        return proposal

    async def list_proposals(self, lead_id: uuid.UUID) -> Sequence[LeadQualificationProposal]:
        """List all qualification proposals for a lead."""
        lead = await self._find_and_authorize_lead(lead_id)

        stmt = (
            select(LeadQualificationProposal)
            .where(
                LeadQualificationProposal.lead_id == lead.id,
                LeadQualificationProposal.tenant_id == lead.tenant_id,
            )
            .order_by(LeadQualificationProposal.created_at.desc())
        )
        res = await self.db.execute(stmt)
        return res.scalars().all()

    async def get_proposal(
        self, lead_id: uuid.UUID, proposal_id: uuid.UUID
    ) -> LeadQualificationProposal:
        """Get a single qualification proposal."""
        lead = await self._find_and_authorize_lead(lead_id)

        stmt = select(LeadQualificationProposal).where(
            LeadQualificationProposal.id == proposal_id,
            LeadQualificationProposal.lead_id == lead.id,
            LeadQualificationProposal.tenant_id == lead.tenant_id,
        )
        res = await self.db.execute(stmt)
        proposal = res.scalars().first()
        if not proposal:
            raise NotFoundError("Qualification proposal not found.")
        return proposal

    async def review_proposal(
        self,
        lead_id: uuid.UUID,
        proposal_id: uuid.UUID,
        action: str,
        review_notes: str | None = None,
    ) -> LeadQualificationProposal:
        """
        Accept or reject an AI qualification proposal.
        Acceptance updates lead qualification fields and advances stage to QUALIFIED.
        """
        lead = await self._find_and_authorize_lead(lead_id)
        proposal = await self.get_proposal(lead_id, proposal_id)

        if proposal.status != "PENDING_REVIEW":
            raise ConflictError(
                f"Proposal cannot be reviewed because its current status is '{proposal.status}'."
            )

        now = datetime.now(UTC)

        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            proposal.reviewed_by = self.context.user_id
            proposal.reviewed_at = now
            proposal.review_notes = review_notes

            if action == "ACCEPT":
                proposal.status = "ACCEPTED"

                # Apply proposed values to lead
                if proposal.budget_min is not None:
                    lead.budget_min = proposal.budget_min
                if proposal.budget_max is not None:
                    lead.budget_max = proposal.budget_max
                if proposal.property_type is not None:
                    lead.property_type = proposal.property_type
                if proposal.bhk is not None:
                    lead.bhk = proposal.bhk
                if proposal.preferred_location is not None:
                    lead.preferred_location = proposal.preferred_location
                if proposal.purpose is not None:
                    lead.purpose = proposal.purpose
                if proposal.move_in_timeline is not None:
                    lead.move_in_timeline = proposal.move_in_timeline

                # Progress stage if in early qualification stages
                if lead.stage in (LeadStage.NEW.value, LeadStage.QUALIFYING.value):
                    lead.stage = LeadStage.QUALIFIED.value

                # Recalculate deterministic lead intent score and health
                from app.scoring.engine import ScoringEngine
                score_out = ScoringEngine.evaluate(lead)
                lead.intent_score = score_out.intent_score
                lead.lead_health = score_out.lead_health
                lead.score_breakdown = score_out.breakdown

                lead.last_activity_at = now

                activity = LeadActivity(
                    id=uuid.uuid4(),
                    tenant_id=lead.tenant_id,
                    lead_id=lead.id,
                    actor_id=self.context.user_id,
                    actor_type="USER",
                    activity_type="PROPOSAL_ACCEPTED",
                    description="AI qualification proposal approved and applied to lead",
                    metadata_={
                        "proposal_id": str(proposal.id),
                        "review_notes": review_notes,
                    },
                    created_at=now,
                )
                self.db.add(activity)

            elif action == "REJECT":
                proposal.status = "REJECTED"

                activity = LeadActivity(
                    id=uuid.uuid4(),
                    tenant_id=lead.tenant_id,
                    lead_id=lead.id,
                    actor_id=self.context.user_id,
                    actor_type="USER",
                    activity_type="PROPOSAL_REJECTED",
                    description=f"AI qualification proposal rejected: {review_notes or 'No notes provided'}",
                    metadata_={
                        "proposal_id": str(proposal.id),
                        "review_notes": review_notes,
                    },
                    created_at=now,
                )
                self.db.add(activity)
            else:
                raise ValidationError(f"Invalid review action: '{action}'. Must be ACCEPT or REJECT.")

            await self.db.flush()

        logger.info(
            "ai_proposal_reviewed",
            lead_id=str(lead.id),
            proposal_id=str(proposal.id),
            action=action,
            reviewed_by=str(self.context.user_id),
        )
        return proposal
