"""
Async SQLAlchemy engine, session factory, and base model.
All DB access goes through `get_db()` dependency.
"""

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncAttrs,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()

# ── Engine ────────────────────────────────────────────────────────────────────
engine = create_async_engine(
    settings.database_url,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_timeout=settings.db_pool_timeout,
    pool_pre_ping=True,  # reconnect on stale connections
    echo=settings.debug,
)

# ── Session factory ───────────────────────────────────────────────────────────
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


# ── Declarative base ──────────────────────────────────────────────────────────
class Base(AsyncAttrs, DeclarativeBase):
    """SQLAlchemy declarative base with async attribute access."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize model to dict (excludes relationships)."""
        return {
            col.name: getattr(self, col.name)
            for col in self.__table__.columns
        }


# ── FastAPI dependency ────────────────────────────────────────────────────────
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency that provides a database session.
    Rolls back on exception; always closes session.
    Fails closed if invoked in testing context against production database.
    """
    import os

    from sqlalchemy.engine import make_url

    if os.getenv("PYTEST_CURRENT_TEST"):
        url = make_url(settings.database_url)
        if url.database == "havn_crm":
            raise RuntimeError(
                "CRITICAL: get_db dependency attempted to open session against 'havn_crm' while running tests! "
                "Ensure override_get_db fixture is active."
            )

    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
