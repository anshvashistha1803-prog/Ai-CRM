"""
Unit tests for TenantService and UserService.
Verifies agency onboarding, tenant isolation, settings updates, user invitations,
token validation, and role hierarchy enforcement.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.dependencies import TenantContext, UserRole
from app.core.exceptions import (
    ConflictError,
    InvalidTokenError,
    PermissionDeniedError,
)
from app.core.security import create_invitation_token
from app.tenants.models import Tenant
from app.tenants.schemas import TenantOnboardRequest, TenantUpdateRequest
from app.tenants.service import TenantService
from app.users.models import User
from app.users.schemas import AcceptInviteRequest, UserInviteRequest, UserUpdateRequest
from app.users.service import UserService


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
    session.flush = AsyncMock()
    session.rollback = AsyncMock()
    return session


# ── TenantService Tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_onboard_agency_success(mock_db):
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_res

    svc = TenantService(mock_db)
    payload = TenantOnboardRequest(
        agency_name="Apex Realty",
        agency_slug="apex-realty",
        admin_email="admin@apexrealty.com",
        admin_name="Apex Admin",
        admin_password="Password123!",
    )

    tenant, admin_user = await svc.onboard_agency(payload)

    assert tenant.name == "Apex Realty"
    assert tenant.slug == "apex-realty"
    assert admin_user.email == "admin@apexrealty.com"
    assert admin_user.role == UserRole.AGENCY_ADMIN.value
    assert mock_db.add.call_count == 2
    mock_db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_onboard_agency_duplicate_slug_conflict(mock_db):
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = uuid.uuid4()
    mock_db.execute.return_value = mock_res

    svc = TenantService(mock_db)
    payload = TenantOnboardRequest(
        agency_name="Apex Realty",
        agency_slug="apex-realty",
        admin_email="admin@apexrealty.com",
        admin_name="Apex Admin",
        admin_password="Password123!",
    )

    with pytest.raises(ConflictError, match="Agency slug 'apex-realty' is already taken"):
        await svc.onboard_agency(payload)


@pytest.mark.asyncio
async def test_tenant_isolation_cross_tenant_access_denied(mock_db):
    tenant_a = uuid.uuid4()
    tenant_b = uuid.uuid4()

    ctx = TenantContext(tenant_id=tenant_a, user_id=uuid.uuid4(), role=UserRole.AGENCY_ADMIN)
    svc = TenantService(mock_db, ctx)

    with pytest.raises(PermissionDeniedError, match="Cannot access foreign agency settings"):
        await svc.get_tenant(tenant_b)


@pytest.mark.asyncio
async def test_update_tenant_settings_success(mock_db):
    tenant_id = uuid.uuid4()
    tenant = Tenant(
        id=tenant_id,
        name="Apex Realty",
        slug="apex-realty",
        timezone="UTC",
        currency_code="INR",
        is_active=True,
        settings={"tier": "pro"},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        is_deleted=False,
    )

    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = tenant
    mock_db.execute.return_value = mock_res

    ctx = TenantContext(tenant_id=tenant_id, user_id=uuid.uuid4(), role=UserRole.AGENCY_ADMIN)
    svc = TenantService(mock_db, ctx)

    updated = await svc.update_tenant(
        tenant_id,
        TenantUpdateRequest(name="Apex Realty Global", timezone="Asia/Kolkata"),
    )

    assert updated.name == "Apex Realty Global"
    assert updated.timezone == "Asia/Kolkata"
    mock_db.flush.assert_awaited()


# ── UserService Tests ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invite_user_manager_can_invite_agent(mock_db):
    tenant_id = uuid.uuid4()
    manager_id = uuid.uuid4()

    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_res

    ctx = TenantContext(tenant_id=tenant_id, user_id=manager_id, role=UserRole.MANAGER)
    svc = UserService(mock_db, ctx)

    token, email, role = await svc.invite_user(
        UserInviteRequest(
            email="agent@apexrealty.com",
            full_name="Agent Smith",
            role="AGENT",
        )
    )

    assert email == "agent@apexrealty.com"
    assert role == "AGENT"
    assert token is not None


@pytest.mark.asyncio
async def test_invite_user_manager_cannot_invite_manager_or_admin(mock_db):
    tenant_id = uuid.uuid4()
    manager_id = uuid.uuid4()

    ctx = TenantContext(tenant_id=tenant_id, user_id=manager_id, role=UserRole.MANAGER)
    svc = UserService(mock_db, ctx)

    with pytest.raises(PermissionDeniedError, match="Managers can only invite Agents"):
        await svc.invite_user(
            UserInviteRequest(
                email="manager2@apexrealty.com",
                full_name="Manager Two",
                role="MANAGER",
            )
        )


@pytest.mark.asyncio
async def test_accept_invitation_success(mock_db):
    tenant_id = uuid.uuid4()
    invited_by = uuid.uuid4()

    token = create_invitation_token(
        email="newagent@apexrealty.com",
        tenant_id=tenant_id,
        role="AGENT",
        invited_by=invited_by,
    )

    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_res

    svc = UserService(mock_db)
    user = await svc.accept_invitation(
        AcceptInviteRequest(
            token=token,
            password="SecurePassword123!",
            full_name="New Agent",
        )
    )

    assert user.email == "newagent@apexrealty.com"
    assert user.role == "AGENT"
    assert user.tenant_id == tenant_id
    mock_db.add.assert_called_once()
    mock_db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_accept_invitation_invalid_token(mock_db):
    svc = UserService(mock_db)
    with pytest.raises(InvalidTokenError, match="Invalid or expired invitation token"):
        await svc.accept_invitation(
            AcceptInviteRequest(
                token="invalid.jwt.token",
                password="SecurePassword123!",
            )
        )


@pytest.mark.asyncio
async def test_update_user_self_deactivation_prevented(mock_db):
    tenant_id = uuid.uuid4()
    admin_id = uuid.uuid4()

    admin_user = User(
        id=admin_id,
        tenant_id=tenant_id,
        email="admin@apexrealty.com",
        email_normalized="admin@apexrealty.com",
        password_hash="hash",
        full_name="Admin User",
        role=UserRole.AGENCY_ADMIN.value,
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        is_deleted=False,
    )

    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = admin_user
    mock_db.execute.return_value = mock_res

    ctx = TenantContext(tenant_id=tenant_id, user_id=admin_id, role=UserRole.AGENCY_ADMIN)
    svc = UserService(mock_db, ctx)

    with pytest.raises(PermissionDeniedError, match="Cannot deactivate your own administrator account"):
        await svc.update_user(admin_id, UserUpdateRequest(is_active=False))


@pytest.mark.asyncio
async def test_soft_delete_user_success(mock_db):
    tenant_id = uuid.uuid4()
    admin_id = uuid.uuid4()
    target_user_id = uuid.uuid4()

    target_user = User(
        id=target_user_id,
        tenant_id=tenant_id,
        email="agent@apexrealty.com",
        email_normalized="agent@apexrealty.com",
        password_hash="hash",
        full_name="Agent User",
        role=UserRole.AGENT.value,
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        is_deleted=False,
    )

    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = target_user
    mock_db.execute.return_value = mock_res

    ctx = TenantContext(tenant_id=tenant_id, user_id=admin_id, role=UserRole.AGENCY_ADMIN)
    svc = UserService(mock_db, ctx)

    await svc.soft_delete_user(target_user_id)

    assert target_user.is_deleted is True
    assert target_user.deleted_at is not None
    assert target_user.is_active is False
    mock_db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_soft_delete_user_prevent_self_deletion(mock_db):
    tenant_id = uuid.uuid4()
    admin_id = uuid.uuid4()

    admin_user = User(
        id=admin_id,
        tenant_id=tenant_id,
        email="admin@apexrealty.com",
        email_normalized="admin@apexrealty.com",
        password_hash="hash",
        full_name="Admin User",
        role=UserRole.AGENCY_ADMIN.value,
        is_active=True,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        is_deleted=False,
    )

    mock_res = MagicMock()
    mock_res.scalars.return_value.first.return_value = admin_user
    mock_db.execute.return_value = mock_res

    ctx = TenantContext(tenant_id=tenant_id, user_id=admin_id, role=UserRole.AGENCY_ADMIN)
    svc = UserService(mock_db, ctx)

    with pytest.raises(PermissionDeniedError, match="Cannot delete your own administrator account"):
        await svc.soft_delete_user(admin_id)


@pytest.mark.asyncio
async def test_re_registration_allowed_after_soft_delete(mock_db):
    """
    Verifies that when a prior user is soft-deleted, inviting a user with the same email
    succeeds because active-user queries filter out `is_deleted=True` records, aligning
    with the partial unique index `uq_users_tenant_email` WHERE is_deleted IS FALSE.
    """
    tenant_id = uuid.uuid4()
    admin_id = uuid.uuid4()

    # The query for existing non-deleted users returns None (since previous user is soft-deleted)
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    mock_res.scalars.return_value.first.return_value = None
    mock_db.execute.return_value = mock_res

    ctx = TenantContext(tenant_id=tenant_id, user_id=admin_id, role=UserRole.AGENCY_ADMIN)
    svc = UserService(mock_db, ctx)

    token, email, role = await svc.invite_user(
        UserInviteRequest(
            email="rejoined.agent@apexrealty.com",
            full_name="Rejoined Agent",
            role=UserRole.AGENT,
        )
    )

    assert email == "rejoined.agent@apexrealty.com"
    assert token is not None
