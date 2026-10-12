"""
Webhook router — Meta WhatsApp Business Cloud API webhook verification and inbound events.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.webhooks.whatsapp_service import WhatsAppWebhookService

router = APIRouter(prefix="/webhooks/whatsapp", tags=["webhooks"])


@router.get("", response_class=PlainTextResponse)
async def verify_webhook(
    hub_mode: str = Query(..., alias="hub.mode"),
    hub_challenge: str = Query(..., alias="hub.challenge"),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Webhook verification handshake for Meta WhatsApp Business Cloud API.
    Must return the raw hub.challenge as plain text upon token match.
    """
    svc = WhatsAppWebhookService(db)
    challenge = svc.verify_subscription(
        mode=hub_mode,
        token=hub_verify_token,
        challenge=hub_challenge,
    )
    return PlainTextResponse(content=challenge, status_code=200)


@router.post("")
async def receive_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Inbound message receiver for Meta WhatsApp Cloud API.
    Validates X-Hub-Signature-256 and transactionally processes leads/events.
    """
    raw_body = await request.body()
    svc = WhatsAppWebhookService(db)

    # Verify HMAC signature
    if x_hub_signature_256 and not svc.verify_signature(raw_body, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Invalid HMAC-SHA256 signature.")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.") from None

    events = await svc.process_inbound_webhook(payload)
    return {"status": "ok", "processed_events": len(events)}
