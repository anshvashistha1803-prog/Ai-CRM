
"""
pytest conftest.py — test fixtures, database setup, and mock adapters.

Tests use a dedicated PostgreSQL database. Never point this configuration
at the development database.
"""

import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.database import Base, get_db
from app.core.security import hash_password
from app.integrations.google.models import (  # noqa: F401
    InventoryStagingItem,
    TenantGoogleIntegration,
)
from app.leads.models import ConsentRecord, Lead, LeadActivity  # noqa: F401
from app.main import app
from app.matching.models import LeadPropertyMatch  # noqa: F401
from app.messaging.models import OutboxMessage  # noqa: F401
from app.notifications.models import Notification  # noqa: F401
from app.properties.models import Property  # noqa: F401
from app.qualification.models import (  # noqa: F401
    LeadQualificationProposal,
)
from app.tenants.models import Tenant
from app.users.models import RefreshToken, User  # noqa: F401
from app.visits.models import SiteVisit  # noqa: F401

settings = get_settings()

# ---------------------------------------------------------------------------
# Dedicated test database
# ---------------------------------------------------------------------------

TEST_DB_URL = os.getenv("TEST_DATABASE_URL")

if not TEST_DB_URL:
    raise RuntimeError(
        "TEST_DATABASE_URL is required. Set it to the dedicated havn_test "
        "database before running tests."
    )

test_url = make_url(TEST_DB_URL)
app_url = make_url(settings.database_url)

if test_url.database != "havn_test":
    raise RuntimeError(
        f"Refusing to run tests against {test_url.database!r}. "
        "The test database must be named 'havn_test'."
    )

if (
    test_url.host == app_url.host
    and test_url.port == app_url.port
    and test_url.database == app_url.database
):
    raise RuntimeError(
        "Refusing to run tests against the application database."
    )

# NullPool avoids reusing asyncpg connections across event loops.
test_engine = create_async_engine(
    TEST_DB_URL,
    echo=False,
    poolclass=NullPool,
)

TestSessionLocal = async_sessionmaker(
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


# ---------------------------------------------------------------------------
# Database lifecycle
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def create_tables() -> AsyncGenerator[None, None]:
    """
    Create tables for a test and remove them afterward.

    This fixture is function-scoped so setup and cleanup run in the
    test's event loop. It may only operate on the guarded havn_test DB.
    """
    try:
        async with test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:
        in_ci = os.getenv("CI", "").lower() in {"true", "1", "yes"}
        require_tests = (
            os.getenv("REQUIRE_INTEGRATION_TESTS", "").lower()
            in {"true", "1", "yes"}
        )

        if in_ci or require_tests:
            pytest.fail(
                f"PostgreSQL test database is required but setup failed: {exc}"
            )

        pytest.skip(
            f"PostgreSQL test database setup failed: {exc}"
        )

    try:
        yield
    finally:
        try:
            async with test_engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
        except Exception:
            async with test_engine.begin() as conn:
                from sqlalchemy import text

                await conn.execute(
                    text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
                )
        finally:
            await test_engine.dispose()


@pytest_asyncio.fixture
async def db(
    create_tables: Any,
) -> AsyncGenerator[AsyncSession, None]:
    """
    Provide an isolated session.

    The outer transaction is rolled back after each test. The session
    uses savepoints so application code can commit without committing
    the outer test transaction.
    """
    async with test_engine.connect() as conn:
        outer_transaction = await conn.begin()

        session = TestSessionLocal(
            bind=conn,
            join_transaction_mode="create_savepoint",
        )

        try:
            yield session
        finally:
            await session.close()

            if outer_transaction.is_active:
                await outer_transaction.rollback()


# ---------------------------------------------------------------------------
# FastAPI test client
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client(
    db: AsyncSession,
) -> AsyncGenerator[AsyncClient, None]:
    """Inject the isolated test session into FastAPI routes."""

    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Tenant and user factories
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def test_tenant(db: AsyncSession) -> Tenant:
    """Create a test tenant."""
    tenant = Tenant(
        id=uuid.uuid4(),
        name="Test Agency",
        slug=f"test-agency-{uuid.uuid4().hex[:8]}",
        timezone="Asia/Kolkata",
        currency_code="INR",
        is_active=True,
        settings={},
    )
    db.add(tenant)
    await db.flush()
    return tenant


@pytest_asyncio.fixture
async def test_agent(
    db: AsyncSession,
    test_tenant: Tenant,
) -> User:
    """Create an AGENT user."""
    user = User(
        id=uuid.uuid4(),
        tenant_id=test_tenant.id,
        email="agent@test.com",
        email_normalized="agent@test.com",
        password_hash=hash_password("TestPass1!"),
        full_name="Test Agent",
        role="AGENT",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


@pytest_asyncio.fixture
async def test_manager(
    db: AsyncSession,
    test_tenant: Tenant,
) -> User:
    """Create a MANAGER user."""
    user = User(
        id=uuid.uuid4(),
        tenant_id=test_tenant.id,
        email="manager@test.com",
        email_normalized="manager@test.com",
        password_hash=hash_password("TestPass1!"),
        full_name="Test Manager",
        role="MANAGER",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


@pytest_asyncio.fixture
async def test_agent_2(
    db: AsyncSession,
    test_tenant: Tenant,
) -> User:
    """Create a second AGENT in Tenant A."""
    user = User(
        id=uuid.uuid4(),
        tenant_id=test_tenant.id,
        email="agent2@test.com",
        email_normalized="agent2@test.com",
        password_hash=hash_password("TestPass1!"),
        full_name="Second Agent",
        role="AGENT",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


@pytest_asyncio.fixture
async def test_tenant_b(db: AsyncSession) -> Tenant:
    """Create a second tenant (Tenant B)."""
    tenant = Tenant(
        id=uuid.uuid4(),
        name="Agency B",
        slug=f"agency-b-{uuid.uuid4().hex[:8]}",
        timezone="UTC",
        currency_code="USD",
        is_active=True,
        settings={},
    )
    db.add(tenant)
    await db.flush()
    return tenant


@pytest_asyncio.fixture
async def test_agent_b(
    db: AsyncSession,
    test_tenant_b: Tenant,
) -> User:
    """Create an AGENT in Tenant B."""
    user = User(
        id=uuid.uuid4(),
        tenant_id=test_tenant_b.id,
        email="agent_b@agency-b.com",
        email_normalized="agent_b@agency-b.com",
        password_hash=hash_password("TestPass1!"),
        full_name="Agent Tenant B",
        role="AGENT",
        is_active=True,
    )
    db.add(user)
    await db.flush()
    return user


# ---------------------------------------------------------------------------
# Authentication token fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def agent_token(
    test_agent: User,
    test_tenant: Tenant,
) -> str:
    """Issue a valid access token for the test agent."""
    from app.core.security import create_access_token

    return create_access_token(
        user_id=test_agent.id,
        tenant_id=test_tenant.id,
        role="AGENT",
    )


@pytest_asyncio.fixture
async def manager_token(
    test_manager: User,
    test_tenant: Tenant,
) -> str:
    """Issue a valid access token for the test manager."""
    from app.core.security import create_access_token

    return create_access_token(
        user_id=test_manager.id,
        tenant_id=test_tenant.id,
        role="MANAGER",
    )


@pytest_asyncio.fixture
async def agent_2_token(
    test_agent_2: User,
    test_tenant: Tenant,
) -> str:
    """Issue a valid token for Agent 2 in Tenant A."""
    from app.core.security import create_access_token

    return create_access_token(
        user_id=test_agent_2.id,
        tenant_id=test_tenant.id,
        role="AGENT",
    )


@pytest_asyncio.fixture
async def agent_b_token(
    test_agent_b: User,
    test_tenant_b: Tenant,
) -> str:
    """Issue a valid token for the agent in Tenant B."""
    from app.core.security import create_access_token

    return create_access_token(
        user_id=test_agent_b.id,
        tenant_id=test_tenant_b.id,
        role="AGENT",
    )


@pytest_asyncio.fixture
async def super_admin_token(test_tenant: Tenant) -> str:
    """Issue a token for the SUPER_ADMIN role."""
    from app.core.security import create_access_token

    return create_access_token(
        user_id=uuid.uuid4(),
        tenant_id=test_tenant.id,
        role="SUPER_ADMIN",
    )
