"""
MatchingService — coordinates deterministic lead-property matching, persistence,
and agent shortlisting/rejection workflows.
Enforces multi-tenant isolation and role-based lead access.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.leads.models import Lead, LeadActivity
from app.matching.engine import MatchEngine
from app.matching.models import LeadPropertyMatch
from app.properties.models import Property

logger = structlog.get_logger()


class MatchingService:
    def __init__(self, db: AsyncSession, context: TenantContext):
        self.db = db
        self.context = context

    async def _find_and_authorize_lead(self, lead_id: uuid.UUID) -> Lead:
        """Fetch lead and ensure the user is authorized to access it."""
        stmt = select(Lead).where(Lead.id == lead_id, Lead.is_deleted.is_(False))
        if not self.context.is_super_admin():
            stmt = stmt.where(Lead.tenant_id == self.context.tenant_id)

        res = await self.db.execute(stmt)
        lead = res.scalars().first()
        if not lead:
            raise NotFoundError("Lead not found.")

        if (
            self.context.role == UserRole.AGENT
            and lead.assigned_agent_id != self.context.user_id
        ):
            raise PermissionDeniedError("Agents can only access matches for their assigned leads.")

        return lead

    async def compute_matches_for_lead(
        self,
        lead_id: uuid.UUID,
        min_score: float = 30.0,
    ) -> Sequence[LeadPropertyMatch]:
        """
        Evaluate all verified available properties against the lead's criteria,
        persist matching scores and explainability reasons, and return results.
        """
        lead = await self._find_and_authorize_lead(lead_id)

        # Retrieve available properties within the same agency
        prop_stmt = select(Property).where(
            Property.tenant_id == lead.tenant_id,
            Property.is_deleted.is_(False),
            Property.status == "AVAILABLE",
        )
        prop_res = await self.db.execute(prop_stmt)
        properties = prop_res.scalars().all()

        now = datetime.now(UTC)
        matches: list[LeadPropertyMatch] = []

        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            for prop in properties:
                eval_res = MatchEngine.evaluate_pair(lead, prop)
                if eval_res.score < min_score:
                    continue

                # Check existing match record
                existing_stmt = select(LeadPropertyMatch).where(
                    LeadPropertyMatch.lead_id == lead.id,
                    LeadPropertyMatch.property_id == prop.id,
                )
                existing_res = await self.db.execute(existing_stmt)
                match = existing_res.scalars().first()

                if match:
                    match.match_score = eval_res.score
                    match.match_reasons = eval_res.reasons
                    match.updated_at = now
                else:
                    match = LeadPropertyMatch(
                        id=uuid.uuid4(),
                        tenant_id=lead.tenant_id,
                        lead_id=lead.id,
                        property_id=prop.id,
                        match_score=eval_res.score,
                        match_reasons=eval_res.reasons,
                        status="SUGGESTED",
                        created_at=now,
                        updated_at=now,
                    )
                    self.db.add(match)

                match.property = prop
                matches.append(match)

            # Record activity
            is_system = self.context.role == UserRole.SUPER_ADMIN or not self.context.user_id
            activity = LeadActivity(
                id=uuid.uuid4(),
                tenant_id=lead.tenant_id,
                lead_id=lead.id,
                actor_id=None if is_system else self.context.user_id,
                actor_type="SYSTEM" if is_system else "USER",
                activity_type="PROPERTY_MATCHES_COMPUTED",
                description=f"Computed {len(matches)} property matches (min score {min_score})",
                metadata_={"matched_count": len(matches), "min_score": min_score},
                created_at=now,
            )
            self.db.add(activity)
            await self.db.flush()

        logger.info(
            "matches_computed",
            lead_id=str(lead.id),
            matched_count=len(matches),
            tenant_id=str(lead.tenant_id),
        )
        # Sort descending by score
        matches.sort(key=lambda m: float(m.match_score), reverse=True)
        return matches

    async def get_lead_matches(self, lead_id: uuid.UUID) -> Sequence[LeadPropertyMatch]:
        """Retrieve stored property matches for a lead."""
        lead = await self._find_and_authorize_lead(lead_id)

        stmt = (
            select(LeadPropertyMatch)
            .options(selectinload(LeadPropertyMatch.property))
            .where(
                LeadPropertyMatch.lead_id == lead.id,
                LeadPropertyMatch.tenant_id == lead.tenant_id,
            )
            .order_by(LeadPropertyMatch.match_score.desc())
        )
        res = await self.db.execute(stmt)
        return res.scalars().all()

    async def update_match_status(
        self,
        lead_id: uuid.UUID,
        match_id: uuid.UUID,
        status: str,
    ) -> LeadPropertyMatch:
        """Update match status (e.g. SHORTLISTED, REJECTED, VISITED)."""
        lead = await self._find_and_authorize_lead(lead_id)

        stmt = (
            select(LeadPropertyMatch)
            .options(selectinload(LeadPropertyMatch.property))
            .where(
                LeadPropertyMatch.id == match_id,
                LeadPropertyMatch.lead_id == lead.id,
                LeadPropertyMatch.tenant_id == lead.tenant_id,
            )
        )
        res = await self.db.execute(stmt)
        match = res.scalars().first()
        if not match:
            raise NotFoundError("Property match not found.")

        now = datetime.now(UTC)
        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            match.status = status
            match.updated_at = now

            activity = LeadActivity(
                id=uuid.uuid4(),
                tenant_id=lead.tenant_id,
                lead_id=lead.id,
                actor_id=self.context.user_id,
                actor_type="USER",
                activity_type="MATCH_STATUS_UPDATED",
                description=f"Property {match.property_id} status changed to {status}",
                metadata_={"match_id": str(match.id), "status": status},
                created_at=now,
            )
            self.db.add(activity)
            await self.db.flush()

        logger.info(
            "match_status_updated",
            match_id=str(match.id),
            lead_id=str(lead.id),
            status=status,
        )
        return match
