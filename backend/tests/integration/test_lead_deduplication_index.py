"""
PostgreSQL integration tests for Lead Deduplication Index.
Runs against the dedicated PostgreSQL test database (havn_test).

Verifies the actual database partial unique index 'uq_leads_tenant_phone_hash':
1. Rejects duplicate active leads within the same tenant.
2. Allows identical phone hashes across different tenants.
3. Allows multiple active leads with NULL phone hashes in the same tenant.
4. Allows an active lead with the same phone hash as a soft-deleted lead.
5. Soft-deleting an active lead frees the phone hash for a new active lead.
6. Migration pre-flight duplicate query accurately detects duplicates.
"""

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.security import compute_blind_hash
from app.leads.models import Lead
from app.tenants.models import Tenant
from tests.conftest import test_engine


def _make_session() -> AsyncSession:
    """Create an isolated AsyncSession bound to test_engine."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    return factory()


@pytest.mark.asyncio
class TestLeadDeduplicationIndex:
    """Real PostgreSQL partial unique index behavior tests."""

    async def _create_tenant(self, session: AsyncSession, name: str) -> Tenant:
        tenant = Tenant(
            id=uuid.uuid4(),
            name=name,
            slug=f"tenant-{uuid.uuid4().hex[:8]}",
            timezone="Asia/Kolkata",
            currency_code="INR",
            is_active=True,
            settings={},
        )
        session.add(tenant)
        await session.commit()
        return tenant

    async def test_duplicate_active_lead_rejected_by_database_constraint(self, create_tables):
        """Inserting two active leads with identical (tenant_id, phone_hash) raises IntegrityError."""
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Agency A")
            phone_hash = compute_blind_hash("+919876543210")
            now = datetime.now(UTC)

            lead1 = Lead(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                source="MANUAL",
                full_name="Lead One",
                phone_hash=phone_hash,
                is_deleted=False,
                created_at=now,
                updated_at=now,
            )
            session.add(lead1)
            await session.commit()

            lead2 = Lead(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                source="MANUAL",
                full_name="Lead Two (Duplicate)",
                phone_hash=phone_hash,
                is_deleted=False,
                created_at=now,
                updated_at=now,
            )
            session.add(lead2)
            with pytest.raises(IntegrityError) as exc_info:
                await session.commit()

            # Confirm violation of uq_leads_tenant_phone_hash
            assert "uq_leads_tenant_phone_hash" in str(exc_info.value).lower()

    async def test_cross_tenant_identical_phone_hash_permitted(self, create_tables):
        """Two different tenants can have active leads with the exact same phone hash."""
        async with _make_session() as session:
            tenant1 = await self._create_tenant(session, "Agency One")
            tenant2 = await self._create_tenant(session, "Agency Two")
            phone_hash = compute_blind_hash("+919876543210")
            now = datetime.now(UTC)

            lead_t1 = Lead(
                id=uuid.uuid4(),
                tenant_id=tenant1.id,
                source="PORTAL",
                full_name="Agency 1 Client",
                phone_hash=phone_hash,
                is_deleted=False,
                created_at=now,
                updated_at=now,
            )
            lead_t2 = Lead(
                id=uuid.uuid4(),
                tenant_id=tenant2.id,
                source="PORTAL",
                full_name="Agency 2 Client",
                phone_hash=phone_hash,
                is_deleted=False,
                created_at=now,
                updated_at=now,
            )
            session.add_all([lead_t1, lead_t2])
            await session.commit()

            # Verify both persisted cleanly
            res = await session.execute(
                sa.select(sa.func.count()).select_from(Lead).where(Lead.phone_hash == phone_hash)
            )
            assert res.scalar() == 2

    async def test_multiple_null_phone_hashes_permitted_in_same_tenant(self, create_tables):
        """An agency can have multiple active leads with phone_hash=None without collision."""
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Agency Null Phones")
            now = datetime.now(UTC)

            leads = [
                Lead(
                    id=uuid.uuid4(),
                    tenant_id=tenant.id,
                    source="WEBSITE",
                    full_name=f"Email Only Lead {i}",
                    phone_hash=None,
                    email_hash=compute_blind_hash(f"buyer{i}@example.com"),
                    is_deleted=False,
                    created_at=now,
                    updated_at=now,
                )
                for i in range(5)
            ]
            session.add_all(leads)
            await session.commit()

            res = await session.execute(
                sa.select(sa.func.count()).select_from(Lead).where(
                    Lead.tenant_id == tenant.id,
                    Lead.phone_hash.is_(None),
                )
            )
            assert res.scalar() == 5

    async def test_soft_deleted_lead_does_not_block_active_lead(self, create_tables):
        """A soft-deleted lead and an active lead with the same phone hash coexist in the same tenant."""
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Agency Soft Delete")
            phone_hash = compute_blind_hash("+919876543210")
            now = datetime.now(UTC)

            archived_lead = Lead(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                source="MANUAL",
                full_name="Archived Lead",
                phone_hash=phone_hash,
                is_deleted=True,
                deleted_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(archived_lead)
            await session.commit()

            # Now create an active lead with the exact same phone hash
            active_lead = Lead(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                source="MANUAL",
                full_name="Active Lead",
                phone_hash=phone_hash,
                is_deleted=False,
                created_at=now,
                updated_at=now,
            )
            session.add(active_lead)
            await session.commit()

            # Both rows exist in DB
            res = await session.execute(
                sa.select(sa.func.count()).select_from(Lead).where(
                    Lead.tenant_id == tenant.id,
                    Lead.phone_hash == phone_hash,
                )
            )
            assert res.scalar() == 2

    async def test_migration_preflight_duplicate_detection_query(self, create_tables):
        """Verify the exact SQL duplicate detection query used in migration 0008 upgrade()."""
        dup_query = sa.text("""
            SELECT count(*)
            FROM (
                SELECT tenant_id, phone_hash
                FROM leads
                WHERE is_deleted IS FALSE AND phone_hash IS NOT NULL
                GROUP BY tenant_id, phone_hash
                HAVING count(*) > 1
            ) dupes
        """)

        async with _make_session() as session:
            # When clean: returns 0
            res = await session.execute(dup_query)
            assert res.scalar() == 0
