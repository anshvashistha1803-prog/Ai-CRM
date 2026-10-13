"""
Unit tests for scheduled Google Sheets inventory synchronization:
- Celery task routing and Beat schedule configuration
- Eligible vs ineligible tenant filtering
- Multi-tenant batch processing with isolated error boundaries
- Skipping already-locked tenant integrations
- Worker failure handling and status updates
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import get_settings
from app.tasks.celery_app import celery_app
from app.tasks.celery_tasks import (
    _sync_all_active_google_sheets_inventory_async,
    sync_all_active_google_sheets_inventory,
)

settings = get_settings()


def test_celery_task_routing_and_beat_schedule_configuration():
    """Verify Celery task is routed to the default worker queue and registered in Beat."""
    task_name = "app.tasks.celery_tasks.sync_all_active_google_sheets_inventory"

    # Task route
    route = celery_app.conf.task_routes.get(task_name)
    assert route is not None, f"Route for {task_name} is missing!"
    assert route.get("queue") == "default"

    # Beat schedule
    beat = celery_app.conf.beat_schedule.get("sync-google-sheets-inventory-hourly")
    assert beat is not None, "Beat schedule 'sync-google-sheets-inventory-hourly' is missing!"
    assert beat["task"] == task_name
    assert beat["schedule"] == float(settings.google_sheets_sync_interval_seconds)
    assert beat["options"]["queue"] == "default"


@pytest.mark.asyncio
async def test_scheduled_sync_zero_eligible_tenants():
    """When no tenants have active Google Sheets integrations, completes cleanly with 0 counts."""
    mock_session = AsyncMock()

    with patch("app.tasks.celery_tasks._get_async_session") as mock_session_factory, patch(
        "app.integrations.google.service.GoogleIntegrationService.get_active_sync_tenant_ids",
        new_callable=AsyncMock,
        return_value=[],
    ):
        mock_session_factory.return_value.__aenter__.return_value = mock_session
        res = await _sync_all_active_google_sheets_inventory_async()

        assert res["total_eligible"] == 0
        assert res["synced"] == 0
        assert res["skipped_locked"] == 0
        assert res["failed"] == 0


@pytest.mark.asyncio
async def test_scheduled_sync_multiple_tenants_success():
    """Multiple eligible tenants are synchronized independently and counted."""
    tenant_1 = uuid.uuid4()
    tenant_2 = uuid.uuid4()

    mock_sync_result = MagicMock()
    mock_sync_result.status = "SUCCESS"
    mock_sync_result.created = 5
    mock_sync_result.updated = 2
    mock_sync_result.staged = 0
    mock_sync_result.error_count = 0

    mock_service_1 = AsyncMock()
    mock_service_1.acquire_sync_lock.return_value = "token-1"
    mock_service_1.release_sync_lock.return_value = True

    mock_service_2 = AsyncMock()
    mock_service_2.acquire_sync_lock.return_value = "token-2"
    mock_service_2.release_sync_lock.return_value = True

    def service_factory(db, tenant_id):
        return mock_service_1 if tenant_id == tenant_1 else mock_service_2

    mock_service_cls = MagicMock(side_effect=service_factory)
    mock_service_cls.get_active_sync_tenant_ids = AsyncMock(return_value=[tenant_1, tenant_2])

    with patch("app.tasks.celery_tasks._get_async_session") as mock_session_factory, patch(
        "app.tasks.celery_tasks.GoogleIntegrationService",
        new=mock_service_cls,
    ), patch(
        "app.tasks.celery_tasks.PropertyInventorySyncService.sync_inventory",
        new_callable=AsyncMock,
        return_value=mock_sync_result,
    ):
        mock_session_factory.return_value.__aenter__.return_value = AsyncMock()
        res = await _sync_all_active_google_sheets_inventory_async()

        assert res["total_eligible"] == 2
        assert res["synced"] == 2
        assert res["skipped_locked"] == 0
        assert res["failed"] == 0

        assert mock_service_1.acquire_sync_lock.called
        assert mock_service_1.release_sync_lock.called
        assert mock_service_2.acquire_sync_lock.called
        assert mock_service_2.release_sync_lock.called


@pytest.mark.asyncio
async def test_scheduled_sync_one_tenant_failing_others_continue():
    """Failure for Tenant 1 does not halt processing; Tenant 2 completes successfully."""
    tenant_1 = uuid.uuid4()
    tenant_2 = uuid.uuid4()

    mock_sync_result_2 = MagicMock()
    mock_sync_result_2.status = "SUCCESS"
    mock_sync_result_2.created = 3
    mock_sync_result_2.updated = 0
    mock_sync_result_2.staged = 0
    mock_sync_result_2.error_count = 0

    mock_service_1 = AsyncMock()
    mock_service_1.acquire_sync_lock.return_value = "token-1"
    mock_service_1.release_sync_lock.return_value = True

    mock_service_2 = AsyncMock()
    mock_service_2.acquire_sync_lock.return_value = "token-2"
    mock_service_2.release_sync_lock.return_value = True

    def service_factory(db, tenant_id):
        return mock_service_1 if tenant_id == tenant_1 else mock_service_2

    mock_service_cls = MagicMock(side_effect=service_factory)
    mock_service_cls.get_active_sync_tenant_ids = AsyncMock(return_value=[tenant_1, tenant_2])

    async def sync_side_effect(self):
        if self.tenant_id == tenant_1:
            raise RuntimeError("Google Sheets API 503 Service Unavailable")
        return mock_sync_result_2

    with patch("app.tasks.celery_tasks._get_async_session") as mock_session_factory, patch(
        "app.tasks.celery_tasks.GoogleIntegrationService",
        new=mock_service_cls,
    ), patch(
        "app.tasks.celery_tasks.PropertyInventorySyncService.sync_inventory",
        side_effect=sync_side_effect,
        autospec=True,
    ):
        mock_session_factory.return_value.__aenter__.return_value = AsyncMock()
        res = await _sync_all_active_google_sheets_inventory_async()

        assert res["total_eligible"] == 2
        assert res["synced"] == 1
        assert res["failed"] == 1
        assert res["skipped_locked"] == 0

        # Tenant 1 lock released with FAILED status
        mock_service_1.release_sync_lock.assert_called_once()
        assert mock_service_1.release_sync_lock.call_args.kwargs.get("final_status") == "FAILED"
        assert "503" in mock_service_1.release_sync_lock.call_args.kwargs.get("error_message")

        # Tenant 2 completed normally
        mock_service_2.release_sync_lock.assert_called_once()
        assert mock_service_2.release_sync_lock.call_args.kwargs.get("final_status") == "SUCCESS"


@pytest.mark.asyncio
async def test_scheduled_sync_skips_actively_locked_tenant():
    """When acquire_sync_lock returns None, tenant is safely skipped without syncing."""
    tenant_1 = uuid.uuid4()

    mock_service = AsyncMock()
    mock_service.acquire_sync_lock.return_value = None  # Already locked by active sync

    mock_service_cls = MagicMock(return_value=mock_service)
    mock_service_cls.get_active_sync_tenant_ids = AsyncMock(return_value=[tenant_1])

    with patch("app.tasks.celery_tasks._get_async_session") as mock_session_factory, patch(
        "app.tasks.celery_tasks.GoogleIntegrationService",
        new=mock_service_cls,
    ), patch(
        "app.tasks.celery_tasks.PropertyInventorySyncService.sync_inventory",
        new_callable=AsyncMock,
    ) as mock_sync:
        mock_session_factory.return_value.__aenter__.return_value = AsyncMock()
        res = await _sync_all_active_google_sheets_inventory_async()

        assert res["total_eligible"] == 1
        assert res["synced"] == 0
        assert res["skipped_locked"] == 1
        assert res["failed"] == 0

        assert not mock_sync.called
        assert not mock_service.release_sync_lock.called


@pytest.mark.asyncio
async def test_scheduled_sync_repeated_runs():
    """Sequential scheduled runs execute cleanly without retaining old locks."""
    tenant_1 = uuid.uuid4()

    mock_sync_result = MagicMock(status="SUCCESS", created=1, updated=1, staged=0, error_count=0)
    mock_service = AsyncMock()
    mock_service.acquire_sync_lock.return_value = "token-run-1"
    mock_service.release_sync_lock.return_value = True

    mock_service_cls = MagicMock(return_value=mock_service)
    mock_service_cls.get_active_sync_tenant_ids = AsyncMock(return_value=[tenant_1])

    with patch("app.tasks.celery_tasks._get_async_session") as mock_session_factory, patch(
        "app.tasks.celery_tasks.GoogleIntegrationService",
        new=mock_service_cls,
    ), patch(
        "app.tasks.celery_tasks.PropertyInventorySyncService.sync_inventory",
        new_callable=AsyncMock,
        return_value=mock_sync_result,
    ):
        mock_session_factory.return_value.__aenter__.return_value = AsyncMock()
        # Run 1
        res_1 = await _sync_all_active_google_sheets_inventory_async()
        assert res_1["synced"] == 1

        # Run 2
        mock_service.acquire_sync_lock.return_value = "token-run-2"
        res_2 = await _sync_all_active_google_sheets_inventory_async()
        assert res_2["synced"] == 1
        assert mock_service.acquire_sync_lock.call_count == 2
        assert mock_service.release_sync_lock.call_count == 2


@pytest.mark.asyncio
async def test_scheduled_sync_worker_crash_releases_or_fails():
    """If an unexpected exception occurs after lock acquisition, release_sync_lock is invoked with FAILED."""
    tenant_1 = uuid.uuid4()

    mock_service = AsyncMock()
    mock_service.acquire_sync_lock.return_value = "token-crash"
    mock_service.release_sync_lock.return_value = True

    mock_service_cls = MagicMock(return_value=mock_service)
    mock_service_cls.get_active_sync_tenant_ids = AsyncMock(return_value=[tenant_1])

    with patch("app.tasks.celery_tasks._get_async_session") as mock_session_factory, patch(
        "app.tasks.celery_tasks.GoogleIntegrationService",
        new=mock_service_cls,
    ), patch(
        "app.tasks.celery_tasks.PropertyInventorySyncService.sync_inventory",
        side_effect=Exception("Worker process killed / unexpected crash"),
    ):
        mock_session_factory.return_value.__aenter__.return_value = AsyncMock()
        res = await _sync_all_active_google_sheets_inventory_async()

        assert res["total_eligible"] == 1
        assert res["failed"] == 1
        assert res["synced"] == 0
        mock_service.release_sync_lock.assert_called_once()
        assert mock_service.release_sync_lock.call_args.kwargs.get("final_status") == "FAILED"


@pytest.mark.asyncio
async def test_scheduled_sync_status_aggregation_distinguishes_outcomes():
    """Batch correctly differentiates full success, partial success, validation failure, and API failure."""
    t_full = uuid.uuid4()
    t_partial = uuid.uuid4()
    t_val_fail = uuid.uuid4()
    t_infra_fail = uuid.uuid4()

    mock_res_full = MagicMock(status="SUCCESS", created=5, updated=0, staged=0, error_count=0)
    mock_res_partial = MagicMock(status="PARTIAL_SUCCESS", created=3, updated=0, staged=2, error_count=2)
    mock_res_val_fail = MagicMock(status="FAILED", created=0, updated=0, staged=0, error_count=5)

    mock_service = AsyncMock()
    mock_service.acquire_sync_lock.return_value = "token-test"
    mock_service.release_sync_lock.return_value = True

    mock_service_cls = MagicMock(return_value=mock_service)
    mock_service_cls.get_active_sync_tenant_ids = AsyncMock(
        return_value=[t_full, t_partial, t_val_fail, t_infra_fail]
    )

    async def sync_side_effect(self):
        if self.tenant_id == t_full:
            return mock_res_full
        elif self.tenant_id == t_partial:
            return mock_res_partial
        elif self.tenant_id == t_val_fail:
            return mock_res_val_fail
        else:
            raise RuntimeError("Google Sheets API 503 Unavailable")

    with patch("app.tasks.celery_tasks._get_async_session") as mock_session_factory, patch(
        "app.tasks.celery_tasks.GoogleIntegrationService",
        new=mock_service_cls,
    ), patch(
        "app.tasks.celery_tasks.PropertyInventorySyncService.sync_inventory",
        side_effect=sync_side_effect,
        autospec=True,
    ):
        mock_session_factory.return_value.__aenter__.return_value = AsyncMock()
        res = await _sync_all_active_google_sheets_inventory_async()

        assert res["total_eligible"] == 4
        assert res["synced"] == 1
        assert res["synced_full"] == 1
        assert res["synced_partial"] == 1
        assert res["failed_validation"] == 1
        assert res["failed_infrastructure"] == 1
        assert res["failed"] == 2
        assert res["skipped_locked"] == 0


@pytest.mark.asyncio
async def test_property_inventory_sync_lock_lost_halts_processing():
    """If lease renewal fails mid-sync, SyncLockLostError is raised and processing halts immediately."""
    from app.integrations.google.inventory_sync import PropertyInventorySyncService, SyncLockLostError

    tenant_id = uuid.uuid4()
    mock_integration = MagicMock()
    mock_integration.id = uuid.uuid4()
    mock_integration.spreadsheet_id = "sheet-test"
    mock_integration.properties_sheet_name = "Properties"
    mock_integration.column_mappings = {}

    mock_db = AsyncMock()
    mock_db_res = MagicMock()
    mock_db_res.scalar_one_or_none.return_value = mock_integration
    mock_db.execute.return_value = mock_db_res

    mock_google_svc = AsyncMock()
    mock_google_svc.renew_sync_lock.return_value = False  # Lease lost to another worker!

    mock_client = AsyncMock()
    mock_client.get_rows.return_value = [
        ["Title", "Price", "Location", "Property Type"],
        ["Prop 1", "50 Lakh", "Dehradun", "Apartment"],
        ["Prop 2", "60 Lakh", "Mussoorie", "Villa"],
    ]

    with patch("app.integrations.google.inventory_sync.GoogleIntegrationService", return_value=mock_google_svc):
        svc = PropertyInventorySyncService(
            mock_db,
            tenant_id,
            sheets_client=mock_client,
            lock_token="lost-token",
            heartbeat_interval_seconds=0,  # Force heartbeat on first row
        )
        with pytest.raises(SyncLockLostError):
            await svc.sync_inventory()


def test_sync_all_active_google_sheets_inventory_celery_task_wrapper():
    """Celery task entry point executes the async function via asyncio.run."""
    with patch(
        "app.tasks.celery_tasks._sync_all_active_google_sheets_inventory_async",
        new_callable=AsyncMock,
        return_value={"total_eligible": 1, "synced": 1, "skipped_locked": 0, "failed": 0},
    ):
        result = sync_all_active_google_sheets_inventory()
        assert result["synced"] == 1


