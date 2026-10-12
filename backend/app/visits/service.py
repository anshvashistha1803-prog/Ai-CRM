"""
SiteVisitService — handles conflict-checked visit bookings, rescheduling,
cancellations, agent calendar conflict resolution, and notification dispatch.
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.leads.models import Lead, LeadActivity
from app.leads.state_machine import LeadStage
from app.messaging.outbox_service import OutboxService
from app.properties.models import Property
from app.users.models import User
from app.visits.models import SiteVisit
from app.visits.schemas import (
    SiteVisitCreateRequest,
    SiteVisitRescheduleRequest,
)

logger = structlog.get_logger()

ACTIVE_VISIT_STATUSES = ("SCHEDULED", "CONFIRMED")


class SiteVisitService:
    def __init__(self, db: AsyncSession, context: TenantContext):
        self.db = db
        self.context = context

    async def _find_and_authorize_lead(self, lead_id: uuid.UUID) -> Lead:
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
            raise PermissionDeniedError("Agents can only manage site visits for their assigned leads.")

        return lead

    async def _find_property(self, property_id: uuid.UUID) -> Property:
        stmt = select(Property).where(
            Property.id == property_id,
            Property.tenant_id == self.context.tenant_id,
            Property.is_deleted.is_(False),
        )
        res = await self.db.execute(stmt)
        prop = res.scalars().first()
        if not prop:
            raise NotFoundError("Property not found.")
        return prop

    async def _check_agent_conflicts(
        self,
        agent_id: uuid.UUID,
        start_at: datetime,
        end_at: datetime,
        exclude_visit_id: uuid.UUID | None = None,
    ) -> None:
        """Ensure the agent has no overlapping active site visits."""
        stmt = select(SiteVisit).where(
            SiteVisit.tenant_id == self.context.tenant_id,
            SiteVisit.assigned_agent_id == agent_id,
            SiteVisit.status.in_(ACTIVE_VISIT_STATUSES),
            SiteVisit.scheduled_start_at < end_at,
            SiteVisit.scheduled_end_at > start_at,
        )
        if exclude_visit_id:
            stmt = stmt.where(SiteVisit.id != exclude_visit_id)

        res = await self.db.execute(stmt)
        conflict = res.scalars().first()
        if conflict:
            raise ConflictError(
                f"Agent is already scheduled for another visit between "
                f"{conflict.scheduled_start_at.isoformat()} and {conflict.scheduled_end_at.isoformat()}."
            )

    async def schedule_visit(self, payload: SiteVisitCreateRequest) -> SiteVisit:
        """Schedule a persistent, conflict-checked site visit."""
        lead = await self._find_and_authorize_lead(payload.lead_id)
        prop = await self._find_property(payload.property_id)

        # Timezone validation
        if payload.scheduled_start_at.tzinfo is None or payload.scheduled_end_at.tzinfo is None:
            raise ValidationError("Scheduled visit timestamps must be timezone-aware.")

        now = datetime.now(UTC)
        if payload.scheduled_start_at <= now:
            raise ValidationError("Visit scheduled start time must be in the future.")

        # Determine agent
        agent_id = payload.assigned_agent_id or lead.assigned_agent_id or self.context.user_id

        # Acquire row-level locks on agent and lead to serialize concurrent bookings
        if agent_id:
            await self.db.execute(
                select(User.id).where(User.id == agent_id).with_for_update()
            )
        await self.db.execute(
            select(Lead.id).where(Lead.id == lead.id).with_for_update()
        )

        # Check agent conflict
        if agent_id:
            await self._check_agent_conflicts(
                agent_id=agent_id,
                start_at=payload.scheduled_start_at,
                end_at=payload.scheduled_end_at,
            )

        # Check duplicate visit for same lead + property in overlapping window
        dup_stmt = select(SiteVisit).where(
            SiteVisit.tenant_id == self.context.tenant_id,
            SiteVisit.lead_id == lead.id,
            SiteVisit.property_id == prop.id,
            SiteVisit.status.in_(ACTIVE_VISIT_STATUSES),
            SiteVisit.scheduled_start_at < payload.scheduled_end_at,
            SiteVisit.scheduled_end_at > payload.scheduled_start_at,
        )
        dup_res = await self.db.execute(dup_stmt)
        if dup_res.scalars().first():
            raise ConflictError("A site visit for this lead and property is already scheduled in this time slot.")

        visit_id = uuid.uuid4()
        outbox_svc = OutboxService(self.db)

        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            visit = SiteVisit(
                id=visit_id,
                tenant_id=self.context.tenant_id,
                lead_id=lead.id,
                property_id=prop.id,
                assigned_agent_id=agent_id,
                scheduled_start_at=payload.scheduled_start_at,
                scheduled_end_at=payload.scheduled_end_at,
                status="SCHEDULED",
                notes=payload.notes,
                created_at=now,
                updated_at=now,
            )
            self.db.add(visit)

            # Advance lead stage to SITE_VISIT_SCHEDULED if in QUALIFIED
            if lead.stage in (LeadStage.QUALIFIED.value, LeadStage.NEW.value, LeadStage.QUALIFYING.value):
                lead.stage = LeadStage.SITE_VISIT_SCHEDULED.value

            lead.last_activity_at = now

            activity = LeadActivity(
                id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                lead_id=lead.id,
                actor_id=self.context.user_id,
                actor_type="USER",
                activity_type="SITE_VISIT_SCHEDULED",
                description=f"Site visit scheduled at {prop.title} for {payload.scheduled_start_at.isoformat()}",
                metadata_={
                    "visit_id": str(visit_id),
                    "property_id": str(prop.id),
                    "start_at": payload.scheduled_start_at.isoformat(),
                },
                created_at=now,
            )
            self.db.add(activity)

            # Enqueue Outbox notification
            await outbox_svc.enqueue_message(
                tenant_id=self.context.tenant_id,
                event_type="SITE_VISIT_NOTIFICATION",
                aggregate_type="VISIT",
                aggregate_id=visit_id,
                payload={
                    "event": "SCHEDULED",
                    "lead_id": str(lead.id),
                    "property_id": str(prop.id),
                    "agent_id": str(agent_id) if agent_id else None,
                    "start_at": payload.scheduled_start_at.isoformat(),
                },
                idempotency_key=f"visit-sched-{visit_id}",
            )
            await self.db.flush()

        logger.info("site_visit_scheduled", visit_id=str(visit_id), lead_id=str(lead.id))
        return visit

    async def get_visit(self, visit_id: uuid.UUID) -> SiteVisit:
        """Fetch visit by ID enforcing tenant isolation."""
        stmt = (
            select(SiteVisit)
            .options(
                selectinload(SiteVisit.lead),
                selectinload(SiteVisit.property),
                selectinload(SiteVisit.assigned_agent),
            )
            .where(
                SiteVisit.id == visit_id,
                SiteVisit.tenant_id == self.context.tenant_id,
            )
        )
        res = await self.db.execute(stmt)
        visit = res.scalars().first()
        if not visit:
            raise NotFoundError("Site visit not found.")
        return visit

    async def reschedule_visit(
        self,
        visit_id: uuid.UUID,
        payload: SiteVisitRescheduleRequest,
    ) -> SiteVisit:
        """Reschedule an existing site visit to a new time window."""
        visit = await self.get_visit(visit_id)
        if visit.status not in ACTIVE_VISIT_STATUSES:
            raise ConflictError(f"Cannot reschedule visit in status '{visit.status}'.")

        now = datetime.now(UTC)
        if payload.new_start_at <= now:
            raise ValidationError("New start time must be in the future.")

        # Acquire row-level locks on agent and lead to serialize concurrent reschedules
        if visit.assigned_agent_id:
            await self.db.execute(
                select(User.id).where(User.id == visit.assigned_agent_id).with_for_update()
            )
        await self.db.execute(
            select(Lead.id).where(Lead.id == visit.lead_id).with_for_update()
        )

        # Check agent conflict
        if visit.assigned_agent_id:
            await self._check_agent_conflicts(
                agent_id=visit.assigned_agent_id,
                start_at=payload.new_start_at,
                end_at=payload.new_end_at,
                exclude_visit_id=visit.id,
            )

        new_visit_id = uuid.uuid4()
        outbox_svc = OutboxService(self.db)

        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            # Mark old visit as RESCHEDULED
            visit.status = "RESCHEDULED"
            visit.updated_at = now

            new_visit = SiteVisit(
                id=new_visit_id,
                tenant_id=self.context.tenant_id,
                lead_id=visit.lead_id,
                property_id=visit.property_id,
                assigned_agent_id=visit.assigned_agent_id,
                scheduled_start_at=payload.new_start_at,
                scheduled_end_at=payload.new_end_at,
                status="SCHEDULED",
                rescheduled_from_id=visit.id,
                notes=payload.notes or visit.notes,
                created_at=now,
                updated_at=now,
            )
            self.db.add(new_visit)

            activity = LeadActivity(
                id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                lead_id=visit.lead_id,
                actor_id=self.context.user_id,
                actor_type="USER",
                activity_type="SITE_VISIT_RESCHEDULED",
                description=f"Site visit rescheduled to {payload.new_start_at.isoformat()}",
                metadata_={
                    "old_visit_id": str(visit.id),
                    "new_visit_id": str(new_visit_id),
                },
                created_at=now,
            )
            self.db.add(activity)

            await outbox_svc.enqueue_message(
                tenant_id=self.context.tenant_id,
                event_type="SITE_VISIT_NOTIFICATION",
                aggregate_type="VISIT",
                aggregate_id=new_visit_id,
                payload={"event": "RESCHEDULED", "old_visit_id": str(visit.id)},
                idempotency_key=f"visit-resched-{new_visit_id}",
            )
            await self.db.flush()

        logger.info("site_visit_rescheduled", old_visit_id=str(visit.id), new_visit_id=str(new_visit_id))
        return new_visit

    async def cancel_visit(self, visit_id: uuid.UUID, reason: str) -> SiteVisit:
        """Cancel an active site visit and revert lead stage if appropriate."""
        visit = await self.get_visit(visit_id)
        if visit.status not in ACTIVE_VISIT_STATUSES:
            raise ConflictError(f"Cannot cancel visit with status '{visit.status}'.")

        now = datetime.now(UTC)
        outbox_svc = OutboxService(self.db)

        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            visit.status = "CANCELLED"
            visit.cancellation_reason = reason
            visit.updated_at = now

            # Check if lead has any other active visits
            other_active_stmt = select(SiteVisit).where(
                SiteVisit.lead_id == visit.lead_id,
                SiteVisit.id != visit.id,
                SiteVisit.status.in_(ACTIVE_VISIT_STATUSES),
            )
            other_active = await self.db.execute(other_active_stmt)

            if not other_active.scalars().first():
                # Revert lead stage back to QUALIFIED
                lead = await self._find_and_authorize_lead(visit.lead_id)
                if lead.stage == LeadStage.SITE_VISIT_SCHEDULED.value:
                    lead.stage = LeadStage.QUALIFIED.value
                    lead.last_activity_at = now

            activity = LeadActivity(
                id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                lead_id=visit.lead_id,
                actor_id=self.context.user_id,
                actor_type="USER",
                activity_type="SITE_VISIT_CANCELLED",
                description=f"Site visit cancelled: {reason}",
                metadata_={"visit_id": str(visit.id), "reason": reason},
                created_at=now,
            )
            self.db.add(activity)

            await outbox_svc.enqueue_message(
                tenant_id=self.context.tenant_id,
                event_type="SITE_VISIT_NOTIFICATION",
                aggregate_type="VISIT",
                aggregate_id=visit.id,
                payload={"event": "CANCELLED", "reason": reason},
                idempotency_key=f"visit-cancel-{visit.id}",
            )
            await self.db.flush()

        logger.info("site_visit_cancelled", visit_id=str(visit.id), reason=reason)
        return visit

    async def complete_visit(self, visit_id: uuid.UUID, notes: str | None = None) -> SiteVisit:
        """Mark a site visit as completed."""
        visit = await self.get_visit(visit_id)
        if visit.status not in ACTIVE_VISIT_STATUSES:
            raise ConflictError(f"Cannot complete visit with status '{visit.status}'.")

        now = datetime.now(UTC)
        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            visit.status = "COMPLETED"
            if notes:
                visit.notes = f"{visit.notes or ''}\nOutcome: {notes}".strip()
            visit.updated_at = now

            activity = LeadActivity(
                id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                lead_id=visit.lead_id,
                actor_id=self.context.user_id,
                actor_type="USER",
                activity_type="SITE_VISIT_COMPLETED",
                description="Site visit successfully completed",
                metadata_={"visit_id": str(visit.id), "notes": notes},
                created_at=now,
            )
            self.db.add(activity)
            await self.db.flush()

        logger.info("site_visit_completed", visit_id=str(visit.id))
        return visit

    async def list_visits(
        self,
        lead_id: uuid.UUID | None = None,
        property_id: uuid.UUID | None = None,
        status: str | None = None,
    ) -> Sequence[SiteVisit]:
        """List visits within agency with optional filters."""
        stmt = (
            select(SiteVisit)
            .where(SiteVisit.tenant_id == self.context.tenant_id)
            .order_by(SiteVisit.scheduled_start_at.desc())
        )
        if lead_id:
            stmt = stmt.where(SiteVisit.lead_id == lead_id)
        if property_id:
            stmt = stmt.where(SiteVisit.property_id == property_id)
        if status:
            stmt = stmt.where(SiteVisit.status == status)

        # Agent restriction
        if self.context.role == UserRole.AGENT:
            stmt = stmt.where(SiteVisit.assigned_agent_id == self.context.user_id)

        res = await self.db.execute(stmt)
        return res.scalars().all()
