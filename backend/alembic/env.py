"""
Alembic env.py — configured for async SQLAlchemy with environment variable override.
Imports all models so Alembic can auto-generate migrations from the ORM.
"""

import asyncio
import os

# Load FastAPI settings (reads .env)
import sys
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import get_settings
from app.core.database import Base

# ── Import ALL models here so Alembic sees them ───────────────────────────────
# Add every new model module here as the project grows.
from app.integrations.google.models import InventoryStagingItem, TenantGoogleIntegration  # noqa: F401
from app.leads.models import ConsentRecord, Lead, LeadActivity  # noqa: F401
from app.matching.models import LeadPropertyMatch  # noqa: F401
from app.properties.models import Property  # noqa: F401
from app.qualification.models import LeadQualificationProposal  # noqa: F401
from app.tenants.models import Tenant  # noqa: F401
from app.users.models import RefreshToken, User  # noqa: F401

settings = get_settings()
config = context.config

# Allow environment override for running migrations against guarded test databases
override_sync_url = os.getenv("ALEMBIC_DATABASE_URL_SYNC") or os.getenv("DATABASE_URL_SYNC") or settings.database_url_sync
config.set_main_option("sqlalchemy.url", override_sync_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without a DB connection (generates SQL script)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations using the async engine."""
    async_url = os.getenv("ALEMBIC_DATABASE_URL") or settings.database_url
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        url=async_url,  # async driver
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
