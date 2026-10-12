"""
Auth router — /auth endpoints.
Cookie-based refresh token delivery (HttpOnly, Secure, SameSite=Strict).
"""

from fastapi import APIRouter, Cookie, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.schemas import LoginRequest, LoginResponse, TokenResponse, UserSummary
from app.auth.service import AuthService
from app.core.config import get_settings
from app.core.database import get_db
from app.core.dependencies import TenantContext, get_current_user
from app.core.exceptions import AuthenticationError

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

_REFRESH_COOKIE = "crm_refresh_token"
_COOKIE_KWARGS = {
    "key": _REFRESH_COOKIE,
    "httponly": True,
    "secure": settings.is_production,  # Secure flag only in production (HTTPS)
    "samesite": "strict",
    "max_age": settings.jwt_refresh_token_expire_days * 86400,
    "path": "/auth",  # scope cookie to /auth path only
}


@router.post("/login", response_model=LoginResponse)
async def login(
    body: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    """
    Authenticate with email + password.
    Returns access token in body; refresh token set as HttpOnly cookie.
    """
    svc = AuthService(db)
    access_token, refresh_token, user = await svc.login(body.email, body.password)

    # Set refresh token as HttpOnly cookie (not in response body)
    response.set_cookie(value=refresh_token, **_COOKIE_KWARGS)

    return LoginResponse(
        token=TokenResponse(
            access_token=access_token,
            expires_in=settings.jwt_access_token_expire_minutes * 60,
        ),
        user=UserSummary.model_validate(user),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    response: Response,
    db: AsyncSession = Depends(get_db),
    refresh_token: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
) -> TokenResponse:
    """
    Issue a new access token using the HttpOnly refresh token cookie.
    The old refresh token is immediately rotated (revoked + new one issued).
    """
    if not refresh_token:
        raise AuthenticationError("No refresh token cookie found.")

    svc = AuthService(db)
    access_token, new_refresh_token, user = await svc.refresh_access_token(refresh_token)

    # Rotate refresh token cookie
    response.set_cookie(value=new_refresh_token, **_COOKIE_KWARGS)

    return TokenResponse(
        access_token=access_token,
        expires_in=settings.jwt_access_token_expire_minutes * 60,
    )


@router.post("/logout", status_code=204)
async def logout(
    response: Response,
    db: AsyncSession = Depends(get_db),
    refresh_token: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
) -> None:
    """Revoke refresh token and clear the cookie."""
    if refresh_token:
        svc = AuthService(db)
        await svc.logout(refresh_token)

    response.delete_cookie(key=_REFRESH_COOKIE, path="/auth")


@router.get("/me", response_model=UserSummary)
async def me(
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserSummary:
    """Return the currently authenticated user's profile."""
    from sqlalchemy import select

    from app.users.models import User

    result = await db.execute(
        select(User).where(User.id == ctx.user_id, User.is_deleted.is_(False))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise AuthenticationError("User not found.")
    return UserSummary.model_validate(user)

