"""
Comprehensive unit test suite for Google OAuth 2.0 and Sheets integration.
Tests cover:
- OAuth state generation, signature validation, expiration, and tenant isolation
- Authorization URL construction and missing configuration handling
- Token exchange and secure AES-256-GCM encryption of refresh/access tokens
- Token refresh lifecycle and revoked-token (invalid_grant) transition
- Health/Status endpoint verification ensuring zero credential leakage
- Disconnect and token revocation
- Idempotent property inventory synchronization from Google Sheets
- Column mapping, field validation, and error resilience
- Spreadsheet access failure error handling without credential leakage
"""

import time
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.core.config import get_settings
from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.core.security import decrypt_field, encrypt_field
from app.integrations.google.inventory_sync import PropertyInventorySyncService, _mask_phone
from app.integrations.google.models import InventoryStagingItem, TenantGoogleIntegration
from app.integrations.google.oauth import (
    OAuthConfigurationError,
    OAuthRevokedError,
    OAuthStateError,
    OAuthTokenExchangeError,
    build_authorization_url,
    exchange_authorization_code,
    generate_oauth_state,
    validate_oauth_state,
)
from app.integrations.google.router import (
    disconnect_google,
    get_google_status,
    google_oauth_callback,
    initiate_google_authorization,
    sync_inventory_from_sheets,
    update_google_config,
)
from app.integrations.google.schemas import (
    GoogleConfigUpdateRequest,
    InventoryStagingPromoteRequest,
)
from app.integrations.google.service import GoogleIntegrationService
from app.integrations.sheets.client import (
    MockGoogleSheetsClient,
    SpreadsheetAccessError,
)
from app.properties.models import Property

settings = get_settings()


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
    session.begin = MagicMock(side_effect=lambda: MockTx(session))
    session.begin_nested = MagicMock(side_effect=lambda: MockTx(session))
    session.commit = AsyncMock()
    session.flush = AsyncMock()
    session.rollback = AsyncMock()
    return session


# ── 1. OAuth State Generation and Validation ─────────────────────────────────

def test_oauth_state_valid():
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    state = generate_oauth_state(tenant_id, user_id, ttl_seconds=600)

    payload = validate_oauth_state(state, expected_tenant_id=tenant_id)
    assert payload["tenant_id"] == str(tenant_id)
    assert payload["user_id"] == str(user_id)
    assert payload["exp"] > time.time()


def test_oauth_state_expired():
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    # Expired 10 seconds ago
    state = generate_oauth_state(tenant_id, user_id, ttl_seconds=-10)

    with pytest.raises(OAuthStateError, match="expired"):
        validate_oauth_state(state)


def test_oauth_state_tampered_signature():
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    state = generate_oauth_state(tenant_id, user_id)

    # Tamper signature
    parts = state.split(".")
    tampered = f"{parts[0]}.badsignature123456"

    with pytest.raises(OAuthStateError, match="signature validation failed"):
        validate_oauth_state(tampered)


def test_oauth_state_tenant_isolation_mismatch():
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    user_id = uuid.uuid4()
    state = generate_oauth_state(tenant_a, user_id)

    # Attempting to validate state expecting Tenant B must fail
    with pytest.raises(OAuthStateError, match="tenant mismatch"):
        validate_oauth_state(state, expected_tenant_id=tenant_b)


# ── 2. Authorization URL & Missing Configuration ──────────────────────────────

def test_build_authorization_url_success():
    state = "test-state-token"
    with patch.object(settings, "google_client_id", "test-client-id.apps.googleusercontent.com"):
        url = build_authorization_url(state=state)
        assert "accounts.google.com/o/oauth2/v2/auth" in url
        assert "client_id=test-client-id" in url
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert f"state={state}" in url


def test_build_authorization_url_missing_client_id():
    with (
        patch.object(settings, "google_client_id", ""),
        pytest.raises(OAuthConfigurationError, match="GOOGLE_CLIENT_ID"),
    ):
        build_authorization_url(state="some-state")


# ── 3. Token Exchange and Encrypted Credential Storage ────────────────────────

@pytest.mark.asyncio
async def test_process_oauth_callback_success(mock_db):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    state = generate_oauth_state(tenant_id, user_id)

    # Mock DB query returning no prior integration
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_res

    fake_token_response = {
        "access_token": "ya29.test-access-token",
        "refresh_token": "1//test-refresh-token",
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/spreadsheets openid email",
        "id_token": "header.eyJlbWFpbCI6ICJhZ2VudEBoYXZuZXN0YXRlcy5jb20iLCAic3ViIjogIjEyMzQ1NiJ9.sig",
    }

    service = GoogleIntegrationService(mock_db, tenant_id)

    with patch(
        "app.integrations.google.service.exchange_authorization_code",
        new_callable=AsyncMock,
        return_value=fake_token_response,
    ):
        integration = await service.process_oauth_callback(code="test-auth-code", state=state)

        assert integration.is_active is True
        assert integration.google_email == "agent@havnestates.com"
        assert integration.connected_by == user_id
        # Verify tokens are stored encrypted and can be decrypted
        assert integration.refresh_token_enc != "1//test-refresh-token"
        assert decrypt_field(integration.refresh_token_enc) == "1//test-refresh-token"
        assert decrypt_field(integration.access_token_enc) == "ya29.test-access-token"
        assert mock_db.flush.called


@pytest.mark.asyncio
async def test_token_exchange_upstream_failure():
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 400
    mock_response.json.return_value = {"error": "invalid_grant", "error_description": "Code expired"}
    mock_client.post.return_value = mock_response

    with (
        patch.object(settings, "google_client_id", "client-id"),
        patch.object(settings, "google_client_secret", "secret"),
        pytest.raises(OAuthTokenExchangeError, match="Code expired"),
    ):
        await exchange_authorization_code("bad-code", client=mock_client)


# ── 4. Token Refresh Lifecycle & Revoked Handling ─────────────────────────────

@pytest.mark.asyncio
async def test_get_valid_access_token_unexpired(mock_db):
    tenant_id = uuid.uuid4()
    now = datetime.now(UTC)

    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        access_token_enc=encrypt_field("valid-cached-token"),
        refresh_token_enc=encrypt_field("refresh-token"),
        token_expires_at=now + timedelta(minutes=30),  # Valid for 30 more minutes
    )

    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = integration
    mock_db.execute.return_value = mock_res

    service = GoogleIntegrationService(mock_db, tenant_id)
    token = await service.get_valid_access_token()
    assert token == "valid-cached-token"


@pytest.mark.asyncio
async def test_get_valid_access_token_refreshes_expired(mock_db):
    tenant_id = uuid.uuid4()
    now = datetime.now(UTC)

    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        access_token_enc=encrypt_field("old-expired-token"),
        refresh_token_enc=encrypt_field("my-refresh-token"),
        token_expires_at=now - timedelta(minutes=5),  # Expired
    )

    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = integration
    mock_db.execute.return_value = mock_res

    service = GoogleIntegrationService(mock_db, tenant_id)

    with patch(
        "app.integrations.google.service.refresh_google_access_token",
        new_callable=AsyncMock,
        return_value={"access_token": "brand-new-token", "expires_in": 3600},
    ):
        token = await service.get_valid_access_token()
        assert token == "brand-new-token"
        assert decrypt_field(integration.access_token_enc) == "brand-new-token"
        assert mock_db.flush.called


@pytest.mark.asyncio
async def test_get_valid_access_token_revoked_upstream(mock_db):
    tenant_id = uuid.uuid4()
    now = datetime.now(UTC)

    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        access_token_enc=encrypt_field("old-token"),
        refresh_token_enc=encrypt_field("revoked-refresh-token"),
        token_expires_at=now - timedelta(minutes=5),
    )

    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = integration
    mock_db.execute.return_value = mock_res

    service = GoogleIntegrationService(mock_db, tenant_id)

    with patch(
        "app.integrations.google.service.refresh_google_access_token",
        side_effect=OAuthRevokedError("Token revoked by user"),
    ):
        with pytest.raises(OAuthRevokedError):
            await service.get_valid_access_token()

        assert integration.is_active is False
        assert integration.last_sync_status == "REVOKED"
        assert integration.revoked_at is not None


# ── 5. Status Endpoint Zero Credential Leakage ───────────────────────────────

@pytest.mark.asyncio
async def test_get_status_zero_credential_leakage(mock_db):
    tenant_id = uuid.uuid4()
    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        google_email="director@havnestates.com",
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
        spreadsheet_id="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms",
        refresh_token_enc=encrypt_field("secret-refresh-token"),
        access_token_enc=encrypt_field("secret-access-token"),
    )

    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = integration
    mock_db.execute.return_value = mock_res

    service = GoogleIntegrationService(mock_db, tenant_id)
    status = await service.get_status()

    assert status.is_connected is True
    assert status.google_email == "director@havnestates.com"
    assert status.spreadsheet_id == "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"

    # Verify serialized dictionary does NOT contain any secret tokens
    data = status.model_dump()
    assert "access_token" not in data
    assert "refresh_token" not in data
    assert "access_token_enc" not in data
    assert "refresh_token_enc" not in data


# ── 6. Disconnect and Revocation ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_disconnect_integration(mock_db):
    tenant_id = uuid.uuid4()
    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        refresh_token_enc=encrypt_field("refresh-token-to-revoke"),
        access_token_enc=encrypt_field("access-token"),
    )

    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = integration
    mock_db.execute.return_value = mock_res

    service = GoogleIntegrationService(mock_db, tenant_id)
    with patch("app.integrations.google.service.revoke_google_token", new_callable=AsyncMock, return_value=True) as mock_revoke:
        res = await service.disconnect()
        assert res is True
        mock_revoke.assert_called_once()
        assert integration.is_active is False
        assert integration.refresh_token_enc is None
        assert integration.access_token_enc is None
        assert integration.revoked_at is not None


# ── 7. Config Update ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_config(mock_db):
    tenant_id = uuid.uuid4()
    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        spreadsheet_id=None,
    )

    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = integration
    mock_db.execute.return_value = mock_res

    service = GoogleIntegrationService(mock_db, tenant_id)
    update_req = GoogleConfigUpdateRequest(
        spreadsheet_id="new-sheet-12345",
        properties_sheet_name="Inventory",
        sync_mode="MERGE_VALIDATED",
    )

    status = await service.update_config(update_req)
    assert status.spreadsheet_id == "new-sheet-12345"
    assert status.properties_sheet_name == "Inventory"
    assert status.sync_mode == "MERGE_VALIDATED"


# ── 8. Idempotent Inventory Synchronization ───────────────────────────────────

@pytest.mark.asyncio
async def test_inventory_sync_idempotency(mock_db):
    tenant_id = uuid.uuid4()
    spreadsheet_id = "test-inv-sheet-id"

    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        spreadsheet_id=spreadsheet_id,
        properties_sheet_name="Properties",
    )

    mock_res_integration = MagicMock()
    mock_res_integration.scalar_one_or_none.return_value = integration

    mock_sheets = MockGoogleSheetsClient()
    mock_sheets.seed_sheet(
        spreadsheet_id,
        "Properties",
        [
            ["Property ID", "Title", "Type", "BHK", "Price", "Location", "Size", "Remarks"],
            ["PROP-001", "Luxury Villa Jakhan", "Villa", "4", "2.5 Cr", "Jakhan Dehradun", "350 guz", "Gated society"],
            ["PROP-002", "Skyline Penthouse", "Penthouse", "3", "1.75 Cr", "Dalanwala", "2800 sq ft", "Private terrace"],
            ["PROP-003", "Commercial Plot", "Plot", "", "95 Lakh", "Rajpur Road", "200 gaj", "Main road frontage"],
        ],
    )

    # 1. First sync run — no existing properties in DB
    mock_db_res_empty = MagicMock()
    mock_db_res_empty.scalar_one_or_none.return_value = None

    # First call returns integration, subsequent return None (not found)
    mock_db.execute.side_effect = [
        mock_res_integration,
        mock_db_res_empty,  # PROP-001 check
        mock_db_res_empty,  # PROP-002 check
        mock_db_res_empty,  # PROP-003 check
    ]

    service = PropertyInventorySyncService(mock_db, tenant_id, sheets_client=mock_sheets)
    result = await service.sync_inventory()

    assert result.status == "SUCCESS"
    assert result.total_rows == 3
    assert result.created == 3
    assert result.updated == 0
    assert result.error_count == 0
    assert mock_db.add.call_count == 3

    # Verify created properties
    created_props = [call.args[0] for call in mock_db.add.call_args_list]
    assert created_props[0].external_id == "PROP-001"
    assert created_props[0].price == 25_000_000  # 2.5 Cr
    assert created_props[0].property_type == "VILLA"
    assert created_props[0].bhk == 4

    assert created_props[1].external_id == "PROP-002"
    assert created_props[1].price == 17_500_000  # 1.75 Cr
    assert created_props[1].property_type == "PENTHOUSE"

    assert created_props[2].external_id == "PROP-003"
    assert created_props[2].price == 9_500_000   # 95 Lakh
    assert created_props[2].property_type == "PLOT"

    # 2. Second sync run — existing properties found in DB (Idempotent update)
    mock_db.add.reset_mock()
    existing_prop1 = created_props[0]
    existing_prop2 = created_props[1]
    existing_prop3 = created_props[2]

    mock_db_res1 = MagicMock()
    mock_db_res1.scalar_one_or_none.return_value = existing_prop1
    mock_db_res2 = MagicMock()
    mock_db_res2.scalar_one_or_none.return_value = existing_prop2
    mock_db_res3 = MagicMock()
    mock_db_res3.scalar_one_or_none.return_value = existing_prop3

    mock_db.execute.side_effect = [
        mock_res_integration,
        mock_db_res1,
        mock_db_res2,
        mock_db_res3,
    ]

    result2 = await service.sync_inventory()
    assert result2.status == "SUCCESS"
    assert result2.created == 0
    assert result2.updated == 3
    assert result2.error_count == 0
    # No new properties added to session
    assert mock_db.add.call_count == 0


# ── 9. Error Handling & Validation Resilience ────────────────────────────────

@pytest.mark.asyncio
async def test_inventory_sync_validation_and_failures(mock_db):
    tenant_id = uuid.uuid4()
    spreadsheet_id = "test-sheet-with-errors"

    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        spreadsheet_id=spreadsheet_id,
        properties_sheet_name="Properties",
    )

    mock_res_integration = MagicMock()
    mock_res_integration.scalar_one_or_none.return_value = integration

    mock_sheets = MockGoogleSheetsClient()
    mock_sheets.seed_sheet(
        spreadsheet_id,
        "Properties",
        [
            ["Property ID", "Title", "Type", "Price", "Location"],
            ["PROP-VALID", "Valid 2BHK Flat", "Apartment", "65 Lakh", "Dalanwala"],
            ["PROP-BAD-PRICE", "Invalid Property", "Flat", "Price on Request", "Jakhan"],  # Unparseable price
        ],
    )

    mock_db_res_empty = MagicMock()
    mock_db_res_empty.scalar_one_or_none.return_value = None

    mock_db.execute.side_effect = [
        mock_res_integration,
        mock_db_res_empty,
    ]

    service = PropertyInventorySyncService(mock_db, tenant_id, sheets_client=mock_sheets)
    result = await service.sync_inventory()

    assert result.status == "PARTIAL_SUCCESS"
    assert result.total_rows == 2
    assert result.created == 1
    assert result.error_count == 1
    assert result.errors[0]["row_number"] == 3
    assert "Invalid or missing property price" in result.errors[0]["error"]


@pytest.mark.asyncio
async def test_inventory_sync_spreadsheet_access_failure(mock_db):
    tenant_id = uuid.uuid4()
    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        spreadsheet_id="forbidden-sheet-id",
    )

    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = integration
    mock_db.execute.return_value = mock_res

    mock_sheets = MockGoogleSheetsClient()
    mock_sheets.set_simulated_error(SpreadsheetAccessError("403 Forbidden: Permission Denied"))

    service = PropertyInventorySyncService(mock_db, tenant_id, sheets_client=mock_sheets)

    with pytest.raises(SpreadsheetAccessError):
        await service.sync_inventory()

    assert integration.last_sync_status == "FAILED"
    assert "Permission Denied" in integration.last_sync_error


# ── 10. Router RBAC & Endpoint Tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_router_authorize_rbac(mock_db):
    tenant_id = uuid.uuid4()
    agent_ctx = TenantContext(user_id=uuid.uuid4(), tenant_id=tenant_id, role=UserRole.AGENT)
    mgr_ctx = TenantContext(user_id=uuid.uuid4(), tenant_id=tenant_id, role=UserRole.MANAGER)

    # AGENT is forbidden (needs MANAGER or higher)
    with pytest.raises(PermissionDeniedError):
        await initiate_google_authorization(context=agent_ctx, db=mock_db)

    # MANAGER is allowed
    with patch.object(settings, "google_client_id", "client-id"), patch.object(settings, "google_client_secret", "secret"):
        resp = await initiate_google_authorization(context=mgr_ctx, db=mock_db)
        assert resp.authorization_url.startswith("https://accounts.google.com")
        assert resp.state is not None


@pytest.mark.asyncio
async def test_router_callback_missing_params(mock_db):
    with pytest.raises(HTTPException) as exc_info:
        await google_oauth_callback(db=mock_db, code=None, state=None)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_router_callback_error_param(mock_db):
    with pytest.raises(HTTPException) as exc_info:
        await google_oauth_callback(
            db=mock_db,
            code=None,
            state="some-state",
            error="access_denied",
            error_description="User denied access",
        )
    assert exc_info.value.status_code == 400
    assert "access_denied" in exc_info.value.detail or "User denied access" in exc_info.value.detail


@pytest.mark.asyncio
async def test_router_disconnect_rbac(mock_db):
    tenant_id = uuid.uuid4()
    mgr_ctx = TenantContext(user_id=uuid.uuid4(), tenant_id=tenant_id, role=UserRole.MANAGER)
    admin_ctx = TenantContext(user_id=uuid.uuid4(), tenant_id=tenant_id, role=UserRole.AGENCY_ADMIN)

    # MANAGER is forbidden to disconnect (requires AGENCY_ADMIN)
    with pytest.raises(PermissionDeniedError):
        await disconnect_google(context=mgr_ctx, db=mock_db)

    # AGENCY_ADMIN is allowed
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_res

    res = await disconnect_google(context=admin_ctx, db=mock_db)
    assert res == {"status": "disconnected"}


@pytest.mark.asyncio
async def test_router_get_status_endpoint(mock_db):
    tenant_id = uuid.uuid4()
    agent_ctx = TenantContext(user_id=uuid.uuid4(), tenant_id=tenant_id, role=UserRole.AGENT)

    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_res

    status_resp = await get_google_status(context=agent_ctx, db=mock_db)
    assert status_resp.is_connected is False
    assert status_resp.google_email is None


@pytest.mark.asyncio
async def test_router_update_config_rbac(mock_db):
    tenant_id = uuid.uuid4()
    agent_ctx = TenantContext(user_id=uuid.uuid4(), tenant_id=tenant_id, role=UserRole.AGENT)
    mgr_ctx = TenantContext(user_id=uuid.uuid4(), tenant_id=tenant_id, role=UserRole.MANAGER)
    req = GoogleConfigUpdateRequest(spreadsheet_id="test-sheet-id")

    # AGENT is forbidden
    with pytest.raises(PermissionDeniedError):
        await update_google_config(payload=req, context=agent_ctx, db=mock_db)

    # MANAGER is allowed
    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        spreadsheet_id=None,
    )
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = integration
    mock_db.execute.return_value = mock_res

    status_resp = await update_google_config(payload=req, context=mgr_ctx, db=mock_db)
    assert status_resp.spreadsheet_id == "test-sheet-id"


@pytest.mark.asyncio
async def test_router_sync_inventory_rbac_and_execution(mock_db):
    tenant_id = uuid.uuid4()
    agent_ctx = TenantContext(user_id=uuid.uuid4(), tenant_id=tenant_id, role=UserRole.AGENT)
    mgr_ctx = TenantContext(user_id=uuid.uuid4(), tenant_id=tenant_id, role=UserRole.MANAGER)

    # AGENT is forbidden
    with pytest.raises(PermissionDeniedError):
        await sync_inventory_from_sheets(context=agent_ctx, db=mock_db)

    # MANAGER is allowed
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_res

    with pytest.raises(HTTPException) as exc_info:
        await sync_inventory_from_sheets(context=mgr_ctx, db=mock_db)
    assert exc_info.value.status_code == 502
    assert "Google Spreadsheet ID is not configured" in exc_info.value.detail


@pytest.mark.asyncio
async def test_router_callback_full_success(mock_db):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    state = generate_oauth_state(tenant_id, user_id)

    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_res

    fake_token_response = {
        "access_token": "ya29.router-token",
        "refresh_token": "1//router-refresh-token",
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/spreadsheets openid email",
        "id_token": "header.eyJlbWFpbCI6ICJyb3V0ZXJAY29tcGFueS5jb20iLCAic3ViIjogIjEyMyJ9.sig",
    }

    with patch(
        "app.integrations.google.service.exchange_authorization_code",
        new_callable=AsyncMock,
        return_value=fake_token_response,
    ):
        resp = await google_oauth_callback(db=mock_db, code="test-code-123", state=state)
        assert resp.status == "connected"
        assert resp.google_email == "router@company.com"


@pytest.mark.asyncio
async def test_inventory_sync_havn_realty_columns_and_size_area_distinction(mock_db):
    """
    Validates sync against exact HAVN Realty inventory sheet format:
    Owner name, Owner number, Property type, Configuration, Size, Area, Asking price, Date, Google map link, Remarks.
    Verifies that Size and Area maintain their distinct meanings (physical size vs locality).
    """
    tenant_id = uuid.uuid4()
    spreadsheet_id = "havn-operational-inventory-sheet"

    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        spreadsheet_id=spreadsheet_id,
        properties_sheet_name="Inventory",
    )

    mock_res_integration = MagicMock()
    mock_res_integration.scalar_one_or_none.return_value = integration

    mock_sheets = MockGoogleSheetsClient()
    mock_sheets.seed_sheet(
        spreadsheet_id,
        "Inventory",
        [
            ["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price", "Date", "Google map link", "Remarks"],
            ["Vikram Malhotra", "+919876543210", "Villa", "4 BHK", "480 sq.yd", "Balbir road dalanwala", "5.95 Cr", "October 11, 2026", "https://maps.google.com/?q=30.3,78.0", "Prime gated estate with lawn"],
            ["Ananya Sharma", "+919811122233", "Penthouse", "3 BHK", "3200 sq ft", "Jakhan Dehradun", "2.25 Cr", "October 11, 2026", "https://maps.google.com/?q=30.4,78.1", "Panoramic Himalayan view"],
            ["Rajesh Gupta", "+919844455566", "Plot", "", "250 gaj", "Kuanwala", "75 Lakh", "October 11, 2026", "https://maps.google.com/?q=30.2,78.0", "Corner plot with wide road frontage"],
        ],
    )

    mock_db_res_empty = MagicMock()
    mock_db_res_empty.scalar_one_or_none.return_value = None

    mock_db.execute.side_effect = [
        mock_res_integration,
        mock_db_res_empty,  # Row 1
        mock_db_res_empty,  # Row 2
        mock_db_res_empty,  # Row 3
    ]

    service = PropertyInventorySyncService(mock_db, tenant_id, sheets_client=mock_sheets)
    result = await service.sync_inventory()

    assert result.status == "SUCCESS"
    assert result.total_rows == 3
    assert result.created == 3
    assert result.error_count == 0

    created_props = [call.args[0] for call in mock_db.add.call_args_list]

    # Row 1: Villa
    villa = created_props[0]
    assert villa.property_type == "VILLA"
    assert villa.bhk == 4
    # Size vs Area distinctness verification
    assert villa.area_sqft == 4320  # 480 sq.yd * 9 sqft/sq.yd = 4320 sqft (Physical Size)
    assert villa.location_text == "Balbir road dalanwala"  # Neighborhood/Submarket (Area)
    assert villa.price == 59_500_000  # 5.95 Cr
    assert villa.external_id.startswith("HAVN-")
    assert "Prime gated estate with lawn" in villa.description
    assert "Owner:" not in (villa.description or "")
    assert villa.owner_name == "Vikram Malhotra"
    assert villa.owner_phone == "+919876543210"
    assert villa.source_data["Size"] == "480 sq.yd"
    assert villa.source_data["Area"] == "Balbir road dalanwala"
    assert "Map: https://maps.google.com/?q=30.3,78.0" in villa.description

    # Row 2: Penthouse
    penthouse = created_props[1]
    assert penthouse.property_type == "PENTHOUSE"
    assert penthouse.bhk == 3
    assert penthouse.area_sqft == 3200  # Size
    assert penthouse.location_text == "Jakhan Dehradun"  # Area
    assert penthouse.price == 22_500_000  # 2.25 Cr

    # Row 3: Plot
    plot = created_props[2]
    assert plot.property_type == "PLOT"
    assert plot.area_sqft == 2250  # 250 gaj * 9 = 2250 sqft
    assert plot.location_text == "Kuanwala"
    assert plot.price == 7_500_000  # 75 Lakh


# ── 12. Parser Unit Tests (Prices, Units, Dimensions, PII Masking) ─────────────

def test_parse_price_indian_denominations():
    service = PropertyInventorySyncService(MagicMock(), uuid.uuid4())

    assert service._parse_price("5.95 Cr") == 59_500_000
    assert service._parse_price("5.95 crore") == 59_500_000
    assert service._parse_price("5.95") == 59_500_000  # float < 50 denotes Crores
    assert service._parse_price("75 Lakh") == 7_500_000
    assert service._parse_price("75 lac") == 7_500_000
    assert service._parse_price("260000") == 260_000
    assert service._parse_price("₹ 1.25 Cr") == 12_500_000
    assert service._parse_price("Rs. 50,000") == 50_000
    assert service._parse_price("25k") == 25_000

    # Unparseable / missing prices return None
    assert service._parse_price("Price on Request") is None
    assert service._parse_price("Call for Price") is None
    assert service._parse_price("POA") is None
    assert service._parse_price("TBD") is None
    assert service._parse_price("") is None
    assert service._parse_price("0") is None


def test_parse_area_sqft_units_and_dimensions():
    service = PropertyInventorySyncService(MagicMock(), uuid.uuid4())

    # Gaj / Guz / Sq.yd (1 gaj = 9 sqft)
    assert service._parse_area_sqft("480 sq.yd") == 4320
    assert service._parse_area_sqft("250 gaj") == 2250
    assert service._parse_area_sqft("100 guz") == 900

    # Sqft with commas
    assert service._parse_area_sqft("3,200 sq ft") == 3200
    assert service._parse_area_sqft("5,200 sqft") == 5200

    # Dimensions: length * width
    assert service._parse_area_sqft("45*90") == 4050
    assert service._parse_area_sqft("40x80") == 3200
    assert service._parse_area_sqft("30 X 60") == 1800

    # Square meters
    assert service._parse_area_sqft("100 sqm") == 1076

    # Acres
    assert service._parse_area_sqft("1 acre") == 43560

    # Empty
    assert service._parse_area_sqft("") is None


def test_parse_property_type_and_bhk():
    service = PropertyInventorySyncService(MagicMock(), uuid.uuid4())

    assert service._parse_property_type("Villa") == "VILLA"
    assert service._parse_property_type("Penthouse") == "PENTHOUSE"
    assert service._parse_property_type("Flat") == "APARTMENT"
    assert service._parse_property_type("Apartment") == "APARTMENT"
    assert service._parse_property_type("Plot") == "PLOT"
    assert service._parse_property_type("Land") == "PLOT"
    assert service._parse_property_type("Commercial Space") == "COMMERCIAL"
    assert service._parse_property_type("Office Space") == "COMMERCIAL"

    # Ambiguous / unclassified returns None (does not blindly default to APARTMENT)
    assert service._parse_property_type("Unknown Type", config="") is None
    assert service._parse_property_type("", config="") is None

    # BHK extraction does not mistake plot dimensions for bedroom counts
    assert service._parse_bhk("4 BHK", "Villa", "VILLA") == 4
    assert service._parse_bhk("3 BHK", "Penthouse", "PENTHOUSE") == 3
    assert service._parse_bhk("45*90", "Plot", "PLOT") is None
    assert service._parse_bhk("40x80", "Land", "PLOT") is None


def test_mask_phone_privacy():
    assert _mask_phone("+919876543210") == "+91****3210"
    assert _mask_phone("9876543210") == "987****3210"
    assert _mask_phone("123") == "****"
    assert _mask_phone("") == ""
    assert _mask_phone(None) == ""


# ── 13. Staging Workflow & Privacy Tests ──────────────────────────────────────

@pytest.mark.asyncio
async def test_inventory_sync_missing_price_routes_to_staging(mock_db):
    """
    Rows with missing prices ('Price on Request') are routed to staging
    and never violate Property.price non-null constraint.
    """
    tenant_id = uuid.uuid4()
    spreadsheet_id = "test-sheet-staging"

    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        spreadsheet_id=spreadsheet_id,
        properties_sheet_name="Inventory",
    )

    mock_res_integration = MagicMock()
    mock_res_integration.scalar_one_or_none.return_value = integration

    mock_sheets = MockGoogleSheetsClient()
    mock_sheets.seed_sheet(
        spreadsheet_id,
        "Inventory",
        [
            ["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price"],
            ["Rohan Mehta", "+919988776655", "Villa", "5 BHK", "600 sq.yd", "Mussoorie Road", "Price on Request"],
        ],
    )

    mock_db.execute.return_value = mock_res_integration

    service = PropertyInventorySyncService(mock_db, tenant_id, sheets_client=mock_sheets)
    result = await service.sync_inventory()

    assert result.total_rows == 1
    assert result.created == 0
    assert result.staged == 1
    assert result.error_count == 1
    assert "Invalid or missing property price" in result.errors[0]["error"]

    # Verify staging item was added to session
    added_items = [call.args[0] for call in mock_db.add.call_args_list]
    assert len(added_items) == 1
    staged = added_items[0]
    assert isinstance(staged, InventoryStagingItem)
    assert staged.status == "PENDING_REVIEW"
    assert staged.owner_name == "Rohan Mehta"
    assert staged.owner_phone == "+919988776655"
    assert staged.source_size == "600 sq.yd"
    assert staged.source_area == "Mussoorie Road"
    assert any("price" in err.lower() for err in staged.validation_errors)


@pytest.mark.asyncio
async def test_inventory_sync_ambiguous_property_type_routes_to_staging(mock_db):
    """
    Rows with unrecognized property types are staged rather than silently defaulting to APARTMENT.
    """
    tenant_id = uuid.uuid4()
    spreadsheet_id = "test-ambiguous-type"

    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        spreadsheet_id=spreadsheet_id,
        properties_sheet_name="Inventory",
    )

    mock_res_integration = MagicMock()
    mock_res_integration.scalar_one_or_none.return_value = integration

    mock_sheets = MockGoogleSheetsClient()
    mock_sheets.seed_sheet(
        spreadsheet_id,
        "Inventory",
        [
            ["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price"],
            ["Kapil Dev", "+919123456789", "Special Asset X", "", "500 sq.yd", "Rajpur Road", "3.5 Cr"],
        ],
    )

    mock_db.execute.return_value = mock_res_integration

    service = PropertyInventorySyncService(mock_db, tenant_id, sheets_client=mock_sheets)
    result = await service.sync_inventory()

    assert result.staged == 1
    assert result.created == 0
    assert any("property type" in err["error"].lower() for err in result.errors)


@pytest.mark.asyncio
async def test_inventory_sync_repeat_sync_idempotency(mock_db):
    """
    Verifies that running sync repeatedly is idempotent and does not create duplicates.
    """
    tenant_id = uuid.uuid4()
    spreadsheet_id = "test-repeat-sync"

    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        spreadsheet_id=spreadsheet_id,
        properties_sheet_name="Inventory",
    )

    mock_res_integration = MagicMock()
    mock_res_integration.scalar_one_or_none.return_value = integration

    mock_sheets = MockGoogleSheetsClient()
    mock_sheets.seed_sheet(
        spreadsheet_id,
        "Inventory",
        [
            ["Property ID", "Property type", "Asking price", "Area"],
            ["PROP-100", "Apartment", "50 Lakh", "Dalanwala"],
        ],
    )

    # First run: not found in DB -> created
    mock_db_res_empty = MagicMock()
    mock_db_res_empty.scalar_one_or_none.return_value = None

    mock_db.execute.side_effect = [
        mock_res_integration,
        mock_db_res_empty,
    ]

    service = PropertyInventorySyncService(mock_db, tenant_id, sheets_client=mock_sheets)
    result1 = await service.sync_inventory()
    assert result1.created == 1
    assert result1.updated == 0

    # Second run: found existing in DB -> updated
    created_prop = mock_db.add.call_args_list[0].args[0]
    mock_db.add.reset_mock()

    mock_db_res_found = MagicMock()
    mock_db_res_found.scalar_one_or_none.return_value = created_prop

    mock_db.execute.side_effect = [
        mock_res_integration,
        mock_db_res_found,
    ]

    result2 = await service.sync_inventory()
    assert result2.created == 0
    assert result2.updated == 1
    assert mock_db.add.call_count == 0


@pytest.mark.asyncio
async def test_inventory_staging_promote_and_prevent_duplicate_promotion(mock_db):
    """
    Promote a staged item: creates canonical Property and marks item PROMOTED.
    Subsequent promotion attempt is rejected with ConflictError.
    """
    tenant_id = uuid.uuid4()
    staging_id = uuid.uuid4()

    staged_item = InventoryStagingItem(
        id=staging_id,
        tenant_id=tenant_id,
        status="PENDING_REVIEW",
        validation_errors=["Missing asking price"],
        raw_data={"Owner name": "Sunita Verma", "Owner number": "+919876500000", "Size": "300 gaj", "Area": "Jakhan"},
        parsed_data={"title": "Villa in Jakhan", "property_type": "VILLA", "location_text": "Jakhan", "area_sqft": 2700},
        source_size="300 gaj",
        source_area="Jakhan",
        owner_name="Sunita Verma",
        owner_phone="+919876500000",
    )

    mock_res_staging = MagicMock()
    mock_res_staging.scalar_one_or_none.return_value = staged_item
    mock_db.execute.return_value = mock_res_staging

    svc = GoogleIntegrationService(mock_db, tenant_id)

    # Promote with valid price
    promote_req = InventoryStagingPromoteRequest(
        price=18_000_000,  # 1.8 Cr
        property_type="VILLA",
        title="Luxury Villa in Jakhan",
        location_text="Jakhan Dehradun",
    )

    resp = await svc.promote_staging_item(staging_id, promote_req)

    assert resp.price == 18_000_000
    assert resp.property_type == "VILLA"
    assert resp.title == "Luxury Villa in Jakhan"
    assert resp.status == "AVAILABLE"
    # Public PropertyResponse does NOT expose owner contact details
    assert not hasattr(resp, "owner_name")
    assert not hasattr(resp, "owner_phone")

    # Verify staging item was updated
    assert staged_item.status == "PROMOTED"
    assert staged_item.promoted_property_id is not None

    # Attempting to promote again raises ConflictError
    with pytest.raises(ConflictError):
        await svc.promote_staging_item(staging_id, promote_req)


@pytest.mark.asyncio
async def test_inventory_staging_tenant_isolation(mock_db):
    """
    Tenant B cannot access or promote staging items belonging to Tenant A.
    """
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()
    staging_id = uuid.uuid4()

    item_a = InventoryStagingItem(
        id=staging_id,
        tenant_id=tenant_a,
        row_index=2,
    )
    assert item_a.tenant_id == tenant_a

    # Query with Tenant B returns None (isolated)
    mock_res_none = MagicMock()
    mock_res_none.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_res_none

    svc_b = GoogleIntegrationService(mock_db, tenant_b)

    with pytest.raises(NotFoundError):
        await svc_b.get_staging_item(staging_id)

    with pytest.raises(NotFoundError):
        await svc_b.promote_staging_item(
            staging_id,
            InventoryStagingPromoteRequest(price=5000000),
        )


@pytest.mark.asyncio
async def test_inventory_sync_duplicate_suspect_routes_to_staging(mock_db):
    """
    When a synced row without explicit ID matches an existing property's owner phone and location
    but has a different external_id hash, it is staged as DUPLICATE_SUSPECT rather than overwriting.
    """
    tenant_id = uuid.uuid4()
    spreadsheet_id = "test-dup-suspect"

    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        spreadsheet_id=spreadsheet_id,
        properties_sheet_name="Inventory",
    )

    mock_res_integration = MagicMock()
    mock_res_integration.scalar_one_or_none.return_value = integration

    # Pre-existing property in the CRM
    existing_prop_id = uuid.uuid4()
    existing_prop = MagicMock(spec=Property)
    existing_prop.id = existing_prop_id
    existing_prop.external_id = "HAVN-ORIGINAL-HASH"
    existing_prop.title = "Original 4 BHK Villa"
    existing_prop.location_text = "Balbir road dalanwala"
    existing_prop.owner_phone = "+919876543210"

    mock_res_existing = MagicMock()
    mock_res_existing.scalar_one_or_none.return_value = existing_prop

    mock_db.execute.side_effect = [
        mock_res_integration,
        mock_res_existing,
    ]

    mock_sheets = MockGoogleSheetsClient()
    mock_sheets.seed_sheet(
        spreadsheet_id,
        "Inventory",
        [
            ["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price"],
            ["Vikram Malhotra", "+919876543210", "Villa", "4 BHK", "600 sq.yd", "Balbir road dalanwala", "6.5 Cr"],
        ],
    )

    service = PropertyInventorySyncService(mock_db, tenant_id, sheets_client=mock_sheets)
    result = await service.sync_inventory()

    assert result.created == 0
    assert result.updated == 0
    assert result.staged == 1
    assert result.duplicate_suspects == 1

    # Verify staged item created as DUPLICATE_SUSPECT pointing to existing property
    added_items = [call.args[0] for call in mock_db.add.call_args_list]
    assert len(added_items) == 1
    staged = added_items[0]
    assert isinstance(staged, InventoryStagingItem)
    assert staged.status == "DUPLICATE_SUSPECT"
    assert staged.duplicate_matched_property_id == existing_prop_id
    assert "Potential duplicate" in staged.validation_errors[0]


@pytest.mark.asyncio
async def test_inventory_sync_supports_rows_beyond_500(mock_db):
    """
    Open-ended range (A1:Z) correctly retrieves rows at and beyond row 500.
    """
    tenant_id = uuid.uuid4()
    spreadsheet_id = "test-large-inventory"

    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        spreadsheet_id=spreadsheet_id,
        properties_sheet_name="Inventory",
    )

    mock_res_integration = MagicMock()
    mock_res_integration.scalar_one_or_none.return_value = integration

    headers = ["Property ID", "Property type", "Asking price", "Area"]
    sheet_data = [headers]
    # Add 500 placeholder rows
    for i in range(1, 501):
        sheet_data.append([f"PROP-{i:03d}", "Apartment", "50 Lakh", "Central"])
    # Row 502 (data row 501)
    sheet_data.append(["PROP-501", "Villa", "3.5 Cr", "Mussoorie Road"])

    mock_sheets = MockGoogleSheetsClient()
    mock_sheets.seed_sheet(spreadsheet_id, "Inventory", sheet_data)

    mock_db_res_empty = MagicMock()
    mock_db_res_empty.scalar_one_or_none.return_value = None

    # Return integration first, then None for each row check
    mock_db.execute.side_effect = [mock_res_integration] + [mock_db_res_empty] * 501

    service = PropertyInventorySyncService(mock_db, tenant_id, sheets_client=mock_sheets)
    result = await service.sync_inventory()

    assert result.total_rows == 501
    assert result.created == 501
    assert result.error_count == 0

    added_props = [call.args[0] for call in mock_db.add.call_args_list if isinstance(call.args[0], Property)]
    assert len(added_props) == 501
    # Verify the row beyond 500 was successfully parsed and created
    assert added_props[-1].external_id == "PROP-501"
    assert added_props[-1].property_type == "VILLA"
    assert added_props[-1].price == 35_000_000


@pytest.mark.asyncio
async def test_inventory_sync_moved_rows_preserve_identity(mock_db):
    """
    When a spreadsheet row moves from row 2 to row 15, deterministic ID matching
    updates the existing property in place without creating a duplicate.
    """
    tenant_id = uuid.uuid4()
    spreadsheet_id = "test-moved-rows"

    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        spreadsheet_id=spreadsheet_id,
        properties_sheet_name="Inventory",
    )

    mock_res_integration = MagicMock()
    mock_res_integration.scalar_one_or_none.return_value = integration

    mock_sheets = MockGoogleSheetsClient()
    row_data = ["Ananya Sharma", "+919811122233", "Penthouse", "3 BHK", "3200 sq ft", "Jakhan Dehradun", "2.25 Cr"]

    # Run 1: row at index 2
    mock_sheets.seed_sheet(
        spreadsheet_id,
        "Inventory",
        [
            ["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price"],
            row_data,
        ],
    )

    mock_db_res_empty = MagicMock()
    mock_db_res_empty.scalar_one_or_none.return_value = None

    mock_db.execute.side_effect = [
        mock_res_integration,
        mock_db_res_empty,
    ]

    service = PropertyInventorySyncService(mock_db, tenant_id, sheets_client=mock_sheets)
    res1 = await service.sync_inventory()
    assert res1.created == 1

    created_prop = mock_db.add.call_args_list[0].args[0]
    stable_ext_id = created_prop.external_id
    mock_db.add.reset_mock()

    # Run 2: row moved down to row 15 with 13 other rows inserted above
    sheet_v2 = [["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price"]]
    for k in range(13):
        sheet_v2.append([f"Owner {k}", f"+9198000000{k:02d}", "Plot", "", "200 gaj", f"Loc {k}", "40 Lakh"])
    sheet_v2.append(row_data)  # Ananya's row now at row 15

    mock_sheets.seed_sheet(spreadsheet_id, "Inventory", sheet_v2)

    # For the moved row, mock finding the existing property
    mock_res_found = MagicMock()
    mock_res_found.scalar_one_or_none.return_value = created_prop

    # 1 integration + 13 other new rows + 1 moved row
    mock_db.execute.side_effect = [mock_res_integration] + [mock_db_res_empty] * 13 + [mock_res_found]

    res2 = await service.sync_inventory()
    assert res2.created == 13
    assert res2.updated == 1
    # Verify the moved property retained its stable external_id
    assert created_prop.external_id == stable_ext_id


@pytest.mark.asyncio
async def test_inventory_sync_changed_values_update_in_place(mock_db):
    """
    Editing price, remarks, or date in a row updates the existing Property record in place
    without creating a duplicate or changing its identity.
    """
    tenant_id = uuid.uuid4()
    spreadsheet_id = "test-changed-values"

    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        spreadsheet_id=spreadsheet_id,
        properties_sheet_name="Inventory",
    )

    mock_res_integration = MagicMock()
    mock_res_integration.scalar_one_or_none.return_value = integration

    mock_sheets = MockGoogleSheetsClient()
    mock_sheets.seed_sheet(
        spreadsheet_id,
        "Inventory",
        [
            ["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price", "Remarks"],
            ["Vikram Malhotra", "+919876543210", "Villa", "4 BHK", "480 sq.yd", "Balbir road", "5.95 Cr", "Original remarks"],
        ],
    )

    mock_db_res_empty = MagicMock()
    mock_db_res_empty.scalar_one_or_none.return_value = None

    mock_db.execute.side_effect = [
        mock_res_integration,
        mock_db_res_empty,
    ]

    service = PropertyInventorySyncService(mock_db, tenant_id, sheets_client=mock_sheets)
    res1 = await service.sync_inventory()
    assert res1.created == 1

    created_prop = mock_db.add.call_args_list[0].args[0]
    assert created_prop.price == 59_500_000
    assert "Original remarks" in created_prop.description
    mock_db.add.reset_mock()

    # Second sync: price negotiated down to 5.25 Cr, remarks updated
    mock_sheets.seed_sheet(
        spreadsheet_id,
        "Inventory",
        [
            ["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price", "Remarks"],
            ["Vikram Malhotra", "+919876543210", "Villa", "4 BHK", "480 sq.yd", "Balbir road", "5.25 Cr", "Renovated and price reduced"],
        ],
    )

    mock_res_found = MagicMock()
    mock_res_found.scalar_one_or_none.return_value = created_prop

    mock_db.execute.side_effect = [
        mock_res_integration,
        mock_res_found,
    ]

    res2 = await service.sync_inventory()
    assert res2.created == 0
    assert res2.updated == 1
    assert mock_db.add.call_count == 0
    # Values updated in place
    assert created_prop.price == 52_500_000
    assert "Renovated and price reduced" in created_prop.description


@pytest.mark.asyncio
async def test_inventory_sync_repeat_sync_no_duplicate_staging_records(mock_db):
    """
    Re-syncing an unchanged sheet with an invalid row updates the existing staging record
    in place and creates 0 duplicate staging records.
    """
    tenant_id = uuid.uuid4()
    spreadsheet_id = "test-repeat-staging"

    integration = TenantGoogleIntegration(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        is_active=True,
        spreadsheet_id=spreadsheet_id,
        properties_sheet_name="Inventory",
    )

    mock_res_integration = MagicMock()
    mock_res_integration.scalar_one_or_none.return_value = integration

    mock_sheets = MockGoogleSheetsClient()
    mock_sheets.seed_sheet(
        spreadsheet_id,
        "Inventory",
        [
            ["Owner name", "Owner number", "Property type", "Configuration", "Size", "Area", "Asking price"],
            ["Rohan Mehta", "+919988776655", "Villa", "5 BHK", "600 sq.yd", "Mussoorie Road", "Price on Request"],
        ],
    )

    # Run 1: Staging item created
    mock_db_res_none = MagicMock()
    mock_db_res_none.scalar_one_or_none.return_value = None

    mock_db.execute.side_effect = [
        mock_res_integration,
        mock_db_res_none,  # staging check returns None -> new item created
    ]

    service = PropertyInventorySyncService(mock_db, tenant_id, sheets_client=mock_sheets)
    res1 = await service.sync_inventory()
    assert res1.staged == 1
    assert mock_db.add.call_count == 1

    created_staged_item = mock_db.add.call_args_list[0].args[0]
    assert isinstance(created_staged_item, InventoryStagingItem)
    mock_db.add.reset_mock()

    # Run 2: Same sheet re-synced -> staging check returns existing item
    mock_res_staged = MagicMock()
    mock_res_staged.scalar_one_or_none.return_value = created_staged_item

    mock_db.execute.side_effect = [
        mock_res_integration,
        mock_res_staged,  # staging check returns existing item -> updated in place
    ]

    res2 = await service.sync_inventory()
    assert res2.staged == 1
    assert res2.created == 0
    # No new item added to session (0 duplicate staging records)
    assert mock_db.add.call_count == 0





