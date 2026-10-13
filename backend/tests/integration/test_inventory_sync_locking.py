"""
PostgreSQL integration tests for Google Sheets Inventory Synchronization Locking & Scheduling.
Runs against the dedicated test database (havn_test).

Verifies:
1. Eligible and ineligible tenant integrations filtering (active, non-deleted, spreadsheet_id present).
2. Atomic lease lock acquisition and mutual exclusion (two workers attempting same tenant).
3. Independent locking across multiple tenants (Tenant A and Tenant B sync simultaneously).
4. Unexpired lock rejection (active run protected).
5. Expired lease lock recovery (worker crash / timeout safety).
6. Safe lease release token verification (stale worker cannot overwrite newer state).
7. End-to-end scheduled batch execution with Celery task async runner.
8. One tenant failure isolation (failing tenant does not block healthy tenant).
9. Coordination between manual API sync and scheduled background sync (409 Conflict if locked).
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.integrations.google.inventory_sync import PropertyInventorySyncService, SyncLockLostError
from app.integrations.google.models import InventoryStagingItem, TenantGoogleIntegration
from app.integrations.google.service import GoogleIntegrationService
from app.integrations.sheets.client import MockGoogleSheetsClient
from app.properties.models import Property
from app.tasks.celery_tasks import _sync_all_active_google_sheets_inventory_async
from app.tenants.models import Tenant
from tests.conftest import test_engine

settings = get_settings()


def _make_session() -> AsyncSession:
    """Create an isolated AsyncSession bound to test_engine."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    return factory()


@pytest.mark.asyncio
class TestInventorySyncLockingAndScheduling:
    """Real PostgreSQL verification of Google Sheets sync locking, recovery, and isolation."""

    async def _create_tenant(self, session: AsyncSession, name: str) -> Tenant:
        tenant = Tenant(
            id=uuid.uuid4(),
            name=name,
            slug=f"sync-lock-{uuid.uuid4().hex[:8]}",
            timezone="Asia/Kolkata",
            currency_code="INR",
            is_active=True,
            settings={},
        )
        session.add(tenant)
        await session.flush()
        return tenant

    async def _create_integration(
        self,
        session: AsyncSession,
        tenant_id: uuid.UUID,
        spreadsheet_id: str | None = "sheet-test-1",
        is_active: bool = True,
        is_deleted: bool = False,
        last_sync_status: str | None = None,
        last_sync_stats: dict | None = None,
    ) -> TenantGoogleIntegration:
        integration = TenantGoogleIntegration(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            is_active=is_active,
            is_deleted=is_deleted,
            spreadsheet_id=spreadsheet_id,
            properties_sheet_name="Inventory",
            last_sync_status=last_sync_status,
            last_sync_stats=last_sync_stats,
        )
        session.add(integration)
        await session.commit()
        return integration

    # -------------------------------------------------------------------------
    # 1. Eligible vs Ineligible Tenant Filtering
    # -------------------------------------------------------------------------
    async def test_eligible_vs_ineligible_tenant_filtering(self, create_tables):
        """
        Only integrations with is_active=True, is_deleted=False, and non-null spreadsheet_id
        are returned by get_active_sync_tenant_ids.
        """
        async with _make_session() as session:
            # 1. Eligible tenant
            t_eligible = await self._create_tenant(session, "Eligible Tenant")
            await self._create_integration(session, t_eligible.id, spreadsheet_id="sheet-ok")

            # 2. Inactive integration
            t_inactive = await self._create_tenant(session, "Inactive Integration")
            await self._create_integration(session, t_inactive.id, is_active=False)

            # 3. Soft-deleted integration
            t_deleted = await self._create_tenant(session, "Deleted Integration")
            await self._create_integration(session, t_deleted.id, is_deleted=True)

            # 4. Missing spreadsheet_id
            t_no_sheet = await self._create_tenant(session, "No Sheet ID")
            await self._create_integration(session, t_no_sheet.id, spreadsheet_id=None)

            active_ids = await GoogleIntegrationService.get_active_sync_tenant_ids(session)

            assert t_eligible.id in active_ids
            assert t_inactive.id not in active_ids
            assert t_deleted.id not in active_ids
            assert t_no_sheet.id not in active_ids

    # -------------------------------------------------------------------------
    # 2. Atomic Lease Lock Acquisition and Mutual Exclusion
    # -------------------------------------------------------------------------
    async def test_concurrent_lock_acquisition_mutual_exclusion(self, create_tables):
        """
        Two workers concurrently attempt to acquire a sync lock on the same tenant integration.
        PostgreSQL FOR UPDATE row-level locking ensures exactly one acquires the lock;
        the other receives None (skipped).
        """
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Mutex Tenant")
            await self._create_integration(session, tenant.id, spreadsheet_id="sheet-mutex")

        async def attempt_lock():
            async with _make_session() as session:
                svc = GoogleIntegrationService(session, tenant.id)
                token = await svc.acquire_sync_lock(lock_ttl_seconds=300)
                await session.commit()
                return token

        token_a, token_b = await asyncio.gather(attempt_lock(), attempt_lock())

        # Exactly one must have acquired the lock, the other was skipped
        assert (token_a is not None and token_b is None) or (token_a is None and token_b is not None)
        winner_token = token_a or token_b
        assert winner_token is not None

        # Verify integration row status in database
        async with _make_session() as session:
            svc = GoogleIntegrationService(session, tenant.id)
            integration = await svc.get_integration()
            assert integration.last_sync_status == "SYNCING"
            assert integration.last_sync_stats.get("sync_lock_token") == winner_token

    # -------------------------------------------------------------------------
    # 3. Multi-Tenant Independent Locking
    # -------------------------------------------------------------------------
    async def test_multi_tenant_concurrent_locking_independence(self, create_tables):
        """
        Workers locking Tenant A and Tenant B concurrently succeed without blocking each other.
        """
        async with _make_session() as session:
            tenant_a = await self._create_tenant(session, "Tenant A")
            await self._create_integration(session, tenant_a.id, spreadsheet_id="sheet-a")

            tenant_b = await self._create_tenant(session, "Tenant B")
            await self._create_integration(session, tenant_b.id, spreadsheet_id="sheet-b")

        async def lock_tenant(t_id):
            async with _make_session() as session:
                svc = GoogleIntegrationService(session, t_id)
                token = await svc.acquire_sync_lock()
                await session.commit()
                return token

        token_a, token_b = await asyncio.gather(lock_tenant(tenant_a.id), lock_tenant(tenant_b.id))

        assert token_a is not None
        assert token_b is not None
        assert token_a != token_b

    # -------------------------------------------------------------------------
    # 4. Unexpired Lock Rejection
    # -------------------------------------------------------------------------
    async def test_active_unexpired_lock_is_respected(self, create_tables):
        """
        If a lock is held and has not expired, subsequent acquire attempts return None.
        """
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Active Lock Tenant")
            recent_time = (datetime.now(UTC) - timedelta(seconds=30)).isoformat()
            await self._create_integration(
                session,
                tenant.id,
                spreadsheet_id="sheet-active",
                last_sync_status="SYNCING",
                last_sync_stats={
                    "sync_started_at": recent_time,
                    "sync_lock_token": "active-worker-token",
                },
            )

        async with _make_session() as session:
            svc = GoogleIntegrationService(session, tenant.id)
            # lock_ttl_seconds = 600, so a 30s lock is active
            token = await svc.acquire_sync_lock(lock_ttl_seconds=600)
            await session.commit()
            assert token is None

    # -------------------------------------------------------------------------
    # 5. Expired Lease Recovery (Crash / Timeout Safety)
    # -------------------------------------------------------------------------
    async def test_expired_lease_lock_recovery(self, create_tables):
        """
        If a prior worker crashed or timed out, and sync_started_at exceeds lock_ttl_seconds,
        a new worker successfully takes over the lock.
        """
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Crashed Worker Tenant")
            # 15 minutes ago
            stale_time = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
            await self._create_integration(
                session,
                tenant.id,
                spreadsheet_id="sheet-crashed",
                last_sync_status="SYNCING",
                last_sync_stats={
                    "sync_started_at": stale_time,
                    "sync_lock_token": "dead-worker-token",
                },
            )

        async with _make_session() as session:
            svc = GoogleIntegrationService(session, tenant.id)
            # TTL = 600s (10 min). 15 min > 10 min -> expired
            token = await svc.acquire_sync_lock(lock_ttl_seconds=600)
            await session.commit()

            assert token is not None
            assert token != "dead-worker-token"

            integration = await svc.get_integration()
            assert integration.last_sync_status == "SYNCING"
            assert integration.last_sync_stats.get("sync_lock_token") == token

    # -------------------------------------------------------------------------
    # 6. Release Verification & Stale Worker Protection
    # -------------------------------------------------------------------------
    async def test_release_lock_token_validation(self, create_tables):
        """
        Valid lock token successfully transitions status and updates stats.
        A stale worker with an outdated token cannot overwrite the lock.
        """
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Token Validation Tenant")
            await self._create_integration(session, tenant.id, spreadsheet_id="sheet-val")

            svc = GoogleIntegrationService(session, tenant.id)
            valid_token = await svc.acquire_sync_lock()
            await session.commit()

        # 1. Stale worker attempts to release with incorrect token
        async with _make_session() as session:
            svc = GoogleIntegrationService(session, tenant.id)
            released = await svc.release_sync_lock(
                token="stale-old-token",
                final_status="SUCCESS",
            )
            await session.commit()
            assert released is False

            # Verify status is still SYNCING
            integration = await svc.get_integration()
            assert integration.last_sync_status == "SYNCING"

        # 2. Legitimate worker releases with valid token
        async with _make_session() as session:
            svc = GoogleIntegrationService(session, tenant.id)
            released = await svc.release_sync_lock(
                token=valid_token,
                final_status="SUCCESS",
                sync_stats={"created": 10, "updated": 2, "staged": 0},
            )
            await session.commit()
            assert released is True

            integration = await svc.get_integration()
            assert integration.last_sync_status == "SUCCESS"
            assert integration.last_sync_at is not None
            assert integration.last_sync_stats.get("created") == 10
            assert "sync_lock_token" not in integration.last_sync_stats

    # -------------------------------------------------------------------------
    # 7. End-to-End Scheduled Batch Execution
    # -------------------------------------------------------------------------
    async def test_end_to_end_scheduled_batch_execution(self, create_tables):
        """
        Runs _sync_all_active_google_sheets_inventory_async against PostgreSQL.
        Both tenants synchronize property rows into the database independently.
        """
        from unittest.mock import patch
        from app.integrations.sheets.service import _mock_sheets_singleton

        _mock_sheets_singleton.sheets.clear()

        async with _make_session() as session:
            t1 = await self._create_tenant(session, "Batch Tenant 1")
            await self._create_integration(session, t1.id, spreadsheet_id="sheet-batch-1")

            t2 = await self._create_tenant(session, "Batch Tenant 2")
            await self._create_integration(session, t2.id, spreadsheet_id="sheet-batch-2")

        # Mock data for Tenant 1
        _mock_sheets_singleton.seed_sheet(
            "sheet-batch-1",
            "Inventory",
            [
                ["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price"],
                ["Anil Sharma", "+919811111111", "Villa", "4 BHK", "480 sq.yd", "Whitefield", "2.5 Cr"],
            ],
        )

        # Mock data for Tenant 2
        _mock_sheets_singleton.seed_sheet(
            "sheet-batch-2",
            "Inventory",
            [
                ["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price"],
                ["Sunil Verma", "+919822222222", "Apartment", "2 BHK", "1200 sq ft", "Koramangala", "85 Lakhs"],
            ],
        )

        test_session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

        # Run scheduled sync against PostgreSQL test database
        with patch("app.tasks.celery_tasks._get_async_session", return_value=test_session_factory):
            stats = await _sync_all_active_google_sheets_inventory_async()

        assert stats["total_eligible"] >= 2
        assert stats["synced"] >= 2
        assert stats["failed"] == 0
        assert stats["skipped_locked"] == 0

        # Verify DB records created for each tenant
        async with _make_session() as session:
            t1_props = (
                await session.execute(
                    sa.select(Property).where(Property.tenant_id == t1.id)
                )
            ).scalars().all()
            assert len(t1_props) == 1
            assert t1_props[0].property_type == "VILLA"
            assert t1_props[0].price == 25_000_000

            t2_props = (
                await session.execute(
                    sa.select(Property).where(Property.tenant_id == t2.id)
                )
            ).scalars().all()
            assert len(t2_props) == 1
            assert t2_props[0].property_type == "APARTMENT"
            assert t2_props[0].price == 8_500_000

    # -------------------------------------------------------------------------
    # 8. Single Tenant Failure Isolation
    # -------------------------------------------------------------------------
    async def test_single_tenant_failure_isolation(self, create_tables):
        """
        One tenant's Google Sheet fails (e.g. invalid permissions/sheet missing).
        The failing tenant is recorded as FAILED, but the other tenant succeeds.
        """
        from unittest.mock import patch
        from app.integrations.sheets.client import SpreadsheetAccessError
        from app.integrations.sheets.service import _mock_sheets_singleton

        _mock_sheets_singleton.sheets.clear()

        async with _make_session() as session:
            t_fail = await self._create_tenant(session, "Failing Tenant")
            await self._create_integration(session, t_fail.id, spreadsheet_id="sheet-fail")

            t_pass = await self._create_tenant(session, "Passing Tenant")
            await self._create_integration(session, t_pass.id, spreadsheet_id="sheet-pass")

        # Pass tenant has mock data
        _mock_sheets_singleton.seed_sheet(
            "sheet-pass",
            "Inventory",
            [
                ["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price"],
                ["Park View Residencies", "+919833333333", "Apartment", "3 BHK", "1800 sq ft", "Indiranagar", "1.2 Cr"],
            ],
        )

        orig_get_rows = _mock_sheets_singleton.get_rows

        async def selective_get_rows(spreadsheet_id, range_name):
            if spreadsheet_id == "sheet-fail":
                raise SpreadsheetAccessError("HTTP 403 Forbidden: Spreadsheet not shared with service account")
            return await orig_get_rows(spreadsheet_id, range_name)

        test_session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

        with patch("app.tasks.celery_tasks._get_async_session", return_value=test_session_factory), patch.object(
            _mock_sheets_singleton, "get_rows", side_effect=selective_get_rows
        ):
            stats = await _sync_all_active_google_sheets_inventory_async()

        assert stats["failed"] >= 1
        assert stats["synced"] >= 1

        # Check DB sync statuses
        async with _make_session() as session:
            svc_fail = GoogleIntegrationService(session, t_fail.id)
            int_fail = await svc_fail.get_integration()
            assert int_fail.last_sync_status == "FAILED"
            assert int_fail.last_sync_error is not None
            assert "403" in int_fail.last_sync_error

            svc_pass = GoogleIntegrationService(session, t_pass.id)
            int_pass = await svc_pass.get_integration()
            assert int_pass.last_sync_status == "SUCCESS"
            assert int_pass.last_sync_error is None

    # -------------------------------------------------------------------------
    # 9. Manual Sync vs Scheduled Sync Coordination
    # -------------------------------------------------------------------------
    async def test_manual_sync_respects_background_lock(self, create_tables):
        """
        When background sync holds the lock, a manual sync attempt cannot acquire it
        (simulating 409 Conflict). Once released, manual sync succeeds.
        """
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Coordination Tenant")
            await self._create_integration(session, tenant.id, spreadsheet_id="sheet-coord")

        # 1. Background sync acquires lock
        async with _make_session() as bg_session:
            bg_svc = GoogleIntegrationService(bg_session, tenant.id)
            bg_token = await bg_svc.acquire_sync_lock()
            await bg_session.commit()
            assert bg_token is not None

        # 2. Manual sync attempts to acquire lock while held
        async with _make_session() as manual_session:
            manual_svc = GoogleIntegrationService(manual_session, tenant.id)
            manual_token = await manual_svc.acquire_sync_lock()
            await manual_session.commit()
            assert manual_token is None  # Blocked / 409 Conflict

        # 3. Background sync releases lock
        async with _make_session() as bg_session:
            bg_svc = GoogleIntegrationService(bg_session, tenant.id)
            await bg_svc.release_sync_lock(bg_token, "SUCCESS")
            await bg_session.commit()

        # 4. Manual sync can now acquire lock
        async with _make_session() as manual_session:
            manual_svc = GoogleIntegrationService(manual_session, tenant.id)
            new_manual_token = await manual_svc.acquire_sync_lock()
            await manual_session.commit()
            assert new_manual_token is not None
            await manual_svc.release_sync_lock(new_manual_token, "SUCCESS")
            await manual_session.commit()

    # -------------------------------------------------------------------------
    # 10. Fix A — Missing or Whitespace Location Staged without Canonical Property
    # -------------------------------------------------------------------------
    async def test_fix_a_missing_and_whitespace_location_routed_to_staging(self, create_tables):
        """
        Fix A verification:
        Rows with missing, blank, or whitespace-only locations must NOT become canonical properties.
        They must enter InventoryStagingItem with status PENDING_REVIEW, preserving original row data.
        Valid locations import normally. Repeated synchronization does not create duplicate staging rows.
        """
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Fix A Location Tenant")
            integration = await self._create_integration(session, tenant.id, spreadsheet_id="sheet-fix-a")

            mock_sheets = MockGoogleSheetsClient()
            sheet_rows = [
                ["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price"],
                # Row 2: Valid property with proper location
                ["Valid Owner", "+919811111111", "Villa", "4 BHK", "400 sq.yd", "Balbir Road Dalanwala", "4.5 Cr"],
                # Row 3: Missing (empty) location
                ["Empty Loc Owner", "+919822222222", "Apartment", "3 BHK", "1500 sq ft", "", "80 Lakh"],
                # Row 4: Whitespace-only location
                ["Whitespace Owner", "+919833333333", "Apartment", "2 BHK", "1000 sq ft", "   ", "50 Lakh"],
            ]
            mock_sheets.seed_sheet("sheet-fix-a", "Inventory", sheet_rows)

            sync_svc = PropertyInventorySyncService(session, tenant.id, sheets_client=mock_sheets)
            res1 = await sync_svc.sync_inventory()
            await session.commit()

            assert res1.total_rows == 3
            assert res1.created == 1
            assert res1.staged == 2
            assert res1.error_count == 2
            assert any("missing property location" in err["error"].lower() for err in res1.errors)

            # Query Canonical Properties: Exactly 1 valid property created, none for bad locations
            props = (await session.execute(
                sa.select(Property).where(Property.tenant_id == tenant.id)
            )).scalars().all()
            assert len(props) == 1
            assert props[0].location_text == "Balbir Road Dalanwala"
            assert props[0].owner_name == "Valid Owner"

            # Query InventoryStagingItems: Exactly 2 staged items with PENDING_REVIEW
            staged_items = (await session.execute(
                sa.select(InventoryStagingItem).where(
                    InventoryStagingItem.tenant_id == tenant.id,
                    InventoryStagingItem.is_deleted.is_(False),
                ).order_by(InventoryStagingItem.row_index.asc())
            )).scalars().all()
            assert len(staged_items) == 2

            # Verify Staged Item 1 (empty location)
            assert staged_items[0].row_index == 3
            assert staged_items[0].status == "PENDING_REVIEW"
            assert staged_items[0].owner_name == "Empty Loc Owner"
            assert any("location" in err.lower() for err in staged_items[0].validation_errors)
            assert staged_items[0].raw_data["Area"] == ""

            # Verify Staged Item 2 (whitespace location)
            assert staged_items[1].row_index == 4
            assert staged_items[1].status == "PENDING_REVIEW"
            assert staged_items[1].owner_name == "Whitespace Owner"
            assert any("location" in err.lower() for err in staged_items[1].validation_errors)

            # Idempotency check: Repeated synchronization updates existing staging records in-place
            res2 = await sync_svc.sync_inventory()
            await session.commit()
            assert res2.created == 0
            assert res2.updated == 1
            assert res2.staged == 2

            props_after = (await session.execute(
                sa.select(Property).where(Property.tenant_id == tenant.id)
            )).scalars().all()
            assert len(props_after) == 1

            staged_after = (await session.execute(
                sa.select(InventoryStagingItem).where(
                    InventoryStagingItem.tenant_id == tenant.id,
                    InventoryStagingItem.is_deleted.is_(False),
                )
            )).scalars().all()
            assert len(staged_after) == 2

    # -------------------------------------------------------------------------
    # 11. Fix B — Manual Sync Commits Lease Acquisition Before Sheets Call
    # -------------------------------------------------------------------------
    async def test_fix_b_manual_sync_commits_lease_before_sheets_call(self, create_tables):
        """
        Fix B verification:
        Manual sync acquires the lease and commits the acquisition transaction
        BEFORE making external calls to Google Sheets.
        While the Sheets call is running, competing workers can inspect the row without
        being blocked behind an uncommitted transaction, and receive None (409 Conflict).
        """
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Fix B Lease Commit Tenant")
            await self._create_integration(session, tenant.id, spreadsheet_id="sheet-fix-b")

        # Worker 1 acquires sync lease and commits it immediately (simulating router.py behavior)
        async with _make_session() as session_w1:
            svc_w1 = GoogleIntegrationService(session_w1, tenant.id)
            token_w1 = await svc_w1.acquire_sync_lock(lock_ttl_seconds=300)
            assert token_w1 is not None
            # Immediate commit before network call
            await session_w1.commit()

        # While Worker 1 is conceptually performing external Sheets network I/O:
        # Worker 2 attempts to acquire the lock concurrently in a separate session
        async with _make_session() as session_w2:
            svc_w2 = GoogleIntegrationService(session_w2, tenant.id)
            token_w2 = await svc_w2.acquire_sync_lock(lock_ttl_seconds=300)
            await session_w2.commit()
            # Worker 2 is immediately rejected (returns None -> 409 Conflict) without blocking
            assert token_w2 is None

        # Verify integration status in DB is committed as SYNCING with token_w1
        async with _make_session() as check_session:
            svc = GoogleIntegrationService(check_session, tenant.id)
            integ = await svc.get_integration()
            assert integ.last_sync_status == "SYNCING"
            assert integ.last_sync_stats["sync_lock_token"] == token_w1

        # Worker 1 finishes and releases the lock
        async with _make_session() as session_w1:
            svc_w1 = GoogleIntegrationService(session_w1, tenant.id)
            released = await svc_w1.release_sync_lock(token_w1, final_status="SUCCESS")
            await session_w1.commit()
            assert released is True

        # Now Worker 2 can acquire the lock
        async with _make_session() as session_w2:
            svc_w2 = GoogleIntegrationService(session_w2, tenant.id)
            token_w2 = await svc_w2.acquire_sync_lock(lock_ttl_seconds=300)
            await session_w2.commit()
            assert token_w2 is not None
            await svc_w2.release_sync_lock(token_w2, final_status="SUCCESS")
            await session_w2.commit()

    # -------------------------------------------------------------------------
    # 12. Fix C — Safe Lease Renewal for Long-Running Syncs
    # -------------------------------------------------------------------------
    async def test_fix_c_lease_renewal_prevents_takeover_and_handles_stale_workers(self, create_tables):
        """
        Fix C verification:
        1. A sync running beyond initial window renews its lease and cannot be taken over prematurely.
        2. A genuinely expired abandoned lease can be reclaimed.
        3. An old token cannot renew a reclaimed lease.
        4. An old token cannot release or finalize a newer lease.
        5. A lost lease causes SyncLockLostError, immediately halting further processing.
        """
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Fix C Renewal Tenant")
            await self._create_integration(session, tenant.id, spreadsheet_id="sheet-fix-c")

        # Step 1: Worker 1 acquires lock with a short TTL (2 seconds)
        short_ttl = 2
        async with _make_session() as s1:
            svc1 = GoogleIntegrationService(s1, tenant.id)
            token_1 = await svc1.acquire_sync_lock(lock_ttl_seconds=short_ttl)
            await s1.commit()
            assert token_1 is not None

        # Step 2: Worker 1 renews the lock at 1 second (before 2s expiry)
        await asyncio.sleep(1.0)
        async with _make_session() as s1:
            svc1 = GoogleIntegrationService(s1, tenant.id)
            renewed = await svc1.renew_sync_lock(token_1, lock_ttl_seconds=short_ttl)
            await s1.commit()
            assert renewed is True

        # Step 3: Competing Worker 2 attempts takeover at 1.5s total elapsed.
        # Since Worker 1 renewed, the lease is still valid and Worker 2 must be rejected.
        await asyncio.sleep(0.5)
        async with _make_session() as s2:
            svc2 = GoogleIntegrationService(s2, tenant.id)
            token_2_early = await svc2.acquire_sync_lock(lock_ttl_seconds=short_ttl)
            await s2.commit()
            assert token_2_early is None  # Blocked / renewal protected Worker 1!

        # Step 4: Worker 1 abandons execution (sleeps past 2s from renewal).
        # Competing Worker 2 attempts takeover after lease has genuinely expired (>2s).
        await asyncio.sleep(2.1)
        async with _make_session() as s2:
            svc2 = GoogleIntegrationService(s2, tenant.id)
            token_2 = await svc2.acquire_sync_lock(lock_ttl_seconds=short_ttl)
            await s2.commit()
            assert token_2 is not None
            assert token_2 != token_1

        # Step 5: Stale Worker 1 attempts to renew the reclaimed lease with old token.
        # Renewal must be denied!
        async with _make_session() as s1:
            svc1 = GoogleIntegrationService(s1, tenant.id)
            stale_renew = await svc1.renew_sync_lock(token_1, lock_ttl_seconds=short_ttl)
            await s1.commit()
            assert stale_renew is False

        # Step 6: Stale Worker 1 attempts to release or overwrite the newer owner's state.
        # Release must be rejected!
        async with _make_session() as s1:
            svc1 = GoogleIntegrationService(s1, tenant.id)
            stale_release = await svc1.release_sync_lock(token_1, final_status="FAILED")
            await s1.commit()
            assert stale_release is False

        # Verify in DB that Worker 2's lock is still intact
        async with _make_session() as check_session:
            svc = GoogleIntegrationService(check_session, tenant.id)
            integ = await svc.get_integration()
            assert integ.last_sync_status == "SYNCING"
            assert integ.last_sync_stats["sync_lock_token"] == token_2

        # Step 7: Worker 2 finishes and releases cleanly
        async with _make_session() as s2:
            svc2 = GoogleIntegrationService(s2, tenant.id)
            clean_release = await svc2.release_sync_lock(token_2, final_status="SUCCESS")
            await s2.commit()
            assert clean_release is True

    async def test_fix_c_sync_service_lost_lease_aborts_processing(self, create_tables):
        """
        Verify that PropertyInventorySyncService raises SyncLockLostError and stops
        further writes when its lease cannot be renewed.
        """
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Fix C Abort Tenant")
            await self._create_integration(session, tenant.id, spreadsheet_id="sheet-abort")

            # Acquire lock
            svc = GoogleIntegrationService(session, tenant.id)
            token = await svc.acquire_sync_lock(lock_ttl_seconds=1)
            await session.commit()

        # Simulate expiration by waiting > 1s
        await asyncio.sleep(1.2)

        async with _make_session() as session:
            # Another worker reclaims lease
            svc2 = GoogleIntegrationService(session, tenant.id)
            new_token = await svc2.acquire_sync_lock(lock_ttl_seconds=300)
            await session.commit()
            assert new_token is not None

        # Original worker attempts to run with stale token and short TTL
        async with _make_session() as session:
            mock_sheets = MockGoogleSheetsClient()
            mock_sheets.seed_sheet(
                "sheet-abort",
                "Inventory",
                [
                    ["Owner name", "Owner number", "Property type", "Size", "Area", "Asking price"],
                    ["Owner 1", "+919811111111", "Apartment", "1000 sq ft", "Whitefield", "50 Lakh"],
                ],
            )
            sync_svc = PropertyInventorySyncService(
                session,
                tenant.id,
                sheets_client=mock_sheets,
                lock_token=token,
                lock_ttl_seconds=1,
            )

            # When sync_svc runs, lease renewal fails (stolen token) and SyncLockLostError is raised
            with pytest.raises(SyncLockLostError):
                await sync_svc.sync_inventory()

        # Verify DB: the newer worker's lock was NOT overwritten by the aborted worker
        async with _make_session() as session:
            svc = GoogleIntegrationService(session, tenant.id)
            integ = await svc.get_integration()
            assert integ.last_sync_status == "SYNCING"
            assert integ.last_sync_stats["sync_lock_token"] == new_token
            # No properties created by the aborted sync
            props = (await session.execute(
                sa.select(Property).where(Property.tenant_id == tenant.id)
            )).scalars().all()
            assert len(props) == 0

    async def test_get_rows_lease_loss_stops_stale_worker(self, create_tables):
        """Verify that losing the lease during or after get_rows halts execution immediately without writing."""
        async with _make_session() as session:
            tenant = await self._create_tenant(session, "Tenant Stale Worker")
            await self._create_integration(session, tenant.id, spreadsheet_id="stale-worker-sheet")
            google_svc = GoogleIntegrationService(session, tenant.id)
            token = await google_svc.acquire_sync_lock(lock_ttl_seconds=10)
            assert token is not None
            await session.commit()

        # Simulate another worker taking over the lease during the fetch
        async with _make_session() as session:
            google_svc = GoogleIntegrationService(session, tenant.id)
            # Force expire previous lock and let new worker acquire it
            integ = await google_svc.get_integration()
            stats = dict(integ.last_sync_stats or {})
            stats["sync_started_at"] = (datetime.now(UTC) - timedelta(seconds=20)).isoformat()
            integ.last_sync_stats = stats
            await session.commit()

            new_token = await google_svc.acquire_sync_lock(lock_ttl_seconds=300)
            assert new_token is not None
            assert new_token != token
            await session.commit()

        # Now stale worker attempts to finish sync with old token
        async with _make_session() as session:
            mock_sheets = MockGoogleSheetsClient()
            mock_sheets.seed_sheet(
                "stale-worker-sheet",
                "Properties",
                [
                    ["Property ID", "Property type", "Asking price", "Area"],
                    ["PROP-STALE-1", "Apartment", "50 Lakh", "Indiranagar"],
                ],
            )
            sync_svc = PropertyInventorySyncService(
                session,
                tenant.id,
                sheets_client=mock_sheets,
                lock_token=token,
                lock_ttl_seconds=10,
            )

            with pytest.raises(SyncLockLostError):
                await sync_svc.sync_inventory()

        # Confirm no property was created by stale worker and lock state belongs to new worker
        async with _make_session() as session:
            google_svc = GoogleIntegrationService(session, tenant.id)
            integ = await google_svc.get_integration()
            assert integ.last_sync_status == "SYNCING"
            assert integ.last_sync_stats["sync_lock_token"] == new_token

            props = (await session.execute(
                sa.select(Property).where(Property.tenant_id == tenant.id)
            )).scalars().all()
            assert len(props) == 0
