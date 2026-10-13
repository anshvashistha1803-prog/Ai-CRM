"""
LeadService — business logic for lead management, deduplication,
PII encryption/decryption, state transitions, and audit activity feeds.
All database queries strictly enforce tenant isolation and role-based agent assignment policies.
"""

import math
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import (
    DuplicateLeadError,
    NotFoundError,
    PermissionDeniedError,
)
from app.core.security import compute_blind_hash, decrypt_field, encrypt_field
from app.leads.deduplication import normalize_email, normalize_phone
from app.leads.models import ConsentRecord, Lead, LeadActivity
from app.leads.schemas import (
    LeadActivityResponse,
    LeadCreateRequest,
    LeadListResponse,
    LeadResponse,
    LeadUpdateRequest,
)
from app.leads.state_machine import LeadStage, validate_transition
from app.users.models import User

logger = structlog.get_logger()


class LeadService:
    def __init__(self, db: AsyncSession, context: TenantContext) -> None:
        self.db = db
        self.context = context

    async def create_lead(self, payload: LeadCreateRequest) -> LeadResponse:
        """
        Create a new lead with E.164 phone normalization, deduplication check,
        AES-256-GCM encryption, consent logging, and timeline activity creation.
        Enforces tenant isolation and agent assignment boundaries.
        """
        # 1. Normalization
        norm_phone = normalize_phone(payload.phone)
        norm_email = normalize_email(payload.email) if payload.email else None

        phone_hash = compute_blind_hash(norm_phone)
        email_hash = compute_blind_hash(norm_email) if norm_email else None

        # 2. Deduplication check (tenant-isolated)
        conditions = [Lead.phone_hash == phone_hash]
        if email_hash:
            conditions.append(Lead.email_hash == email_hash)

        dup_stmt = select(Lead).where(
            Lead.tenant_id == self.context.tenant_id,
            Lead.is_deleted.is_(False),
            or_(*conditions),
        )
        dup_result = await self.db.execute(dup_stmt)
        existing_lead = dup_result.scalars().first()

        if existing_lead:
            raise DuplicateLeadError(
                f"A lead with this phone number or email already exists in this agency (ID: {existing_lead.id})."
            )

        # 3. Role-based assignment policy
        if self.context.role == UserRole.AGENT:
            # Agents can only create leads assigned to themselves
            if payload.assigned_agent_id and payload.assigned_agent_id != self.context.user_id:
                raise PermissionDeniedError("Agents cannot assign leads to other users.")
            assigned_agent_id = self.context.user_id
        else:
            # Managers and Admins may assign to any agent in their agency
            assigned_agent_id = payload.assigned_agent_id
            if assigned_agent_id:
                agent_check = await self.db.execute(
                    select(User.id).where(
                        User.id == assigned_agent_id,
                        User.tenant_id == self.context.tenant_id,
                        User.is_deleted.is_(False),
                    )
                )
                if not agent_check.scalar_one_or_none():
                    raise PermissionDeniedError("Assigned agent does not belong to this agency.")

        # 4. Encrypt PII
        phone_enc = encrypt_field(norm_phone)
        email_enc = encrypt_field(norm_email) if norm_email else None

        now = datetime.now(UTC)
        lead = Lead(
            id=uuid.uuid4(),
            tenant_id=self.context.tenant_id,
            source=payload.source,
            full_name=payload.full_name,
            phone_e164_enc=phone_enc,
            email_enc=email_enc,
            phone_hash=phone_hash,
            email_hash=email_hash,
            preferred_contact=payload.preferred_contact,
            budget_min=payload.budget_min,
            budget_max=payload.budget_max,
            currency_code=payload.currency_code,
            property_type=payload.property_type,
            bhk=payload.bhk,
            preferred_location=payload.preferred_location,
            purpose=payload.purpose,
            move_in_timeline=payload.move_in_timeline,
            notes=payload.notes,
            assigned_agent_id=assigned_agent_id,
            stage=LeadStage.NEW.value,
            lead_health="UNKNOWN",
            automation_status="ACTIVE",
            do_not_contact=False,
            is_deleted=False,
            created_at=now,
            updated_at=now,
            last_activity_at=now,
        )

        # 5. Multi-table write inside explicit transaction boundary
        try:
            async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
                self.db.add(lead)
                await self.db.flush()

                # Consent logging
                if payload.consent_granted:
                    consent = ConsentRecord(
                        id=uuid.uuid4(),
                        tenant_id=self.context.tenant_id,
                        lead_id=lead.id,
                        consent_type="MARKETING",
                        granted=True,
                        channel=payload.consent_channel,
                        ip_address=payload.consent_ip,
                        user_agent=payload.consent_user_agent,
                        created_at=now,
                    )
                    self.db.add(consent)

                # Timeline activity log
                activity = LeadActivity(
                    id=uuid.uuid4(),
                    tenant_id=self.context.tenant_id,
                    lead_id=lead.id,
                    actor_id=self.context.user_id,
                    actor_type="USER",
                    activity_type="LEAD_CREATED",
                    description=f"Lead created via {payload.source}",
                    metadata_={"source": payload.source, "initial_stage": lead.stage},
                    created_at=now,
                )
                self.db.add(activity)
                await self.db.flush()
        except IntegrityError as exc:
            err_msg = str(exc).lower()
            orig = getattr(exc, "orig", None)
            sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)

            is_foreign_key = (
                sqlstate == "23503"
                or "foreign key" in err_msg
                or "violates foreign key constraint" in err_msg
            )
            is_not_null = (
                sqlstate == "23502"
                or "not-null" in err_msg
                or "violates not-null constraint" in err_msg
            )
            is_unique = (
                sqlstate == "23505"
                or "unique" in err_msg
                or "duplicate" in err_msg
            )
            is_dedup_target = (
                "phone" in err_msg
                or "email" in err_msg
                or "uq_leads" in err_msg
                or "dedup" in err_msg
            )

            if is_unique and is_dedup_target and not (is_foreign_key or is_not_null):
                raise DuplicateLeadError(
                    "A lead with this phone number or email already exists in this agency."
                ) from exc

            # Do not mask foreign key, not null, check constraint, or other unrelated integrity errors
            raise

        return self._to_response(lead, norm_phone, norm_email)

    async def get_lead(self, lead_id: uuid.UUID) -> LeadResponse:
        """Fetch lead by ID with strict tenant isolation, agent assignment policy, and decrypt PII."""
        lead = await self._find_lead(lead_id)
        return self._to_response(lead)

    async def list_leads(
        self,
        page: int = 1,
        page_size: int = 20,
        stage: str | None = None,
        assigned_agent_id: uuid.UUID | None = None,
        lead_health: str | None = None,
        search: str | None = None,
    ) -> LeadListResponse:
        """Paginated list of leads for the tenant with role-based agent isolation and filters."""
        if self.context.is_super_admin():
            logger.info(
                "super_admin_lead_list_access",
                super_admin_id=str(self.context.user_id),
                tenant_id=str(self.context.tenant_id),
            )

        query = select(Lead).where(
            Lead.tenant_id == self.context.tenant_id,
            Lead.is_deleted.is_(False),
        )

        # Policy: Agents can view ONLY their assigned leads
        if self.context.role == UserRole.AGENT:
            if assigned_agent_id and assigned_agent_id != self.context.user_id:
                raise PermissionDeniedError("Agents can only access their assigned leads.")
            query = query.where(Lead.assigned_agent_id == self.context.user_id)
        else:
            if assigned_agent_id:
                query = query.where(Lead.assigned_agent_id == assigned_agent_id)

        if stage:
            query = query.where(Lead.stage == stage)
        if lead_health:
            query = query.where(Lead.lead_health == lead_health)
        if search:
            query = query.where(Lead.full_name.ilike(f"%{search}%"))

        # Total count
        count_stmt = select(func.count()).select_from(query.subquery())
        total_result = await self.db.execute(count_stmt)
        total_items = total_result.scalar_one()

        # Paginated fetch ordered by newest activity
        query = query.order_by(Lead.last_activity_at.desc().nullslast()).offset(
            (page - 1) * page_size
        ).limit(page_size)

        result = await self.db.execute(query)
        leads = result.scalars().all()

        items = [self._to_response(ld) for ld in leads]
        total_pages = math.ceil(total_items / page_size) if total_items > 0 else 0

        return LeadListResponse(
            data=items,
            total=total_items,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

    async def update_lead(self, lead_id: uuid.UUID, payload: LeadUpdateRequest) -> LeadResponse:
        """Update lead attributes and log activity within an explicit transaction boundary."""
        lead = await self._find_lead(lead_id)

        # Enforce assignment policy on updates
        if payload.assigned_agent_id is not None and payload.assigned_agent_id != lead.assigned_agent_id:
            if self.context.role == UserRole.AGENT:
                raise PermissionDeniedError("Agents are not authorized to reassign leads.")
            agent_check = await self.db.execute(
                select(User.id).where(
                    User.id == payload.assigned_agent_id,
                    User.tenant_id == self.context.tenant_id,
                    User.is_deleted.is_(False),
                )
            )
            if not agent_check.scalar_one_or_none():
                raise PermissionDeniedError("Assigned agent does not belong to this agency.")

        now = datetime.now(UTC)
        changes: dict[str, Any] = {}

        try:
            async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
                if payload.full_name is not None and payload.full_name != lead.full_name:
                    changes["full_name"] = {"from": lead.full_name, "to": payload.full_name}
                    lead.full_name = payload.full_name

                if payload.phone is not None:
                    norm_phone = normalize_phone(payload.phone)
                    phone_hash = compute_blind_hash(norm_phone)
                    if phone_hash != lead.phone_hash:
                        # Check duplicate within agency
                        dup_stmt = select(Lead).where(
                            Lead.tenant_id == self.context.tenant_id,
                            Lead.phone_hash == phone_hash,
                            Lead.id != lead.id,
                            Lead.is_deleted.is_(False),
                        )
                        dup_res = await self.db.execute(dup_stmt)
                        if dup_res.scalars().first():
                            raise DuplicateLeadError("Another lead already has this phone number.")
                        lead.phone_e164_enc = encrypt_field(norm_phone)
                        lead.phone_hash = phone_hash
                        changes["phone"] = "updated"

                if payload.email is not None:
                    norm_email = normalize_email(payload.email)
                    email_hash = compute_blind_hash(norm_email)
                    if email_hash != lead.email_hash:
                        # Check duplicate within agency
                        dup_stmt = select(Lead).where(
                            Lead.tenant_id == self.context.tenant_id,
                            Lead.email_hash == email_hash,
                            Lead.id != lead.id,
                            Lead.is_deleted.is_(False),
                        )
                        dup_res = await self.db.execute(dup_stmt)
                        if dup_res.scalars().first():
                            raise DuplicateLeadError("Another lead already has this email address.")
                        lead.email_enc = encrypt_field(norm_email)
                        lead.email_hash = email_hash
                        changes["email"] = "updated"

                for field in [
                    "preferred_contact",
                    "budget_min",
                    "budget_max",
                    "currency_code",
                    "property_type",
                    "bhk",
                    "preferred_location",
                    "purpose",
                    "move_in_timeline",
                    "notes",
                    "assigned_agent_id",
                    "do_not_contact",
                ]:
                    val = getattr(payload, field)
                    if val is not None and getattr(lead, field) != val:
                        changes[field] = {"from": getattr(lead, field), "to": val}
                        setattr(lead, field, val)

                if changes:
                    lead.last_activity_at = now
                    activity = LeadActivity(
                        id=uuid.uuid4(),
                        tenant_id=self.context.tenant_id,
                        lead_id=lead.id,
                        actor_id=self.context.user_id,
                        actor_type="USER",
                        activity_type="LEAD_UPDATED",
                        description=f"Lead fields updated: {', '.join(changes.keys())}",
                        metadata_={"changes": changes},
                        created_at=now,
                    )
                    self.db.add(activity)
                    await self.db.flush()
        except IntegrityError as exc:
            err_msg = str(exc).lower()
            orig = getattr(exc, "orig", None)
            sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)

            is_unique = (
                sqlstate == "23505"
                or "unique" in err_msg
                or "duplicate" in err_msg
            )
            is_dedup_target = (
                "phone" in err_msg
                or "email" in err_msg
                or "uq_leads" in err_msg
                or "dedup" in err_msg
            )
            if is_unique and is_dedup_target:
                raise DuplicateLeadError(
                    "Another lead already has this contact information."
                ) from exc
            raise

        return self._to_response(lead)

    async def assign_lead(self, lead_id: uuid.UUID, agent_id: uuid.UUID | None) -> LeadResponse:
        """Assign or reassign a lead to an agent (requires MANAGER or higher role)."""
        self.context.require_role(UserRole.MANAGER)
        lead = await self._find_lead(lead_id)

        if agent_id is not None:
            agent_check = await self.db.execute(
                select(User.id).where(
                    User.id == agent_id,
                    User.tenant_id == self.context.tenant_id,
                    User.is_deleted.is_(False),
                )
            )
            if not agent_check.scalar_one_or_none():
                raise PermissionDeniedError("Assigned agent does not belong to this agency.")

        old_agent_id = lead.assigned_agent_id
        if old_agent_id == agent_id:
            return self._to_response(lead)

        now = datetime.now(UTC)
        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            lead.assigned_agent_id = agent_id
            lead.last_activity_at = now

            activity = LeadActivity(
                id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                lead_id=lead.id,
                actor_id=self.context.user_id,
                actor_type="USER",
                activity_type="LEAD_ASSIGNED",
                description=f"Lead reassigned from {old_agent_id} to {agent_id}",
                metadata_={
                    "previous_agent_id": str(old_agent_id) if old_agent_id else None,
                    "new_agent_id": str(agent_id) if agent_id else None,
                },
                created_at=now,
            )
            self.db.add(activity)
            await self.db.flush()

        return self._to_response(lead)

    async def transition_stage(
        self,
        lead_id: uuid.UUID,
        target_stage: LeadStage,
        reason: str | None = None,
        is_system: bool = False,
    ) -> LeadResponse:
        """Enforce strict state machine transition rules and audit stage changes."""
        lead = await self._find_lead(lead_id)

        from_stage = LeadStage(lead.stage) if isinstance(lead.stage, str) else lead.stage
        to_stage = LeadStage(target_stage) if isinstance(target_stage, str) else target_stage

        # 1. State machine validation
        validate_transition(
            from_stage=from_stage,
            to_stage=to_stage,
            actor_role=self.context.role,
            is_system=is_system,
        )

        now = datetime.now(UTC)

        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            lead.stage = to_stage.value
            lead.last_activity_at = now

            # 2. Activity timeline entry
            activity = LeadActivity(
                id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                lead_id=lead.id,
                actor_id=self.context.user_id if not is_system else None,
                actor_type="SYSTEM" if is_system else "USER",
                activity_type="STAGE_CHANGED",
                description=f"Stage transitioned from {from_stage.value} to {to_stage.value}",
                metadata_={
                    "from_stage": from_stage.value,
                    "to_stage": to_stage.value,
                    "reason": reason,
                },
                created_at=now,
            )
            self.db.add(activity)
            await self.db.flush()

        return self._to_response(lead)

    async def get_activities(self, lead_id: uuid.UUID) -> list[LeadActivityResponse]:
        """Retrieve audit timeline for a lead."""
        await self._find_lead(lead_id)  # Validate lead existence & tenant/agent isolation

        stmt = (
            select(LeadActivity)
            .where(
                LeadActivity.lead_id == lead_id,
                LeadActivity.tenant_id == self.context.tenant_id,
            )
            .order_by(LeadActivity.created_at.desc())
        )
        result = await self.db.execute(stmt)
        activities = result.scalars().all()

        return [
            LeadActivityResponse(
                id=act.id,
                lead_id=act.lead_id,
                actor_id=act.actor_id,
                actor_type=act.actor_type,
                activity_type=act.activity_type,
                description=act.description,
                metadata=act.metadata_,
                created_at=act.created_at,
            )
            for act in activities
        ]

    async def soft_delete_lead(self, lead_id: uuid.UUID) -> None:
        """Soft delete a lead (requires MANAGER or higher role)."""
        self.context.require_role(UserRole.MANAGER)
        lead = await self._find_lead(lead_id)

        now = datetime.now(UTC)
        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            lead.is_deleted = True
            lead.deleted_at = now

            activity = LeadActivity(
                id=uuid.uuid4(),
                tenant_id=self.context.tenant_id,
                lead_id=lead.id,
                actor_id=self.context.user_id,
                actor_type="USER",
                activity_type="LEAD_DELETED",
                description="Lead soft-deleted",
                metadata_={},
                created_at=now,
            )
            self.db.add(activity)
            await self.db.flush()

    async def _find_lead(self, lead_id: uuid.UUID) -> Lead:
        """Helper to find lead with tenant isolation, agent assignment check, and active status check."""
        stmt = select(Lead).where(
            Lead.id == lead_id,
            Lead.tenant_id == self.context.tenant_id,
            Lead.is_deleted.is_(False),
        )
        result = await self.db.execute(stmt)
        lead = result.scalars().first()
        if not lead:
            raise NotFoundError(f"Lead {lead_id} not found.")

        # Policy: Agents can view and update only their assigned leads
        if self.context.role == UserRole.AGENT and lead.assigned_agent_id != self.context.user_id:
            raise PermissionDeniedError("Agents can only access leads assigned to them.")

        # Super-admin access audit
        if self.context.is_super_admin():
            logger.info(
                "super_admin_lead_access",
                super_admin_id=str(self.context.user_id),
                tenant_id=str(self.context.tenant_id),
                lead_id=str(lead.id),
            )

        return lead

    def _to_response(
        self,
        lead: Lead,
        cached_phone: str | None = None,
        cached_email: str | None = None,
    ) -> LeadResponse:
        """Convert ORM model to response schema, decrypting PII fields."""
        phone = cached_phone if cached_phone is not None else decrypt_field(lead.phone_e164_enc)
        email = cached_email if cached_email is not None else decrypt_field(lead.email_enc)

        return LeadResponse(
            id=lead.id,
            tenant_id=lead.tenant_id,
            external_id=lead.external_id,
            source=lead.source,
            full_name=lead.full_name,
            phone=phone,
            email=email,
            preferred_contact=lead.preferred_contact,
            budget_min=lead.budget_min,
            budget_max=lead.budget_max,
            currency_code=lead.currency_code,
            property_type=lead.property_type,
            bhk=lead.bhk,
            preferred_location=lead.preferred_location,
            purpose=lead.purpose,
            move_in_timeline=lead.move_in_timeline,
            stage=lead.stage,
            lead_health=lead.lead_health,
            intent_score=lead.intent_score,
            automation_status=lead.automation_status,
            assigned_agent_id=lead.assigned_agent_id,
            do_not_contact=lead.do_not_contact,
            is_duplicate_of=lead.is_duplicate_of,
            last_activity_at=lead.last_activity_at,
            score_breakdown=lead.score_breakdown,
            notes=lead.notes,
            created_at=lead.created_at,
            updated_at=lead.updated_at,
        )

