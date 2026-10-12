"""
FastAPI router for Property catalog endpoints (/api/v1/properties).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import TenantContext, UserRole, get_current_user, require_role
from app.properties.schemas import (
    PropertyCreateRequest,
    PropertyListResponse,
    PropertyResponse,
    PropertyUpdateRequest,
)
from app.properties.service import PropertyService

router = APIRouter(prefix="/properties", tags=["Properties"])


@router.post(
    "",
    response_model=PropertyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new property listing",
)
async def create_property(
    payload: PropertyCreateRequest,
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PropertyResponse:
    """Create a new property in the current agency catalog."""
    service = PropertyService(db, context)
    return await service.create_property(payload)


@router.get(
    "",
    response_model=PropertyListResponse,
    summary="List properties with filters and pagination",
)
async def list_properties(
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    property_type: str | None = Query(default=None),
    min_price: int | None = Query(default=None, ge=0),
    max_price: int | None = Query(default=None, ge=0),
    bhk: int | None = Query(default=None, ge=1),
    status: str | None = Query(default=None),
    search: str | None = Query(default=None),
) -> PropertyListResponse:
    """Retrieve paginated properties filtered by type, price, BHK, status, or location."""
    service = PropertyService(db, context)
    return await service.list_properties(
        page=page,
        page_size=page_size,
        property_type=property_type,
        min_price=min_price,
        max_price=max_price,
        bhk=bhk,
        status=status,
        search=search,
    )


@router.get(
    "/{property_id}",
    response_model=PropertyResponse,
    summary="Get single property details",
)
async def get_property(
    property_id: uuid.UUID,
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PropertyResponse:
    """Fetch property details by ID within current tenant scope."""
    service = PropertyService(db, context)
    return await service.get_property(property_id)


@router.patch(
    "/{property_id}",
    response_model=PropertyResponse,
    summary="Update property listing",
)
async def update_property(
    property_id: uuid.UUID,
    payload: PropertyUpdateRequest,
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PropertyResponse:
    """Update property listing attributes."""
    service = PropertyService(db, context)
    return await service.update_property(property_id, payload)


@router.delete(
    "/{property_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Soft delete property listing",
)
async def delete_property(
    property_id: uuid.UUID,
    context: Annotated[TenantContext, Depends(require_role(UserRole.MANAGER))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> None:
    """Soft delete property listing (requires MANAGER role or higher)."""
    service = PropertyService(db, context)
    await service.delete_property(property_id)
