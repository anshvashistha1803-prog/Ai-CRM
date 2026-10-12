"""
Unit tests for PropertyService.
Verifies property catalog CRUD, filtering, role permissions, and tenant isolation.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.properties.models import Property
from app.properties.schemas import PropertyCreateRequest, PropertyUpdateRequest
from app.properties.service import PropertyService


def _create_mock_property(tenant_id: uuid.UUID) -> Property:
    now = datetime.now(UTC)
    return Property(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        title="Luxury 3BHK Apartment in Koramangala",
        property_type="APARTMENT",
        bhk=3,
        price=18500000,
        currency_code="INR",
        location_text="Koramangala 4th Block, Bangalore",
        latitude=12.9352,
        longitude=77.6245,
        area_sqft=1850,
        description="Spacious home with clubhouse amenities",
        amenities=["GYM", "SWIMMING_POOL", "CLUBHOUSE"],
        status="AVAILABLE",
        added_by=uuid.uuid4(),
        photos=["https://cdn.example.com/photo1.jpg"],
        created_at=now,
        updated_at=now,
        is_deleted=False,
    )


class MockTx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            await self.session.rollback()


@pytest.fixture
def mock_db():
    session = AsyncMock()
    session.add = MagicMock()
    session.in_transaction = MagicMock(return_value=True)
    session.begin_nested = MagicMock(side_effect=lambda: MockTx(session))
    session.begin = MagicMock(side_effect=lambda: MockTx(session))
    return session


@pytest.fixture
def tenant_id():
    return uuid.uuid4()


@pytest.fixture
def agent_id():
    return uuid.uuid4()


@pytest.mark.asyncio
class TestPropertyService:
    async def test_create_property(self, mock_db, tenant_id, agent_id):
        context = TenantContext(user_id=agent_id, tenant_id=tenant_id, role=UserRole.AGENT)
        service = PropertyService(mock_db, context)

        payload = PropertyCreateRequest(
            title="Prestige High Fields 3BHK",
            property_type="APARTMENT",
            bhk=3,
            price=15000000,
            location_text="Whitefield, Bangalore",
            area_sqft=1600,
            amenities=["PARKING", "SECURITY"],
        )

        resp = await service.create_property(payload)
        assert resp.title == "Prestige High Fields 3BHK"
        assert resp.tenant_id == tenant_id
        assert resp.added_by == agent_id
        mock_db.add.assert_called_once()
        mock_db.flush.assert_awaited()

    async def test_get_property_found(self, mock_db, tenant_id, agent_id):
        prop = _create_mock_property(tenant_id)
        mock_res = MagicMock()
        mock_res.scalars.return_value.first.return_value = prop
        mock_db.execute.return_value = mock_res

        context = TenantContext(user_id=agent_id, tenant_id=tenant_id, role=UserRole.AGENT)
        service = PropertyService(mock_db, context)

        result = await service.get_property(prop.id)
        assert result.id == prop.id
        assert result.title == prop.title

    async def test_get_property_cross_tenant_raises_not_found(self, mock_db, tenant_id, agent_id):
        mock_res = MagicMock()
        mock_res.scalars.return_value.first.return_value = None
        mock_db.execute.return_value = mock_res

        context = TenantContext(user_id=agent_id, tenant_id=tenant_id, role=UserRole.AGENT)
        service = PropertyService(mock_db, context)

        with pytest.raises(NotFoundError):
            await service.get_property(uuid.uuid4())

    async def test_list_properties(self, mock_db, tenant_id, agent_id):
        prop = _create_mock_property(tenant_id)

        count_res = MagicMock()
        count_res.scalar_one.return_value = 1
        list_res = MagicMock()
        list_res.scalars.return_value.all.return_value = [prop]
        mock_db.execute.side_effect = [count_res, list_res]

        context = TenantContext(user_id=agent_id, tenant_id=tenant_id, role=UserRole.AGENT)
        service = PropertyService(mock_db, context)

        result = await service.list_properties(page=1, page_size=10, bhk=3, min_price=10000000)
        assert result.total == 1
        assert len(result.data) == 1
        assert result.data[0].bhk == 3

    async def test_update_property(self, mock_db, tenant_id, agent_id):
        prop = _create_mock_property(tenant_id)
        mock_res = MagicMock()
        mock_res.scalars.return_value.first.return_value = prop
        mock_db.execute.return_value = mock_res

        context = TenantContext(user_id=agent_id, tenant_id=tenant_id, role=UserRole.AGENT)
        service = PropertyService(mock_db, context)

        update_payload = PropertyUpdateRequest(price=20000000, status="SOLD")
        updated = await service.update_property(prop.id, update_payload)
        assert updated.price == 20000000
        assert updated.status == "SOLD"

    async def test_delete_property_requires_manager(self, mock_db, tenant_id, agent_id):
        context = TenantContext(user_id=agent_id, tenant_id=tenant_id, role=UserRole.AGENT)
        service = PropertyService(mock_db, context)

        with pytest.raises(PermissionDeniedError, match="requires MANAGER role"):
            await service.delete_property(uuid.uuid4())

    async def test_delete_property_by_manager_succeeds(self, mock_db, tenant_id):
        prop = _create_mock_property(tenant_id)
        mock_res = MagicMock()
        mock_res.scalars.return_value.first.return_value = prop
        mock_db.execute.return_value = mock_res

        manager_ctx = TenantContext(user_id=uuid.uuid4(), tenant_id=tenant_id, role=UserRole.MANAGER)
        service = PropertyService(mock_db, manager_ctx)

        await service.delete_property(prop.id)
        assert prop.is_deleted is True
        assert prop.deleted_at is not None
