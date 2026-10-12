"""
User router — endpoints for user invitations, acceptance, team listings, and updates.
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import TenantContext, get_current_user
from app.users.schemas import (
    AcceptInviteRequest,
    UserInviteRequest,
    UserInviteResponse,
    UserResponse,
    UserUpdateRequest,
)
from app.users.service import UserService

router = APIRouter(prefix="/users", tags=["users"])


@router.post("/invite", response_model=UserInviteResponse)
async def invite_user(
    body: UserInviteRequest,
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserInviteResponse:
    """Invite a new team member to the agency (requires MANAGER or higher)."""
    svc = UserService(db, ctx)
    token, email, role = await svc.invite_user(body)
    return UserInviteResponse(
        email=email,
        role=role,
        invitation_token=token,
    )


@router.post("/accept-invite", response_model=UserResponse, status_code=201)
async def accept_invitation(
    body: AcceptInviteRequest,
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Accept an agency invitation using an invitation token (public endpoint)."""
    svc = UserService(db)
    user = await svc.accept_invitation(body)
    return UserResponse.model_validate(user)


@router.get("", response_model=list[UserResponse])
async def list_users(
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[UserResponse]:
    """List all team members in the currently authenticated agency."""
    svc = UserService(db, ctx)
    users = await svc.list_users()
    return [UserResponse.model_validate(u) for u in users]


@router.get("/{user_id}", response_model=UserResponse)
async def get_user(
    user_id: uuid.UUID,
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Retrieve details of a team member in the agency."""
    svc = UserService(db, ctx)
    user = await svc.get_user(user_id)
    return UserResponse.model_validate(user)


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdateRequest,
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    """Update a team member's role or status (requires AGENCY_ADMIN)."""
    svc = UserService(db, ctx)
    user = await svc.update_user(user_id, body)
    return UserResponse.model_validate(user)


@router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: uuid.UUID,
    ctx: TenantContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft delete a team member (requires AGENCY_ADMIN)."""
    svc = UserService(db, ctx)
    await svc.soft_delete_user(user_id)
