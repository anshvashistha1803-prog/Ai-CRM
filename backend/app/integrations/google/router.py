"""
FastAPI router for Google OAuth 2.0 and Sheets integration (/api/v1/integrations/google).
Handles authorization-code flow, callback processing, connection status,
and property inventory synchronization.
"""

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import TenantContext, UserRole, get_current_user
from app.integrations.google.inventory_sync import PropertyInventorySyncService
from app.integrations.google.oauth import (
    OAuthConfigurationError,
    OAuthError,
    OAuthStateError,
    validate_oauth_state,
)
from app.integrations.google.schemas import (
    GoogleAuthorizeResponse,
    GoogleCallbackResponse,
    GoogleConfigUpdateRequest,
    GoogleStatusResponse,
    InventoryStagingItemResponse,
    InventoryStagingListResponse,
    InventoryStagingPromoteRequest,
    InventorySyncResponse,
)
from app.integrations.google.service import GoogleIntegrationService
from app.properties.schemas import PropertyResponse

logger = structlog.get_logger()

router = APIRouter(prefix="/integrations/google", tags=["Integrations - Google"])


@router.get(
    "/authorize",
    response_model=GoogleAuthorizeResponse,
    summary="Initiate Google OAuth 2.0 authorization",
)
async def initiate_google_authorization(
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GoogleAuthorizeResponse:
    """
    Generate a signed OAuth state and return the Google authorization URL.
    Restricted to MANAGER or higher roles.
    """
    context.require_role(UserRole.MANAGER)
    service = GoogleIntegrationService(db, context.tenant_id)
    try:
        return await service.initiate_authorization(context.user_id)
    except OAuthConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.get(
    "/callback",
    response_model=GoogleCallbackResponse,
    summary="Google OAuth 2.0 callback endpoint",
)
async def google_oauth_callback(
    db: Annotated[AsyncSession, Depends(get_db)],
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
) -> GoogleCallbackResponse:
    """
    Handle Google OAuth 2.0 authorization redirect.
    Validates state parameter, exchanges authorization code for tokens,
    and stores encrypted credentials in the tenant's integration record.
    """
    if error:
        logger.warning("google_oauth_callback_error_received", error=error, description=error_description)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google authorization denied: {error_description or error}",
        )

    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing required authorization code or state parameter.",
        )

    try:
        # Validate state first to identify tenant
        state_payload = validate_oauth_state(state)
        tenant_id = state_payload["tenant_id"]

        import uuid

        tenant_uuid = uuid.UUID(tenant_id)
        service = GoogleIntegrationService(db, tenant_uuid)
        integration = await service.process_oauth_callback(code=code, state=state)

        return GoogleCallbackResponse(
            status="connected",
            google_email=integration.google_email,
            scopes=integration.scopes or [],
            spreadsheet_id=integration.spreadsheet_id,
        )
    except OAuthStateError as exc:
        logger.warning("google_oauth_callback_invalid_state", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"OAuth state validation failed: {exc}",
        ) from exc
    except OAuthError as exc:
        logger.error("google_oauth_callback_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Google token exchange failed: {exc}",
        ) from exc


@router.get(
    "/status",
    response_model=GoogleStatusResponse,
    summary="Retrieve Google integration and Sheets configuration status",
)
async def get_google_status(
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GoogleStatusResponse:
    """
    Check if Google integration is active and fetch sheet settings.
    Does not expose access tokens or refresh tokens.
    """
    service = GoogleIntegrationService(db, context.tenant_id)
    return await service.get_status()


@router.patch(
    "/config",
    response_model=GoogleStatusResponse,
    summary="Update Google Sheets configuration",
)
async def update_google_config(
    payload: GoogleConfigUpdateRequest,
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GoogleStatusResponse:
    """
    Update spreadsheet ID, worksheet names, column mappings, and sync mode.
    Restricted to MANAGER or higher roles.
    """
    context.require_role(UserRole.MANAGER)
    service = GoogleIntegrationService(db, context.tenant_id)
    return await service.update_config(payload)


@router.post(
    "/disconnect",
    summary="Disconnect Google integration and revoke credentials",
)
async def disconnect_google(
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """
    Disconnect Google integration and revoke access tokens.
    Restricted to AGENCY_ADMIN role.
    """
    context.require_role(UserRole.AGENCY_ADMIN)
    service = GoogleIntegrationService(db, context.tenant_id)
    await service.disconnect()
    return {"status": "disconnected"}


@router.post(
    "/sync/inventory",
    response_model=InventorySyncResponse,
    summary="Trigger property inventory synchronization from Google Sheets",
)
async def sync_inventory_from_sheets(
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InventorySyncResponse:
    """
    Read inventory rows from configured Google Sheet and synchronize into CRM property catalog.
    Restricted to MANAGER or higher roles.
    """
    context.require_role(UserRole.MANAGER)
    service = GoogleIntegrationService(db, context.tenant_id)
    integration = await service.get_integration()
    if not integration or not integration.spreadsheet_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google Spreadsheet ID is not configured for this tenant.",
        )

    token = await service.acquire_sync_lock()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A Google Sheets inventory synchronization is already in progress for this tenant.",
        )

    # Commit lease acquisition immediately so the row-level FOR UPDATE lock is released
    # before external network operations to Google Sheets begin.
    await db.commit()

    sync_svc = PropertyInventorySyncService(db, context.tenant_id, lock_token=token)
    try:
        res = await sync_svc.sync_inventory()
        await service.release_sync_lock(token, final_status=res.status)
        await db.commit()
        return res
    except Exception as exc:
        await db.rollback()
        try:
            await service.release_sync_lock(token, final_status="FAILED", error_message=str(exc))
            await db.commit()
        except Exception as release_exc:
            logger.error("inventory_sync_release_lock_failed", error=str(release_exc))
        logger.error("inventory_sync_endpoint_error", tenant_id=str(context.tenant_id), error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Google Sheets inventory synchronization failed: {exc}",
        ) from exc


@router.get(
    "/staging",
    response_model=InventoryStagingListResponse,
    summary="List staged inventory rows for review",
)
async def list_staging_inventory(
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    status: Annotated[str | None, Query()] = None,
) -> InventoryStagingListResponse:
    """
    Retrieve paginated inventory staging rows (e.g. pending review or duplicate suspects).
    Restricted to MANAGER or higher roles.
    """
    context.require_role(UserRole.MANAGER)
    service = GoogleIntegrationService(db, context.tenant_id)
    return await service.list_staging_items(page=page, page_size=page_size, status=status)


@router.get(
    "/staging/{staging_id}",
    response_model=InventoryStagingItemResponse,
    summary="Get details of a staged inventory row",
)
async def get_staging_inventory_item(
    staging_id: uuid.UUID,
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InventoryStagingItemResponse:
    """
    Fetch a single staged inventory row.
    Restricted to MANAGER or higher roles.
    """
    context.require_role(UserRole.MANAGER)
    service = GoogleIntegrationService(db, context.tenant_id)
    return await service.get_staging_item(staging_id)


@router.post(
    "/staging/{staging_id}/promote",
    response_model=PropertyResponse,
    summary="Promote a staged inventory row into a canonical Property",
)
async def promote_staging_inventory_item(
    staging_id: uuid.UUID,
    payload: InventoryStagingPromoteRequest,
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PropertyResponse:
    """
    Review and promote a staged inventory row to create an active CRM Property listing.
    Ensures transactional safety and prevents duplicate promotion.
    Restricted to MANAGER or higher roles.
    """
    context.require_role(UserRole.MANAGER)
    service = GoogleIntegrationService(db, context.tenant_id)
    return await service.promote_staging_item(
        staging_id=staging_id,
        payload=payload,
        user_id=context.user_id,
    )


@router.post(
    "/staging/{staging_id}/reject",
    response_model=InventoryStagingItemResponse,
    summary="Reject a staged inventory row",
)
async def reject_staging_inventory_item(
    staging_id: uuid.UUID,
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    reason: Annotated[str | None, Query()] = None,
) -> InventoryStagingItemResponse:
    """
    Mark a staged inventory row as rejected.
    Restricted to MANAGER or higher roles.
    """
    context.require_role(UserRole.MANAGER)
    service = GoogleIntegrationService(db, context.tenant_id)
    return await service.reject_staging_item(staging_id=staging_id, reason=reason)

