"""
Webhook router — Meta WhatsApp Business Cloud API & Meta Lead Ads webhooks.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.webhooks.meta_service import MetaLeadAdsWebhookService
from app.webhooks.whatsapp_service import WhatsAppWebhookService

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
whatsapp_router = APIRouter(prefix="/whatsapp")
meta_router = APIRouter(prefix="/meta-leadgen")


# ── WhatsApp Cloud API Webhook ────────────────────────────────────────────────
@whatsapp_router.get("", response_class=PlainTextResponse)
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


@whatsapp_router.post("")
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
    if not x_hub_signature_256:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header.")

    if not svc.verify_signature(raw_body, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Invalid HMAC-SHA256 signature.")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.") from None

    events = await svc.process_inbound_webhook(payload)
    return {"status": "ok", "processed_events": len(events)}


# ── Meta Lead Ads Webhook ─────────────────────────────────────────────────────
@meta_router.get("", response_class=PlainTextResponse)
async def verify_meta_leadgen_webhook(
    hub_mode: str = Query(..., alias="hub.mode"),
    hub_challenge: str = Query(..., alias="hub.challenge"),
    hub_verify_token: str = Query(..., alias="hub.verify_token"),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """
    Webhook verification handshake for Meta Lead Ads intake.
    Returns the raw hub.challenge upon verification token match.
    """
    svc = MetaLeadAdsWebhookService(db)
    challenge = svc.verify_subscription(
        mode=hub_mode,
        token=hub_verify_token,
        challenge=hub_challenge,
    )
    return PlainTextResponse(content=challenge, status_code=200)


@meta_router.post("")
async def receive_meta_leadgen_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Inbound lead notification receiver for Meta Lead Ads.
    Validates X-Hub-Signature-256 with constant-time check and transactionally ingests leads.
    """
    raw_body = await request.body()
    svc = MetaLeadAdsWebhookService(db)

    # Verify HMAC signature
    if not x_hub_signature_256:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header.")

    if not svc.verify_signature(raw_body, x_hub_signature_256):
        raise HTTPException(status_code=403, detail="Invalid HMAC-SHA256 signature.")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.") from None

    events = await svc.process_inbound_webhook(payload)
    return {"status": "ok", "processed_events": len(events), "details": events}


router.include_router(whatsapp_router)
router.include_router(meta_router)
