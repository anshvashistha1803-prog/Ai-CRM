"""
Google Integration Service.
Coordinates Google OAuth 2.0 lifecycle, token encryption at rest,
token refresh, revoked-token status transitions, and integration configuration.
"""

import base64
import contextlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from jose import jwt
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError
from app.core.security import decrypt_field, encrypt_field
from app.integrations.google.models import InventoryStagingItem, TenantGoogleIntegration
from app.integrations.google.oauth import (
    OAuthConfigurationError,
    OAuthRevokedError,
    build_authorization_url,
    exchange_authorization_code,
    generate_oauth_state,
    refresh_google_access_token,
    revoke_google_token,
    validate_oauth_state,
)
from app.integrations.google.schemas import (
    GoogleAuthorizeResponse,
    GoogleConfigUpdateRequest,
    GoogleStatusResponse,
    InventoryStagingItemResponse,
    InventoryStagingListResponse,
    InventoryStagingPromoteRequest,
)
from app.properties.models import Property
from app.properties.schemas import PropertyResponse

logger = structlog.get_logger()
settings = get_settings()


class GoogleIntegrationService:
    """
    Manages tenant-scoped Google OAuth 2.0 lifecycle and Google Sheets integration.
    Guarantees strict tenant isolation: state, tokens, and configs are partitioned by tenant_id.
    """

    def __init__(self, db: AsyncSession, tenant_id: uuid.UUID) -> None:
        self.db = db
        self.tenant_id = tenant_id

    async def get_integration(self) -> TenantGoogleIntegration | None:
        """Fetch the Google integration record for the current tenant."""
        stmt = select(TenantGoogleIntegration).where(
            TenantGoogleIntegration.tenant_id == self.tenant_id,
            TenantGoogleIntegration.is_deleted.is_(False),
        )
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def get_or_create_integration(self) -> TenantGoogleIntegration:
        """Fetch or initialize a Google integration record for the current tenant."""
        integration = await self.get_integration()
        if not integration:
            integration = TenantGoogleIntegration(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                is_active=False,
                properties_sheet_name="Properties",
                incoming_leads_sheet_name="Incoming Leads",
                qualified_leads_sheet_name="Qualified Leads",
                column_mappings={},
                sync_mode="MERGE_VALIDATED",
            )
            self.db.add(integration)
            await self.db.flush()
        return integration

    async def initiate_authorization(self, user_id: uuid.UUID) -> GoogleAuthorizeResponse:
        """
        Generate a secure, signed OAuth state token and construct the Google authorization URL.
        """
        if not settings.google_client_id or not settings.google_client_secret:
            raise OAuthConfigurationError(
                "Google OAuth credentials (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET) are not configured."
            )

        state = generate_oauth_state(self.tenant_id, user_id)
        auth_url = build_authorization_url(state=state)

        logger.info(
            "google_oauth_authorization_initiated",
            tenant_id=str(self.tenant_id),
            user_id=str(user_id),
        )
        return GoogleAuthorizeResponse(authorization_url=auth_url, state=state)

    async def process_oauth_callback(
        self,
        code: str,
        state: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> TenantGoogleIntegration:
        """
        Validate OAuth state, exchange authorization code, securely store encrypted tokens.
        """
        # 1. Cryptographically validate state and enforce tenant isolation
        state_payload = validate_oauth_state(state, expected_tenant_id=self.tenant_id)
        user_id_str = state_payload.get("user_id")
        user_id = uuid.UUID(user_id_str) if user_id_str else None

        # 2. Exchange authorization code with Google token endpoint
        token_data = await exchange_authorization_code(code, client=http_client)

        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in", 3600)
        scopes_raw = token_data.get("scope", "")
        id_token = token_data.get("id_token")

        if not access_token:
            raise OAuthConfigurationError("Google token exchange did not return an access token.")

        google_email = None
        google_sub = None
        if id_token:
            if "." in id_token:
                parts = id_token.split(".")
                if len(parts) >= 2:
                    with contextlib.suppress(Exception):
                        payload_segment = parts[1]
                        padded = payload_segment + "=" * (-len(payload_segment) % 4)
                        claims = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
                        google_email = claims.get("email")
                        google_sub = claims.get("sub")
            if not google_email:
                with contextlib.suppress(Exception):
                    claims = jwt.get_unverified_claims(id_token)
                    google_email = claims.get("email")
                    google_sub = claims.get("sub")

        # 3. Store tokens encrypted with AES-256-GCM
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=expires_in)

        integration = await self.get_or_create_integration()
        integration.access_token_enc = encrypt_field(access_token)
        if refresh_token:
            integration.refresh_token_enc = encrypt_field(refresh_token)
        integration.token_expires_at = expires_at

        scopes_list = scopes_raw.split(" ") if isinstance(scopes_raw, str) else scopes_raw
        integration.scopes = scopes_list
        integration.is_active = True
        integration.connected_at = now
        integration.revoked_at = None
        integration.connected_by = user_id
        if google_email:
            integration.google_email = google_email
        if google_sub:
            integration.google_user_id = google_sub

        await self.db.flush()

        logger.info(
            "google_oauth_callback_completed",
            tenant_id=str(self.tenant_id),
            google_email=google_email,
            scopes_count=len(scopes_list),
        )
        return integration

    async def get_valid_access_token(
        self,
        http_client: httpx.AsyncClient | None = None,
    ) -> str:
        """
        Retrieve a valid access token for the tenant.
        Refreshes automatically if expired using decrypted refresh token.
        Marks integration revoked if Google returns invalid_grant.
        """
        integration = await self.get_integration()
        if not integration or not integration.is_active or not integration.refresh_token_enc:
            raise OAuthConfigurationError(
                "Google integration is not connected or lacks refresh credentials for this tenant."
            )

        now = datetime.now(UTC)
        # If access token is still valid with at least 60 seconds buffer
        if (
            integration.access_token_enc
            and integration.token_expires_at
            and integration.token_expires_at > (now + timedelta(seconds=60))
        ):
            access_token = decrypt_field(integration.access_token_enc)
            if access_token:
                return access_token

        # Refresh needed
        refresh_token = decrypt_field(integration.refresh_token_enc)
        if not refresh_token:
            raise OAuthConfigurationError("Missing Google refresh token.")

        try:
            new_tokens = await refresh_google_access_token(refresh_token, client=http_client)
            new_access_token = new_tokens.get("access_token")
            expires_in = new_tokens.get("expires_in", 3600)
            new_refresh = new_tokens.get("refresh_token")

            if not new_access_token:
                raise OAuthConfigurationError("Token refresh response did not include an access token.")

            integration.access_token_enc = encrypt_field(new_access_token)
            integration.token_expires_at = now + timedelta(seconds=expires_in)
            if new_refresh:
                integration.refresh_token_enc = encrypt_field(new_refresh)

            await self.db.flush()
            return new_access_token

        except OAuthRevokedError as exc:
            # Token was revoked or expired upstream
            integration.is_active = False
            integration.revoked_at = now
            integration.last_sync_status = "REVOKED"
            integration.last_sync_error = "Google OAuth credentials revoked by user or expired."
            await self.db.flush()
            logger.warning(
                "google_integration_marked_revoked",
                tenant_id=str(self.tenant_id),
                reason=str(exc),
            )
            raise

    async def get_status(self) -> GoogleStatusResponse:
        """
        Return the connection status and sheets configuration for the tenant.
        NEVER leaks tokens, credentials, or secrets in response.
        """
        integration = await self.get_integration()
        if not integration or not integration.is_active or not integration.refresh_token_enc:
            return GoogleStatusResponse(
                is_connected=False,
                connected_at=None,
                connected_by=None,
                google_email=None,
                scopes=[],
                spreadsheet_id=integration.spreadsheet_id if integration else None,
                properties_sheet_name=(integration.properties_sheet_name if integration and integration.properties_sheet_name else "Properties"),
                incoming_leads_sheet_name=(integration.incoming_leads_sheet_name if integration and integration.incoming_leads_sheet_name else "Incoming Leads"),
                qualified_leads_sheet_name=(integration.qualified_leads_sheet_name if integration and integration.qualified_leads_sheet_name else "Qualified Leads"),
                column_mappings=(integration.column_mappings if integration and integration.column_mappings else {}),
                sync_mode=(integration.sync_mode if integration and integration.sync_mode else "MERGE_VALIDATED"),
                last_sync_at=integration.last_sync_at if integration else None,
                last_sync_status=integration.last_sync_status if integration else None,
                last_sync_error=integration.last_sync_error if integration else None,
                last_sync_stats=(integration.last_sync_stats if integration and integration.last_sync_stats else {}),
            )

        return GoogleStatusResponse(
            is_connected=True,
            connected_at=integration.connected_at,
            connected_by=integration.connected_by,
            google_email=integration.google_email,
            scopes=integration.scopes or [],
            spreadsheet_id=integration.spreadsheet_id,
            properties_sheet_name=integration.properties_sheet_name or "Properties",
            incoming_leads_sheet_name=integration.incoming_leads_sheet_name or "Incoming Leads",
            qualified_leads_sheet_name=integration.qualified_leads_sheet_name or "Qualified Leads",
            column_mappings=integration.column_mappings or {},
            sync_mode=integration.sync_mode or "MERGE_VALIDATED",
            last_sync_at=integration.last_sync_at,
            last_sync_status=integration.last_sync_status,
            last_sync_error=integration.last_sync_error,
            last_sync_stats=integration.last_sync_stats or {},
        )

    async def update_config(self, payload: GoogleConfigUpdateRequest) -> GoogleStatusResponse:
        """
        Update tenant-scoped Google Sheets configuration.
        """
        integration = await self.get_or_create_integration()

        if payload.spreadsheet_id is not None:
            integration.spreadsheet_id = payload.spreadsheet_id.strip() or None
        if payload.properties_sheet_name is not None:
            integration.properties_sheet_name = payload.properties_sheet_name.strip()
        if payload.incoming_leads_sheet_name is not None:
            integration.incoming_leads_sheet_name = payload.incoming_leads_sheet_name.strip()
        if payload.qualified_leads_sheet_name is not None:
            integration.qualified_leads_sheet_name = payload.qualified_leads_sheet_name.strip()
        if payload.column_mappings is not None:
            integration.column_mappings = payload.column_mappings
        if payload.sync_mode is not None:
            integration.sync_mode = payload.sync_mode

        await self.db.flush()
        return await self.get_status()

    async def disconnect(self, http_client: httpx.AsyncClient | None = None) -> bool:
        """
        Disconnect Google integration, revoke tokens upstream, clear stored credentials.
        """
        integration = await self.get_integration()
        if not integration or not integration.is_active:
            return True

        # Best-effort upstream token revocation
        token_to_revoke = None
        if integration.refresh_token_enc:
            token_to_revoke = decrypt_field(integration.refresh_token_enc)
        elif integration.access_token_enc:
            token_to_revoke = decrypt_field(integration.access_token_enc)

        if token_to_revoke:
            await revoke_google_token(token_to_revoke, client=http_client)

        integration.is_active = False
        integration.refresh_token_enc = None
        integration.access_token_enc = None
        integration.token_expires_at = None
        integration.revoked_at = datetime.now(UTC)
        integration.last_sync_status = "DISCONNECTED"
        await self.db.flush()

        logger.info("google_integration_disconnected", tenant_id=str(self.tenant_id))
        return True

    async def list_staging_items(
        self,
        page: int = 1,
        page_size: int = 50,
        status: str | None = None,
    ) -> InventoryStagingListResponse:
        """Fetch paginated inventory staging records for this tenant."""
        query = select(InventoryStagingItem).where(
            InventoryStagingItem.tenant_id == self.tenant_id,
            InventoryStagingItem.is_deleted.is_(False),
        )
        if status:
            query = query.where(InventoryStagingItem.status == status)

        count_stmt = select(func.count()).select_from(query.subquery())
        total_res = await self.db.execute(count_stmt)
        total = total_res.scalar_one()

        query = query.order_by(InventoryStagingItem.created_at.desc()).offset(
            (page - 1) * page_size
        ).limit(page_size)

        res = await self.db.execute(query)
        items = res.scalars().all()

        return InventoryStagingListResponse(
            data=[InventoryStagingItemResponse.model_validate(item) for item in items],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_staging_item(self, staging_id: uuid.UUID) -> InventoryStagingItemResponse:
        """Fetch single staging item ensuring tenant isolation."""
        stmt = select(InventoryStagingItem).where(
            InventoryStagingItem.id == staging_id,
            InventoryStagingItem.tenant_id == self.tenant_id,
            InventoryStagingItem.is_deleted.is_(False),
        )
        res = await self.db.execute(stmt)
        item = res.scalar_one_or_none()
        if not item:
            raise NotFoundError(f"Staged inventory item {staging_id} not found.")
        return InventoryStagingItemResponse.model_validate(item)

    async def promote_staging_item(
        self,
        staging_id: uuid.UUID,
        payload: InventoryStagingPromoteRequest,
        user_id: uuid.UUID | None = None,
    ) -> PropertyResponse:
        """
        Review and promote a staged inventory row into a canonical Property.
        Enforces transactional integrity, tenant isolation, and prevents duplicate promotion.
        """
        stmt = select(InventoryStagingItem).where(
            InventoryStagingItem.id == staging_id,
            InventoryStagingItem.tenant_id == self.tenant_id,
            InventoryStagingItem.is_deleted.is_(False),
        )
        res = await self.db.execute(stmt)
        item = res.scalar_one_or_none()
        if not item:
            raise NotFoundError(f"Staged inventory item {staging_id} not found.")

        if item.status == "PROMOTED":
            raise ConflictError(
                f"Staging item {staging_id} has already been promoted to property {item.promoted_property_id}."
            )

        now = datetime.now(UTC)
        parsed = item.parsed_data or {}
        raw = item.raw_data or {}

        title = payload.title or parsed.get("title") or "Promoted Property"
        prop_type = payload.property_type or parsed.get("property_type") or "APARTMENT"
        loc = payload.location_text or parsed.get("location_text") or "Dehradun"
        bhk = payload.bhk if payload.bhk is not None else parsed.get("bhk")
        area_sqft = payload.area_sqft if payload.area_sqft is not None else parsed.get("area_sqft")
        desc = payload.description if payload.description is not None else parsed.get("description")
        ext_id = item.external_id or f"HAVN-{uuid.uuid4().hex[:8].upper()}"

        async with self.db.begin_nested() if self.db.in_transaction() else self.db.begin():
            new_prop = Property(
                id=uuid.uuid4(),
                tenant_id=self.tenant_id,
                external_id=ext_id,
                title=title,
                property_type=prop_type,
                bhk=bhk,
                price=payload.price,
                currency_code="INR",
                location_text=loc,
                area_sqft=area_sqft,
                description=desc,
                status=payload.status or "AVAILABLE",
                added_by=user_id,
                owner_name=item.owner_name,
                owner_phone=item.owner_phone,
                source_data=raw,
                amenities=[],
                photos=[],
                created_at=now,
                updated_at=now,
                is_deleted=False,
            )
            self.db.add(new_prop)

            item.status = "PROMOTED"
            item.promoted_property_id = new_prop.id
            item.updated_at = now
            await self.db.flush()

        return PropertyResponse(
            id=new_prop.id,
            tenant_id=new_prop.tenant_id,
            title=new_prop.title,
            property_type=new_prop.property_type,
            bhk=new_prop.bhk,
            price=new_prop.price,
            currency_code=new_prop.currency_code,
            location_text=new_prop.location_text,
            latitude=float(new_prop.latitude) if new_prop.latitude is not None else None,
            longitude=float(new_prop.longitude) if new_prop.longitude is not None else None,
            area_sqft=new_prop.area_sqft,
            description=new_prop.description,
            amenities=new_prop.amenities or [],
            status=new_prop.status,
            added_by=new_prop.added_by,
            photos=new_prop.photos or [],
            created_at=new_prop.created_at,
            updated_at=new_prop.updated_at,
        )

    async def reject_staging_item(
        self,
        staging_id: uuid.UUID,
        reason: str | None = None,
    ) -> InventoryStagingItemResponse:
        """Mark a staged inventory item as REJECTED."""
        stmt = select(InventoryStagingItem).where(
            InventoryStagingItem.id == staging_id,
            InventoryStagingItem.tenant_id == self.tenant_id,
            InventoryStagingItem.is_deleted.is_(False),
        )
        res = await self.db.execute(stmt)
        item = res.scalar_one_or_none()
        if not item:
            raise NotFoundError(f"Staged inventory item {staging_id} not found.")

        if item.status == "PROMOTED":
            raise ConflictError(f"Cannot reject item {staging_id} that is already promoted.")

        item.status = "REJECTED"
        if reason:
            errors = list(item.validation_errors or [])
            errors.append(f"Rejected: {reason}")
            item.validation_errors = errors
        item.updated_at = datetime.now(UTC)
        await self.db.flush()

        return InventoryStagingItemResponse.model_validate(item)

