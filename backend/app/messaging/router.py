"""
FastAPI router for Operations and Dead-Letter Outbox management (/api/v1/operations/dead-letters).
Restricted strictly to SUPER_ADMIN and AGENCY_ADMIN roles. Enforces tenant isolation.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import TenantContext, UserRole, get_current_user, require_role
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.messaging.outbox_service import OutboxService
from app.messaging.schemas import (
    DeadLetterListResponse,
    DeadLetterMessageSummary,
    DeadLetterReconcileRequest,
    DeadLetterReconcileResponse,
    DeadLetterRetryRequest,
    DeadLetterRetryResponse,
)

router = APIRouter(prefix="/operations/dead-letters", tags=["Operations"])


def _mask_payload_for_operator(payload: dict | None) -> dict:
    """Mask tokens, secrets, and raw PII from payload preview."""
    if not payload:
        return {}
    clean = {}
    for k, v in payload.items():
        if any(sec in k.lower() for sec in ["token", "secret", "password", "key", "auth"]):
            clean[k] = "******"
        elif k in ["recipient_phone", "sender_phone", "phone"] and isinstance(v, str):
            from app.messaging.whatsapp_client import mask_phone_for_logging

            clean[k] = mask_phone_for_logging(v)
        else:
            clean[k] = v
    return clean


@router.get(
    "",
    response_model=DeadLetterListResponse,
    status_code=status.HTTP_200_OK,
    summary="List dead-letter messages for tenant",
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def list_dead_letters(
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    tenant_id: Annotated[uuid.UUID | None, Query()] = None,
) -> DeadLetterListResponse:
    """
    List dead-letter messages.
    AGENCY_ADMIN can only view their own tenant.
    SUPER_ADMIN can specify tenant_id or view their own tenant.
    """
    target_tenant_id = context.tenant_id
    if context.role == UserRole.SUPER_ADMIN and tenant_id is not None:
        target_tenant_id = tenant_id
    elif context.role == UserRole.AGENCY_ADMIN and tenant_id is not None and tenant_id != context.tenant_id:
        raise PermissionDeniedError("AGENCY_ADMIN cannot inspect dead-letter messages of other agencies.")

    svc = OutboxService(db)
    messages = await svc.list_dead_letters(target_tenant_id, limit=limit, offset=offset)

    items = [
        DeadLetterMessageSummary(
            id=m.id,
            tenant_id=m.tenant_id,
            event_type=m.event_type,
            aggregate_type=m.aggregate_type,
            aggregate_id=m.aggregate_id,
            status=m.status,
            retry_count=m.retry_count,
            max_retries=m.max_retries,
            last_error=m.last_error,
            created_at=m.created_at,
            updated_at=m.updated_at,
            payload_preview=_mask_payload_for_operator(m.payload),
        )
        for m in messages
    ]

    return DeadLetterListResponse(
        items=items,
        total=len(items),
        limit=limit,
        offset=offset,
    )


@router.post(
    "/{message_id}/retry",
    response_model=DeadLetterRetryResponse,
    status_code=status.HTTP_200_OK,
    summary="Retry a dead-letter message",
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def retry_dead_letter(
    message_id: uuid.UUID,
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    body: DeadLetterRetryRequest | None = None,
    tenant_id: Annotated[uuid.UUID | None, Query()] = None,
) -> DeadLetterRetryResponse:
    """
    Retry a message currently in DEAD_LETTER status.
    Transactional row-lock prevents race conditions.
    Validates that business action has not already succeeded.
    Requires verified reconciliation or audited override if delivery state is ambiguous.
    """
    target_tenant_id = context.tenant_id
    if context.role == UserRole.SUPER_ADMIN and tenant_id is not None:
        target_tenant_id = tenant_id
    elif context.role == UserRole.AGENCY_ADMIN and tenant_id is not None and tenant_id != context.tenant_id:
        raise PermissionDeniedError("AGENCY_ADMIN cannot retry dead-letter messages of other agencies.")

    svc = OutboxService(db)
    retried_msg = await svc.retry_dead_letter(
        message_id=message_id,
        tenant_id=target_tenant_id,
        operator_id=context.user_id,
        reconciliation_outcome=body.reconciliation_outcome if body else None,
        allow_duplicate_risk_override=body.allow_duplicate_risk_override if body else False,
        override_reason=body.override_reason if body else None,
    )

    if not retried_msg:
        raise NotFoundError("Dead-letter message not found or not in DEAD_LETTER status.")

    await db.commit()

    return DeadLetterRetryResponse(
        message_id=retried_msg.id,
        tenant_id=retried_msg.tenant_id,
        status=retried_msg.status,
        retried_by=context.user_id,
        detail="Message reset to PENDING for outbox worker processing.",
    )


@router.post(
    "/{message_id}/reconcile",
    response_model=DeadLetterReconcileResponse,
    status_code=status.HTTP_200_OK,
    summary="Reconcile an ambiguous delivery dead-letter message",
    dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))],
)
async def reconcile_dead_letter(
    message_id: uuid.UUID,
    body: DeadLetterReconcileRequest,
    context: Annotated[TenantContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    tenant_id: Annotated[uuid.UUID | None, Query()] = None,
) -> DeadLetterReconcileResponse:
    """
    Dedicated reconciliation workflow for ambiguous delivery dead-letters.
    Requires outcome CONFIRMED_DELIVERED or CONFIRMED_UNDELIVERED.
    """
    target_tenant_id = context.tenant_id
    if context.role == UserRole.SUPER_ADMIN and tenant_id is not None:
        target_tenant_id = tenant_id
    elif context.role == UserRole.AGENCY_ADMIN and tenant_id is not None and tenant_id != context.tenant_id:
        raise PermissionDeniedError("AGENCY_ADMIN cannot reconcile dead-letter messages of other agencies.")

    svc = OutboxService(db)
    reconciled_msg = await svc.reconcile_dead_letter(
        message_id=message_id,
        tenant_id=target_tenant_id,
        reconciliation_outcome=body.reconciliation_outcome,
        provider_message_id=body.provider_message_id,
        notes=body.notes,
        operator_id=context.user_id,
    )

    await db.commit()

    return DeadLetterReconcileResponse(
        message_id=reconciled_msg.id,
        tenant_id=reconciled_msg.tenant_id,
        status=reconciled_msg.status,
        reconciliation_outcome=body.reconciliation_outcome.upper(),
        reconciled_by=context.user_id,
        detail=f"Message reconciliation completed with outcome '{body.reconciliation_outcome.upper()}'.",
    )
