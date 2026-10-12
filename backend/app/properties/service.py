"""
PropertyService — business logic for managing real estate property catalogs.
All queries strictly enforce tenant isolation and soft-delete filtering.
"""

import math
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.properties.models import Property
from app.properties.schemas import (
    PropertyCreateRequest,
    PropertyListResponse,
    PropertyResponse,
    PropertyUpdateRequest,
)


class PropertyService:
    def __init__(self, db: AsyncSession, context: TenantContext) -> None:
        self.db = db
        self.context = context

    async def create_property(self, payload: PropertyCreateRequest) -> PropertyResponse:
        """Create a new property in the agency catalog."""
        now = datetime.now(UTC)
        prop = Property(
            id=uuid.uuid4(),
            tenant_id=self.context.tenant_id,
            title=payload.title,
            property_type=payload.property_type,
            bhk=payload.bhk,
            price=payload.price,
            currency_code=payload.currency_code,
            location_text=payload.location_text,
            latitude=payload.latitude,
            longitude=payload.longitude,
            area_sqft=payload.area_sqft,
            description=payload.description,
            amenities=payload.amenities,
            status=payload.status,
            added_by=self.context.user_id,
            photos=payload.photos,
            created_at=now,
            updated_at=now,
            is_deleted=False,
        )

        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            self.db.add(prop)
            await self.db.flush()

        return self._to_response(prop)

    async def get_property(self, property_id: uuid.UUID) -> PropertyResponse:
        """Fetch property by ID ensuring tenant isolation."""
        prop = await self._find_property(property_id)
        return self._to_response(prop)

    async def list_properties(
        self,
        page: int = 1,
        page_size: int = 20,
        property_type: str | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        bhk: int | None = None,
        status: str | None = None,
        search: str | None = None,
    ) -> PropertyListResponse:
        """Paginated properties query with filters."""
        query = select(Property).where(
            Property.tenant_id == self.context.tenant_id,
            Property.is_deleted.is_(False),
        )

        if property_type:
            query = query.where(Property.property_type == property_type)
        if min_price is not None:
            query = query.where(Property.price >= min_price)
        if max_price is not None:
            query = query.where(Property.price <= max_price)
        if bhk is not None:
            query = query.where(Property.bhk == bhk)
        if status:
            query = query.where(Property.status == status)
        if search:
            query = query.where(
                Property.title.ilike(f"%{search}%") | Property.location_text.ilike(f"%{search}%")
            )

        # Count total
        count_stmt = select(func.count()).select_from(query.subquery())
        total_result = await self.db.execute(count_stmt)
        total_items = total_result.scalar_one()

        # Paginated fetch
        query = query.order_by(Property.created_at.desc()).offset(
            (page - 1) * page_size
        ).limit(page_size)

        result = await self.db.execute(query)
        properties = result.scalars().all()

        items = [self._to_response(p) for p in properties]
        total_pages = math.ceil(total_items / page_size) if total_items > 0 else 0

        return PropertyListResponse(
            data=items,
            total=total_items,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )

    async def update_property(
        self, property_id: uuid.UUID, payload: PropertyUpdateRequest
    ) -> PropertyResponse:
        """Update property attributes within the agency."""
        prop = await self._find_property(property_id)
        now = datetime.now(UTC)

        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            for field in [
                "title",
                "property_type",
                "bhk",
                "price",
                "currency_code",
                "location_text",
                "latitude",
                "longitude",
                "area_sqft",
                "description",
                "amenities",
                "status",
                "photos",
            ]:
                val = getattr(payload, field)
                if val is not None:
                    setattr(prop, field, val)

            prop.updated_at = now
            await self.db.flush()

        return self._to_response(prop)

    async def delete_property(self, property_id: uuid.UUID) -> None:
        """Soft delete a property listing (requires MANAGER or higher role)."""
        if not self.context.has_role(UserRole.MANAGER):
            raise PermissionDeniedError("Deleting properties requires MANAGER role or higher.")

        prop = await self._find_property(property_id)
        now = datetime.now(UTC)

        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            prop.is_deleted = True
            prop.deleted_at = now
            await self.db.flush()

    async def _find_property(self, property_id: uuid.UUID) -> Property:
        stmt = select(Property).where(
            Property.id == property_id,
            Property.tenant_id == self.context.tenant_id,
            Property.is_deleted.is_(False),
        )
        result = await self.db.execute(stmt)
        prop = result.scalars().first()
        if not prop:
            raise NotFoundError(f"Property {property_id} not found.")
        return prop

    def _to_response(self, prop: Property) -> PropertyResponse:
        return PropertyResponse(
            id=prop.id,
            tenant_id=prop.tenant_id,
            title=prop.title,
            property_type=prop.property_type,
            bhk=prop.bhk,
            price=prop.price,
            currency_code=prop.currency_code,
            location_text=prop.location_text,
            latitude=float(prop.latitude) if prop.latitude is not None else None,
            longitude=float(prop.longitude) if prop.longitude is not None else None,
            area_sqft=prop.area_sqft,
            description=prop.description,
            amenities=prop.amenities or [],
            status=prop.status,
            added_by=prop.added_by,
            photos=prop.photos or [],
            created_at=prop.created_at,
            updated_at=prop.updated_at,
        )
