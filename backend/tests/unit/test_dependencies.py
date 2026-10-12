"""
Unit tests for core/dependencies.py:
- Role hierarchy and RBAC enforcement
- TenantContext behavior
- Token-to-TenantContext extraction
"""

import uuid

import pytest
from fastapi.security import HTTPAuthorizationCredentials

from app.core.dependencies import TenantContext, UserRole, get_current_user, require_role
from app.core.exceptions import AuthenticationError, InvalidTokenError, PermissionDeniedError
from app.core.security import create_access_token, create_refresh_token


class TestRoleHierarchy:
    def test_agent_role_permissions(self):
        ctx = TenantContext(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=UserRole.AGENT)
        assert ctx.has_role(UserRole.AGENT) is True
        assert ctx.has_role(UserRole.MANAGER) is False
        assert ctx.has_role(UserRole.AGENCY_ADMIN) is False
        assert ctx.has_role(UserRole.SUPER_ADMIN) is False
        assert ctx.is_super_admin() is False

        # require_role check
        ctx.require_role(UserRole.AGENT)
        with pytest.raises(PermissionDeniedError):
            ctx.require_role(UserRole.MANAGER)

    def test_manager_role_permissions(self):
        ctx = TenantContext(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=UserRole.MANAGER)
        assert ctx.has_role(UserRole.AGENT) is True
        assert ctx.has_role(UserRole.MANAGER) is True
        assert ctx.has_role(UserRole.AGENCY_ADMIN) is False
        assert ctx.has_role(UserRole.SUPER_ADMIN) is False

        ctx.require_role(UserRole.AGENT)
        ctx.require_role(UserRole.MANAGER)
        with pytest.raises(PermissionDeniedError):
            ctx.require_role(UserRole.AGENCY_ADMIN)

    def test_agency_admin_role_permissions(self):
        ctx = TenantContext(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=UserRole.AGENCY_ADMIN)
        assert ctx.has_role(UserRole.AGENT) is True
        assert ctx.has_role(UserRole.MANAGER) is True
        assert ctx.has_role(UserRole.AGENCY_ADMIN) is True
        assert ctx.has_role(UserRole.SUPER_ADMIN) is False

        ctx.require_role(UserRole.AGENCY_ADMIN)
        with pytest.raises(PermissionDeniedError):
            ctx.require_role(UserRole.SUPER_ADMIN)

    def test_super_admin_role_permissions(self):
        ctx = TenantContext(user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=UserRole.SUPER_ADMIN)
        assert ctx.has_role(UserRole.AGENT) is True
        assert ctx.has_role(UserRole.MANAGER) is True
        assert ctx.has_role(UserRole.AGENCY_ADMIN) is True
        assert ctx.has_role(UserRole.SUPER_ADMIN) is True
        assert ctx.is_super_admin() is True

        ctx.require_role(UserRole.SUPER_ADMIN)


class TestGetCurrentUser:
    @pytest.mark.asyncio
    async def test_valid_access_token_resolves_context(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        token = create_access_token(user_id=user_id, tenant_id=tenant_id, role="MANAGER")

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        ctx = await get_current_user(creds)

        assert ctx.user_id == user_id
        assert ctx.tenant_id == tenant_id
        assert ctx.role == UserRole.MANAGER

    @pytest.mark.asyncio
    async def test_missing_credentials_raises_auth_error(self):
        with pytest.raises(AuthenticationError):
            await get_current_user(None)

    @pytest.mark.asyncio
    async def test_refresh_token_rejected_as_access_token(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        token, _ = create_refresh_token(user_id=user_id, tenant_id=tenant_id)

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(InvalidTokenError):
            await get_current_user(creds)

    @pytest.mark.asyncio
    async def test_require_role_dependency_checker(self):
        checker = require_role(UserRole.MANAGER)
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()

        manager_ctx = TenantContext(user_id=user_id, tenant_id=tenant_id, role=UserRole.MANAGER)
        # Should succeed
        result = await checker(manager_ctx)
        assert result == manager_ctx

        agent_ctx = TenantContext(user_id=user_id, tenant_id=tenant_id, role=UserRole.AGENT)
        with pytest.raises(PermissionDeniedError):
            await checker(agent_ctx)
