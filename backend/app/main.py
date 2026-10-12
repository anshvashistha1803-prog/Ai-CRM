"""
FastAPI application factory.
All middleware, routers, and lifecycle hooks are registered here.
"""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.middleware import RequestIDMiddleware, RequestLoggingMiddleware

# ── Structlog configuration ───────────────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO level
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

settings = get_settings()

# ── Rate limiter ──────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    structlog.get_logger().info("app_startup", environment=settings.environment)
    yield
    structlog.get_logger().info("app_shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI Real Estate CRM",
        description="Multi-tenant AI-assisted CRM for real estate agencies.",
        version="0.1.0",
        docs_url="/docs" if settings.is_development else None,   # disable in production
        redoc_url="/redoc" if settings.is_development else None,
        openapi_url="/openapi.json" if settings.is_development else None,
        lifespan=lifespan,
    )

    # Store settings on app state for access in exception handlers
    app.state.settings = settings

    # ── Rate limiter ──────────────────────────────────────────────────────────
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    # ── Custom middleware (order matters — outermost runs first) ──────────────
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(RequestIDMiddleware)

    # ── Exception handlers ────────────────────────────────────────────────────
    register_exception_handlers(app)

    # ── Routers ───────────────────────────────────────────────────────────────
    _register_routers(app)

    return app


def _register_routers(app: FastAPI) -> None:
    """Import and mount all module routers."""
    from app.auth.router import router as auth_router
    from app.integrations.google.router import router as google_router
    from app.leads.router import router as leads_router
    from app.matching.router import router as matching_router
    from app.properties.router import router as properties_router
    from app.qualification.router import router as qualification_router
    from app.tenants.router import router as tenants_router
    from app.users.router import router as users_router
    from app.visits.router import router as visits_router
    from app.webhooks.router import router as webhooks_router

    api_prefix = "/api/v1"

    app.include_router(auth_router, prefix=api_prefix)
    app.include_router(google_router, prefix=api_prefix)
    app.include_router(leads_router, prefix=api_prefix)
    app.include_router(matching_router, prefix=api_prefix)
    app.include_router(properties_router, prefix=api_prefix)
    app.include_router(qualification_router, prefix=api_prefix)
    app.include_router(tenants_router, prefix=api_prefix)
    app.include_router(users_router, prefix=api_prefix)
    app.include_router(visits_router, prefix=api_prefix)
    app.include_router(webhooks_router, prefix=api_prefix)

    # ── Health check ──────────────────────────────────────────────────────────
    @app.get("/health", tags=["ops"])
    async def health() -> dict:
        import redis.asyncio as aioredis
        from sqlalchemy import text

        from app.core.database import engine

        db_ok = False
        redis_ok = False

        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            db_ok = True
        except Exception:
            pass

        try:
            r = aioredis.from_url(settings.redis_url)
            await r.ping()
            await r.aclose()
            redis_ok = True
        except Exception:
            pass

        status = "healthy" if db_ok and redis_ok else "degraded"
        return {
            "status": status,
            "database": "ok" if db_ok else "error",
            "redis": "ok" if redis_ok else "error",
            "version": "0.1.0",
        }


# ── Entry point ───────────────────────────────────────────────────────────────
app = create_app()
