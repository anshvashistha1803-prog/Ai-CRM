"""
PostgreSQL integration tests for the Google Sheets Inventory Integration Workflow.
Runs against the dedicated test database (havn_test).

Covers all 10 verification scenarios required for Phase 2, Step 2.1:
1. Valid property row.
2. Missing or invalid price.
3. Invalid property type.
4. Duplicate property ID (in-place update).
5. Duplicate-suspect listing (phone + location match).
6. Missing optional fields.
7. Repeated synchronization idempotency (no duplicate properties or staging rows).
8. Staging review and promotion into canonical property.
9. Multi-tenant isolation (two tenants importing similar/identical data).
10. Failure handling and error capture (Spreadsheet access failure).
"""

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import ConflictError, NotFoundError
from app.integrations.google.inventory_sync import PropertyInventorySyncService
from app.integrations.google.models import InventoryStagingItem, TenantGoogleIntegration
from app.integrations.google.schemas import InventoryStagingPromoteRequest
from app.integrations.google.service import GoogleIntegrationService
from app.integrations.sheets.client import (
    MockGoogleSheetsClient,
    SpreadsheetAccessError,
)
from app.properties.models import Property
from app.tenants.models import Tenant
from tests.conftest import test_engine


def _make_session() -> AsyncSession:
    """Create an isolated AsyncSession bound to test_engine."""
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    return factory()


@pytest.mark.asyncio
class TestInventorySyncIntegrationWorkflow:
    """End-to-end integration tests for inventory sync against PostgreSQL."""

    async def _create_tenant_with_integration(
        self, session: AsyncSession, name: str, spreadsheet_id: str
    ) -> tuple[Tenant, TenantGoogleIntegration]:
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
        await session.flush()

        integration = TenantGoogleIntegration(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            is_active=True,
            spreadsheet_id=spreadsheet_id,
            properties_sheet_name="Inventory",
        )
        session.add(integration)
        await session.commit()
        return tenant, integration

    async def test_01_valid_property_row(self, create_tables):
        """Scenario 1: Valid property row creates canonical Property with all parsed attributes."""
        async with _make_session() as session:
            tenant, integration = await self._create_tenant_with_integration(
                session, "Agency Valid", "sheet-valid-01"
            )

            mock_sheets = MockGoogleSheetsClient()
            mock_sheets.seed_sheet(
                "sheet-valid-01",
                "Inventory",
                [
                    ["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price", "Date", "Google map link", "Remarks"],
                    ["Vikram Malhotra", "+919876543210", "Villa", "4 BHK", "480 sq.yd", "Balbir road dalanwala", "5.95 Cr", "October 11, 2026", "https://maps.google.com/?q=30.3,78.0", "Prime gated estate"],
                ],
            )

            sync_svc = PropertyInventorySyncService(session, tenant.id, sheets_client=mock_sheets)
            res = await sync_svc.sync_inventory()
            await session.commit()

            assert res.status == "SUCCESS"
            assert res.total_rows == 1
            assert res.created == 1
            assert res.error_count == 0

            # Query real database row
            db_res = await session.execute(
                sa.select(Property).where(Property.tenant_id == tenant.id)
            )
            prop = db_res.scalar_one()

            assert prop.property_type == "VILLA"
            assert prop.bhk == 4
            assert prop.area_sqft == 4320  # 480 * 9 sqft
            assert prop.location_text == "Balbir road dalanwala"
            assert prop.price == 59_500_000
            assert prop.external_id.startswith("HAVN-")
            assert prop.owner_name == "Vikram Malhotra"
            assert prop.owner_phone == "+919876543210"
            assert prop.source_data["Size"] == "480 sq.yd"
            assert "Map: https://maps.google.com/?q=30.3,78.0" in prop.description

    async def test_02_missing_or_invalid_price(self, create_tables):
        """Scenario 2: Missing or unparseable price routes row to inventory staging."""
        async with _make_session() as session:
            tenant, integration = await self._create_tenant_with_integration(
                session, "Agency Staging", "sheet-bad-price-02"
            )

            mock_sheets = MockGoogleSheetsClient()
            mock_sheets.seed_sheet(
                "sheet-bad-price-02",
                "Inventory",
                [
                    ["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price"],
                    ["Rohan Mehta", "+919988776655", "Villa", "5 BHK", "600 sq.yd", "Mussoorie Road", "Price on Request"],
                ],
            )

            sync_svc = PropertyInventorySyncService(session, tenant.id, sheets_client=mock_sheets)
            res = await sync_svc.sync_inventory()
            await session.commit()

            assert res.status == "PARTIAL_SUCCESS"
            assert res.total_rows == 1
            assert res.created == 0
            assert res.staged == 1
            assert res.error_count == 1
            assert "Invalid or missing property price" in res.errors[0]["error"]

            # Verify no property created
            props = (await session.execute(sa.select(Property).where(Property.tenant_id == tenant.id))).scalars().all()
            assert len(props) == 0

            # Verify staged item in database
            staged = (await session.execute(
                sa.select(InventoryStagingItem).where(InventoryStagingItem.tenant_id == tenant.id)
            )).scalar_one()
            assert staged.status == "PENDING_REVIEW"
            assert staged.owner_name == "Rohan Mehta"
            assert staged.owner_phone == "+919988776655"
            assert "price" in staged.validation_errors[0].lower()

    async def test_03_invalid_property_type(self, create_tables):
        """Scenario 3: Unrecognized property type routes row to staging."""
        async with _make_session() as session:
            tenant, integration = await self._create_tenant_with_integration(
                session, "Agency Bad Type", "sheet-bad-type-03"
            )

            mock_sheets = MockGoogleSheetsClient()
            mock_sheets.seed_sheet(
                "sheet-bad-type-03",
                "Inventory",
                [
                    ["Owner name", "Owner number", "Property type", "Size", "Area", "Asking price"],
                    ["Rajesh Koothrappali", "+919876500000", "Floating Fortress", "500 sq.yd", "Rajpur Road", "3 Cr"],
                ],
            )

            sync_svc = PropertyInventorySyncService(session, tenant.id, sheets_client=mock_sheets)
            res = await sync_svc.sync_inventory()
            await session.commit()

            assert res.staged == 1
            assert res.created == 0
            assert any("property type" in e["error"].lower() for e in res.errors)

            staged = (await session.execute(
                sa.select(InventoryStagingItem).where(InventoryStagingItem.tenant_id == tenant.id)
            )).scalar_one()
            assert staged.status == "PENDING_REVIEW"
            assert "property type" in staged.validation_errors[0].lower()

    async def test_04_duplicate_property_id_updates_in_place(self, create_tables):
        """Scenario 4: Re-syncing an existing explicit Property ID updates attributes in-place."""
        async with _make_session() as session:
            tenant, integration = await self._create_tenant_with_integration(
                session, "Agency Dup ID", "sheet-dup-id-04"
            )

            mock_sheets = MockGoogleSheetsClient()
            # Initial sync
            mock_sheets.seed_sheet(
                "sheet-dup-id-04",
                "Inventory",
                [
                    ["Property ID", "Property type", "Asking price", "Area", "Title"],
                    ["PROP-XYZ-1", "Apartment", "50 Lakh", "Dalanwala", "Cozy 2BHK Flat"],
                ],
            )

            sync_svc = PropertyInventorySyncService(session, tenant.id, sheets_client=mock_sheets)
            res1 = await sync_svc.sync_inventory()
            await session.commit()
            assert res1.created == 1

            # Update sheet with revised price and title
            mock_sheets.seed_sheet(
                "sheet-dup-id-04",
                "Inventory",
                [
                    ["Property ID", "Property type", "Asking price", "Area", "Title"],
                    ["PROP-XYZ-1", "Apartment", "45 Lakh", "Dalanwala", "Discounted 2BHK Flat"],
                ],
            )

            res2 = await sync_svc.sync_inventory()
            await session.commit()
            assert res2.created == 0
            assert res2.updated == 1

            # Verify in DB: exactly 1 property exists with updated price
            props = (await session.execute(
                sa.select(Property).where(Property.tenant_id == tenant.id)
            )).scalars().all()
            assert len(props) == 1
            assert props[0].external_id == "PROP-XYZ-1"
            assert props[0].price == 4_500_000
            assert props[0].title == "Discounted 2BHK Flat"

    async def test_05_duplicate_suspect_listing(self, create_tables):
        """Scenario 5: Cross-listing with matching phone and location but different specs flags DUPLICATE_SUSPECT."""
        async with _make_session() as session:
            tenant, integration = await self._create_tenant_with_integration(
                session, "Agency Suspect", "sheet-suspect-05"
            )

            # Pre-create an existing property
            existing_prop = Property(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                external_id="HAVN-ORIGINAL-99",
                title="Existing Property",
                property_type="VILLA",
                bhk=4,
                price=50_000_000,
                location_text="Balbir road dalanwala",
                owner_phone="+919876543210",
                owner_name="Vikram Malhotra",
                status="AVAILABLE",
                amenities=[],
                photos=[],
                source_data={},
            )
            session.add(existing_prop)
            await session.commit()

            # Sheet contains row with same owner phone and location, but different size (generates different hash)
            mock_sheets = MockGoogleSheetsClient()
            mock_sheets.seed_sheet(
                "sheet-suspect-05",
                "Inventory",
                [
                    ["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price"],
                    ["Vikram Malhotra", "+919876543210", "Villa", "4 BHK", "700 sq.yd", "Balbir road dalanwala", "6.5 Cr"],
                ],
            )

            sync_svc = PropertyInventorySyncService(session, tenant.id, sheets_client=mock_sheets)
            res = await sync_svc.sync_inventory()
            await session.commit()

            assert res.created == 0
            assert res.updated == 0
            assert res.staged == 1
            assert res.duplicate_suspects == 1

            staged = (await session.execute(
                sa.select(InventoryStagingItem).where(InventoryStagingItem.tenant_id == tenant.id)
            )).scalar_one()
            assert staged.status == "DUPLICATE_SUSPECT"
            assert staged.duplicate_matched_property_id == existing_prop.id

    async def test_06_missing_optional_fields(self, create_tables):
        """Scenario 6: Missing optional fields (BHK, title, description, map link) are handled gracefully."""
        async with _make_session() as session:
            tenant, integration = await self._create_tenant_with_integration(
                session, "Agency Minimal", "sheet-minimal-06"
            )

            mock_sheets = MockGoogleSheetsClient()
            mock_sheets.seed_sheet(
                "sheet-minimal-06",
                "Inventory",
                [
                    ["Property type", "Asking price", "Area"],
                    ["Plot", "40 Lakh", "Kuanwala"],
                ],
            )

            sync_svc = PropertyInventorySyncService(session, tenant.id, sheets_client=mock_sheets)
            res = await sync_svc.sync_inventory()
            await session.commit()

            assert res.created == 1
            prop = (await session.execute(
                sa.select(Property).where(Property.tenant_id == tenant.id)
            )).scalar_one()

            assert prop.property_type == "PLOT"
            assert prop.price == 4_000_000
            assert prop.location_text == "Kuanwala"
            assert prop.bhk is None
            assert prop.area_sqft is None
            assert "PLOT in Kuanwala" in prop.title

    async def test_07_repeated_synchronization_idempotency(self, create_tables):
        """Scenario 7: Syncing multiple times creates 0 duplicate properties and 0 duplicate staging rows."""
        async with _make_session() as session:
            tenant, integration = await self._create_tenant_with_integration(
                session, "Agency Idempotent", "sheet-idempotent-07"
            )

            mock_sheets = MockGoogleSheetsClient()
            sheet_data = [
                ["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price"],
                ["Owner Valid", "+919811111111", "Apartment", "2 BHK", "1200 sqft", "Dalanwala", "60 Lakh"],
                ["Owner Bad", "+919822222222", "Villa", "4 BHK", "300 gaj", "Jakhan", "TBD"],  # Bad price
            ]
            mock_sheets.seed_sheet("sheet-idempotent-07", "Inventory", sheet_data)

            sync_svc = PropertyInventorySyncService(session, tenant.id, sheets_client=mock_sheets)
            # Run 1
            res1 = await sync_svc.sync_inventory()
            await session.commit()
            assert res1.created == 1
            assert res1.staged == 1

            # Run 2 with unchanged sheet
            res2 = await sync_svc.sync_inventory()
            await session.commit()
            assert res2.created == 0
            assert res2.updated == 1
            assert res2.staged == 1

            # Verify total count in DB
            prop_cnt = (await session.execute(
                sa.select(sa.func.count()).select_from(Property).where(Property.tenant_id == tenant.id)
            )).scalar()
            staged_cnt = (await session.execute(
                sa.select(sa.func.count()).select_from(InventoryStagingItem).where(InventoryStagingItem.tenant_id == tenant.id)
            )).scalar()

            assert prop_cnt == 1  # Exactly 1 property, no duplicate
            assert staged_cnt == 1  # Exactly 1 staging row, updated in place

    async def test_08_staging_review_and_promotion(self, create_tables):
        """Scenario 8: Reviewing and promoting a staged item creates a Property and locks item."""
        async with _make_session() as session:
            tenant, integration = await self._create_tenant_with_integration(
                session, "Agency Promote", "sheet-promote-08"
            )

            # Create staged item
            staging_id = uuid.uuid4()
            staged = InventoryStagingItem(
                id=staging_id,
                tenant_id=tenant.id,
                status="PENDING_REVIEW",
                validation_errors=["Missing price"],
                raw_data={"Owner": "Kamal Hassan", "Loc": "Dehradun"},
                parsed_data={"title": "Penthouse in Dehradun", "property_type": "PENTHOUSE", "location_text": "Dehradun", "bhk": 3},
                owner_name="Kamal Hassan",
                owner_phone="+919876543210",
            )
            session.add(staged)
            await session.commit()

            service = GoogleIntegrationService(session, tenant.id)
            promote_req = InventoryStagingPromoteRequest(
                price=25_000_000,
                property_type="PENTHOUSE",
                title="Super Luxury Penthouse",
            )

            promoted_prop = await service.promote_staging_item(staging_id, promote_req)
            await session.commit()

            assert promoted_prop.price == 25_000_000
            assert promoted_prop.title == "Super Luxury Penthouse"

            # Check DB state
            reloaded_staged = (await session.execute(
                sa.select(InventoryStagingItem).where(InventoryStagingItem.id == staging_id)
            )).scalar_one()
            assert reloaded_staged.status == "PROMOTED"
            assert reloaded_staged.promoted_property_id == promoted_prop.id

            # Duplicate promotion attempt is rejected
            with pytest.raises(ConflictError):
                await service.promote_staging_item(staging_id, promote_req)

    async def test_09_multi_tenant_isolation(self, create_tables):
        """Scenario 9: Two tenants importing identical sheet listings remain 100% isolated."""
        async with _make_session() as session:
            tenant_a, int_a = await self._create_tenant_with_integration(
                session, "Agency Alpha", "sheet-shared-alpha"
            )
            tenant_b, int_b = await self._create_tenant_with_integration(
                session, "Agency Beta", "sheet-shared-beta"
            )

            identical_data = [
                ["Owner name", "Owner number", "Property type", "Size", "Area", "Asking price"],
                ["Same Owner", "+919899999999", "Villa", "500 sq.yd", "Dalanwala", "4 Cr"],
            ]

            mock_sheets = MockGoogleSheetsClient()
            mock_sheets.seed_sheet("sheet-shared-alpha", "Inventory", identical_data)
            mock_sheets.seed_sheet("sheet-shared-beta", "Inventory", identical_data)

            # Sync Tenant A
            sync_a = PropertyInventorySyncService(session, tenant_a.id, sheets_client=mock_sheets)
            res_a = await sync_a.sync_inventory()
            await session.commit()

            # Sync Tenant B
            sync_b = PropertyInventorySyncService(session, tenant_b.id, sheets_client=mock_sheets)
            res_b = await sync_b.sync_inventory()
            await session.commit()

            assert res_a.created == 1
            assert res_b.created == 1

            props_a = (await session.execute(sa.select(Property).where(Property.tenant_id == tenant_a.id))).scalars().all()
            props_b = (await session.execute(sa.select(Property).where(Property.tenant_id == tenant_b.id))).scalars().all()

            assert len(props_a) == 1
            assert len(props_b) == 1

            # Separate IDs and separate tenant salt in external_id hash
            assert props_a[0].id != props_b[0].id
            assert props_a[0].external_id != props_b[0].external_id

            # Cross-tenant query by Tenant B for Tenant A's property returns nothing
            isolated_check = (await session.execute(
                sa.select(Property).where(Property.tenant_id == tenant_b.id, Property.id == props_a[0].id)
            )).scalar_one_or_none()
            assert isolated_check is None

    async def test_10_database_and_spreadsheet_failure_handling(self, create_tables):
        """Scenario 10: Spreadsheet access failure records error and marks status FAILED."""
        async with _make_session() as session:
            tenant, integration = await self._create_tenant_with_integration(
                session, "Agency Error", "sheet-error-10"
            )

            mock_sheets = MockGoogleSheetsClient()
            mock_sheets.set_simulated_error(SpreadsheetAccessError("HTTP 403: Forbidden - Sheet sharing disabled"))

            sync_svc = PropertyInventorySyncService(session, tenant.id, sheets_client=mock_sheets)

            with pytest.raises(SpreadsheetAccessError):
                await sync_svc.sync_inventory()
            await session.commit()

            # Integration record must record failure
            reloaded_int = (await session.execute(
                sa.select(TenantGoogleIntegration).where(TenantGoogleIntegration.id == integration.id)
            )).scalar_one()

            assert reloaded_int.last_sync_status == "FAILED"
            assert "Forbidden" in reloaded_int.last_sync_error

    async def test_11_unambiguous_property_edits_all_mutable_fields_in_place(self, create_tables):
        """Scenario 11: Edits to owner name, size, price, location, and owner number update existing property in place when identity is unambiguous."""
        async with _make_session() as session:
            tenant, integration = await self._create_tenant_with_integration(
                session, "Agency Unambiguous", "sheet-unambiguous-11"
            )

            mock_sheets = MockGoogleSheetsClient()
            # Initial import with explicit Property ID
            mock_sheets.seed_sheet(
                "sheet-unambiguous-11",
                "Inventory",
                [
                    ["Property ID", "Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price"],
                    ["PROP-ESTATE-101", "Ramesh Sen", "+919876543210", "Villa", "4 BHK", "500 sq.yd", "Balbir Road Dalanwala", "5 Cr"],
                ],
            )

            sync_svc = PropertyInventorySyncService(session, tenant.id, sheets_client=mock_sheets)
            res1 = await sync_svc.sync_inventory()
            await session.commit()

            assert res1.status == "SUCCESS"
            assert res1.created == 1
            assert res1.updated == 0

            # Verify initial state in DB
            prop1 = (await session.execute(
                sa.select(Property).where(Property.tenant_id == tenant.id, Property.external_id == "PROP-ESTATE-101")
            )).scalar_one()
            assert prop1.owner_name == "Ramesh Sen"
            assert prop1.price == 50_000_000
            assert prop1.location_text == "Balbir Road Dalanwala"
            assert prop1.area_sqft == 4500  # 500 sq.yd * 9 sqft/sq.yd

            # Second import: owner name, number, size, price, and location are ALL modified in source sheet
            mock_sheets.seed_sheet(
                "sheet-unambiguous-11",
                "Inventory",
                [
                    ["Property ID", "Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price"],
                    ["PROP-ESTATE-101", "Anita Sen", "+919876500000", "Villa", "4 BHK", "650 sq.yd", "Rajpur Road Heights", "5.75 Cr"],
                ],
            )

            res2 = await sync_svc.sync_inventory()
            await session.commit()

            assert res2.status == "SUCCESS"
            assert res2.created == 0
            assert res2.updated == 1
            assert res2.staged == 0

            # Verify only 1 property exists and ALL mutable fields were safely updated in place
            props = (await session.execute(
                sa.select(Property).where(Property.tenant_id == tenant.id)
            )).scalars().all()
            assert len(props) == 1
            prop_updated = props[0]
            assert prop_updated.id == prop1.id
            assert prop_updated.external_id == "PROP-ESTATE-101"
            assert prop_updated.owner_name == "Anita Sen"
            assert prop_updated.owner_phone == "+919876500000"
            assert prop_updated.price == 57_500_000
            assert prop_updated.location_text == "Rajpur Road Heights"
            assert prop_updated.area_sqft == 5850  # 650 sq.yd * 9 sqft/sq.yd

            # Verify no staging records were created
            staged_count = (await session.execute(
                sa.select(sa.func.count(InventoryStagingItem.id)).where(InventoryStagingItem.tenant_id == tenant.id)
            )).scalar()
            assert staged_count == 0
