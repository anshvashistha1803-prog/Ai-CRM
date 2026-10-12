"""
Unit tests for AuthService.

Covers:
- Successful login returns tokens and correct user object.
- Login with wrong password raises AuthenticationError.
- Login with unknown email raises AuthenticationError.
- Login with deactivated user raises AuthenticationError.
- Refresh token rotation works and invalidates previous token.
- Refresh with invalid/expired token raises InvalidTokenError.
- Refresh with non-refresh token raises InvalidTokenError.
- Refresh for deactivated user raises AuthenticationError.
- Logout revokes the specific token.
- Logout all revokes all tokens for a user.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.auth.service import AuthService, _hash_token
from app.core.exceptions import AuthenticationError, InvalidTokenError
from app.core.security import create_access_token, create_refresh_token, hash_password
from app.users.models import RefreshToken, User


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


@pytest.fixture
def active_user():
    user = User()
    user.id = uuid.uuid4()
    user.tenant_id = uuid.uuid4()
    user.email = "agent@agency.com"
    user.email_normalized = "agent@agency.com"
    user.password_hash = hash_password("ValidPassword123!")
    user.role = "agent"
    user.is_active = True
    user.is_deleted = False
    return user


@pytest.mark.asyncio
async def test_login_success(mock_db, active_user):
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = active_user
    mock_db.execute.return_value = mock_res

    auth_svc = AuthService(mock_db)
    access_token, refresh_token, user = await auth_svc.login("agent@agency.com", "ValidPassword123!")

    assert access_token is not None
    assert refresh_token is not None
    assert user.id == active_user.id
    mock_db.add.assert_called_once()
    added_obj = mock_db.add.call_args[0][0]
    assert isinstance(added_obj, RefreshToken)
    assert added_obj.user_id == active_user.id


@pytest.mark.asyncio
async def test_login_invalid_password(mock_db, active_user):
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = active_user
    mock_db.execute.return_value = mock_res

    auth_svc = AuthService(mock_db)
    with pytest.raises(AuthenticationError, match="Invalid email or password"):
        await auth_svc.login("agent@agency.com", "WrongPassword!")


@pytest.mark.asyncio
async def test_login_unknown_email(mock_db):
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_res

    auth_svc = AuthService(mock_db)
    with pytest.raises(AuthenticationError, match="Invalid email or password"):
        await auth_svc.login("unknown@agency.com", "SomePassword123!")


@pytest.mark.asyncio
async def test_login_inactive_user(mock_db, active_user):
    active_user.is_active = False
    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = active_user
    mock_db.execute.return_value = mock_res

    auth_svc = AuthService(mock_db)
    with pytest.raises(AuthenticationError, match="deactivated"):
        await auth_svc.login("agent@agency.com", "ValidPassword123!")


@pytest.mark.asyncio
async def test_refresh_token_success(mock_db, active_user):
    refresh_token, _ = create_refresh_token(active_user.id, active_user.tenant_id)
    token_hash = _hash_token(refresh_token)

    db_token = RefreshToken()
    db_token.id = uuid.uuid4()
    db_token.user_id = active_user.id
    db_token.token_hash = token_hash
    db_token.revoked_at = None
    db_token.expires_at = datetime.now(UTC) + timedelta(days=7)

    # First execute for token lookup, second for user lookup
    mock_token_res = MagicMock()
    mock_token_res.scalar_one_or_none.return_value = db_token
    mock_user_res = MagicMock()
    mock_user_res.scalar_one_or_none.return_value = active_user

    mock_db.execute.side_effect = [mock_token_res, mock_user_res]

    auth_svc = AuthService(mock_db)
    new_access_token, new_refresh_token, user = await auth_svc.refresh_access_token(refresh_token)

    assert new_access_token is not None
    assert new_refresh_token is not None
    assert new_refresh_token != refresh_token
    assert db_token.revoked_at is not None
    assert user.id == active_user.id


@pytest.mark.asyncio
async def test_refresh_token_expired_or_not_found(mock_db, active_user):
    refresh_token, _ = create_refresh_token(active_user.id, active_user.tenant_id)

    mock_token_res = MagicMock()
    mock_token_res.scalar_one_or_none.return_value = None
    mock_db.execute.return_value = mock_token_res

    auth_svc = AuthService(mock_db)
    with pytest.raises(InvalidTokenError, match="Refresh token not found or already used"):
        await auth_svc.refresh_access_token(refresh_token)


@pytest.mark.asyncio
async def test_refresh_token_wrong_token_type(mock_db, active_user):
    access_token = create_access_token(active_user.id, active_user.tenant_id, active_user.role)

    auth_svc = AuthService(mock_db)
    with pytest.raises(InvalidTokenError, match="Not a refresh token"):
        await auth_svc.refresh_access_token(access_token)


@pytest.mark.asyncio
async def test_refresh_token_malformed(mock_db):
    auth_svc = AuthService(mock_db)
    with pytest.raises(InvalidTokenError, match="Invalid or expired"):
        await auth_svc.refresh_access_token("not-a-valid-jwt-token")


@pytest.mark.asyncio
async def test_refresh_token_inactive_user(mock_db, active_user):
    active_user.is_active = False
    refresh_token, _ = create_refresh_token(active_user.id, active_user.tenant_id)
    token_hash = _hash_token(refresh_token)

    db_token = RefreshToken()
    db_token.id = uuid.uuid4()
    db_token.user_id = active_user.id
    db_token.token_hash = token_hash
    db_token.revoked_at = None
    db_token.expires_at = datetime.now(UTC) + timedelta(days=7)

    mock_token_res = MagicMock()
    mock_token_res.scalar_one_or_none.return_value = db_token
    mock_user_res = MagicMock()
    mock_user_res.scalar_one_or_none.return_value = active_user

    mock_db.execute.side_effect = [mock_token_res, mock_user_res]

    auth_svc = AuthService(mock_db)
    with pytest.raises(AuthenticationError, match="inactive"):
        await auth_svc.refresh_access_token(refresh_token)


@pytest.mark.asyncio
async def test_logout_revokes_token(mock_db):
    raw_token = "some_sample_refresh_token"
    token_hash = _hash_token(raw_token)

    db_token = RefreshToken()
    db_token.id = uuid.uuid4()
    db_token.token_hash = token_hash
    db_token.revoked_at = None

    mock_res = MagicMock()
    mock_res.scalar_one_or_none.return_value = db_token
    mock_db.execute.return_value = mock_res

    auth_svc = AuthService(mock_db)
    await auth_svc.logout(raw_token)

    assert db_token.revoked_at is not None
    mock_db.flush.assert_called_once()


@pytest.mark.asyncio
async def test_logout_all_users_tokens(mock_db):
    user_id = uuid.uuid4()
    auth_svc = AuthService(mock_db)
    await auth_svc.logout_all(user_id)

    mock_db.execute.assert_called_once()
    mock_db.flush.assert_called_once()
