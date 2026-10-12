"""
Unit tests for development bootstrap procedure.

Verifies:
1. Creation of tenant and initial active MANAGER user.
2. Production guard: strictly refuses to run in production.
3. Idempotency: repeated runs do not duplicate tenant or user.
4. Reactivation of soft-deleted or deactivated users.
5. Password strength and email format validation.
6. Integration with AuthService: bootstrapped user can successfully authenticate.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.auth.service import AuthService
from app.commands.bootstrap import (
    bootstrap_dev_environment,
    resolve_password,
    validate_bootstrap_email,
    validate_bootstrap_password,
)
from app.core.config import Settings
from app.core.dependencies import UserRole
from app.core.security import verify_password
from app.tenants.models import Tenant
from app.users.models import User


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


@pytest.mark.asyncio
async def test_bootstrap_creates_tenant_and_manager(mock_db):
    """Bootstrap creates a tenant and an initial active MANAGER user."""
    mock_res_empty = MagicMock()
    mock_res_empty.scalar_one_or_none.return_value = None
    mock_db.execute.side_effect = [mock_res_empty, mock_res_empty]

    tenant, user, t_created, u_created = await bootstrap_dev_environment(
        mock_db,
        email="manager@havnrealty.com",
        password="ValidPassword123!",
        tenant_name="HAVN Realty",
        tenant_slug="havn",
        full_name="HAVN Manager",
        role=UserRole.MANAGER.value,
    )

    assert t_created is True
    assert u_created is True
    assert tenant.name == "HAVN Realty"
    assert tenant.slug == "havn"
    assert tenant.is_active is True

    assert user.email == "manager@havnrealty.com"
    assert user.email_normalized == "manager@havnrealty.com"
    assert user.role == UserRole.MANAGER.value
    assert user.is_active is True
    assert user.is_deleted is False

    # Password is securely hashed with bcrypt
    assert verify_password("ValidPassword123!", user.password_hash) is True
    assert mock_db.add.call_count == 2


@pytest.mark.asyncio
async def test_bootstrap_refuses_in_production(mock_db):
    """Bootstrap strictly refuses to run if environment is production."""
    prod_settings = Settings(
        environment="production",
        secret_key="a" * 32,
        app_encryption_key="b" * 32,
    )

    with pytest.raises(RuntimeError, match="strictly disabled in production"):
        await bootstrap_dev_environment(
            mock_db,
            email="manager@havnrealty.com",
            password="ValidPassword123!",
            settings_override=prod_settings,
        )

    assert mock_db.add.call_count == 0


@pytest.mark.asyncio
async def test_bootstrap_is_idempotent_no_duplicates(mock_db):
    """
    When tenant and user already exist, bootstrap updates them in place
    and creates zero duplicate records.
    """
    existing_tenant = Tenant(
        id=uuid.uuid4(),
        name="HAVN Realty",
        slug="havn",
        is_active=True,
    )
    existing_user = User(
        id=uuid.uuid4(),
        tenant_id=existing_tenant.id,
        email="manager@havnrealty.com",
        email_normalized="manager@havnrealty.com",
        password_hash="old_hash",
        full_name="Existing Manager",
        role=UserRole.MANAGER.value,
        is_active=True,
    )

    mock_res_tenant = MagicMock()
    mock_res_tenant.scalar_one_or_none.return_value = existing_tenant

    mock_res_user = MagicMock()
    mock_res_user.scalar_one_or_none.return_value = existing_user

    mock_db.execute.side_effect = [mock_res_tenant, mock_res_user]

    tenant, user, t_created, u_created = await bootstrap_dev_environment(
        mock_db,
        email="manager@havnrealty.com",
        password="NewValidPassword123!",
        tenant_slug="havn",
    )

    assert t_created is False
    assert u_created is False
    assert tenant.id == existing_tenant.id
    assert user.id == existing_user.id
    assert verify_password("NewValidPassword123!", user.password_hash) is True
    # Zero items added to session (idempotent update in place)
    assert mock_db.add.call_count == 0


@pytest.mark.asyncio
async def test_bootstrap_reactivates_soft_deleted_user(mock_db):
    """Reactivates user and tenant if previously deactivated or soft-deleted."""
    tenant = Tenant(
        id=uuid.uuid4(),
        name="HAVN Realty",
        slug="havn",
        is_active=False,
        is_deleted=True,
    )
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email="manager@havnrealty.com",
        email_normalized="manager@havnrealty.com",
        password_hash="old_hash",
        full_name="Deleted Manager",
        role=UserRole.AGENT.value,
        is_active=False,
        is_deleted=True,
    )

    mock_res_t = MagicMock()
    mock_res_t.scalar_one_or_none.return_value = tenant
    mock_res_u = MagicMock()
    mock_res_u.scalar_one_or_none.return_value = user
    mock_db.execute.side_effect = [mock_res_t, mock_res_u]

    _, reactivated_user, _, _ = await bootstrap_dev_environment(
        mock_db,
        email="manager@havnrealty.com",
        password="NewValidPassword123!",
        role=UserRole.MANAGER.value,
    )

    assert tenant.is_active is True
    assert tenant.is_deleted is False
    assert reactivated_user.is_active is True
    assert reactivated_user.is_deleted is False
    assert reactivated_user.role == UserRole.MANAGER.value


def test_password_strength_validation():
    """Validates that password rules are strictly enforced."""
    with pytest.raises(ValueError, match="between 10 and 128"):
        validate_bootstrap_password("Short1!")

    with pytest.raises(ValueError, match="at least one uppercase letter"):
        validate_bootstrap_password("lowercase123!@#")

    with pytest.raises(ValueError, match="at least one digit"):
        validate_bootstrap_password("NoDigitsHere!@#")

    with pytest.raises(ValueError, match="at least one special character"):
        validate_bootstrap_password("NoSpecialChar1234")

    # Valid password
    valid = validate_bootstrap_password("CorrectPass123!")
    assert valid == "CorrectPass123!"


def test_email_validation():
    """Validates email format and normalization."""
    with pytest.raises(ValueError, match="Email cannot be empty"):
        validate_bootstrap_email("")

    with pytest.raises(ValueError, match="Invalid email address"):
        validate_bootstrap_email("not-an-email")

    assert validate_bootstrap_email("  Manager@HavnRealty.COM ") == "manager@havnrealty.com"


@pytest.mark.asyncio
async def test_bootstrapped_user_can_login_via_auth_service(mock_db):
    """Verifies that an account created by bootstrap successfully logs in via AuthService."""
    # 1. Bootstrap user
    mock_res_empty = MagicMock()
    mock_res_empty.scalar_one_or_none.return_value = None
    mock_db.execute.side_effect = [mock_res_empty, mock_res_empty]

    _, bootstrapped_user, _, _ = await bootstrap_dev_environment(
        mock_db,
        email="manager@havnrealty.com",
        password="ValidPassword123!",
    )

    # 2. Login via AuthService (selects user, then updates last_login_at)
    mock_res_user = MagicMock()
    mock_res_user.scalar_one_or_none.return_value = bootstrapped_user
    mock_res_update = MagicMock()
    mock_db.execute.side_effect = [mock_res_user, mock_res_update]

    auth_svc = AuthService(mock_db)
    access_token, refresh_token, authenticated_user = await auth_svc.login(
        "manager@havnrealty.com", "ValidPassword123!"
    )

    assert access_token is not None
    assert refresh_token is not None
    assert authenticated_user.id == bootstrapped_user.id
    assert authenticated_user.role == UserRole.MANAGER.value


def test_resolve_password_variants(monkeypatch):
    """Test resolution of password via CLI arg, environment variable, or fallback."""
    args1 = MagicMock(password="ArgPass123!", password_env=None, password_stdin=False)
    assert resolve_password(args1) == "ArgPass123!"

    monkeypatch.setenv("TEST_BOOTSTRAP_PW", "EnvPass123!")
    args2 = MagicMock(password=None, password_env="TEST_BOOTSTRAP_PW", password_stdin=False)
    assert resolve_password(args2) == "EnvPass123!"

    monkeypatch.setenv("BOOTSTRAP_PASSWORD", "FallbackPass123!")
    args3 = MagicMock(password=None, password_env=None, password_stdin=False)
    assert resolve_password(args3) == "FallbackPass123!"
