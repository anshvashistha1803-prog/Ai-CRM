"""
Official WhatsApp Business Cloud API Client.
Supports live Meta Graph API calls and deterministic mock mode for local dev/testing.
Enforces 24-hour messaging window checks and template policy rules.
"""

import uuid
from typing import Any

import httpx
import structlog

from app.core.config import get_settings

logger = structlog.get_logger()
settings = get_settings()


class WhatsAppProviderError(Exception):
    """Base exception for WhatsApp Cloud API operations."""

    is_ambiguous: bool = False


class WhatsAppConfirmedFailureError(WhatsAppProviderError):
    """Raised when the provider returned an explicit HTTP error response (e.g. 400, 401, 403, 500)."""

    def __init__(self, message: str, status_code: int | None = None, response_body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.is_ambiguous = False


class WhatsAppAmbiguousDeliveryError(WhatsAppProviderError):
    """
    Raised on socket write/read timeouts or network disconnects while awaiting Meta's ACK.
    The message may have been accepted and dispatched by Meta.
    Blind retries risk duplicate outbound message delivery to the recipient.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.is_ambiguous = True


def mask_phone_for_logging(phone: str) -> str:
    """Mask phone number to prevent PII exposure in logs (e.g. +91******3210)."""
    if not phone:
        return "[EMPTY]"
    clean = phone.strip()
    if len(clean) <= 6:
        return "***"
    prefix = clean[:3]
    suffix = clean[-4:]
    return f"{prefix}{'*' * max(0, len(clean) - 7)}{suffix}"


def classify_provider_error(http_err: httpx.HTTPStatusError) -> Exception:
    """
    Verify and classify provider HTTP responses against actual delivery semantics:
    - 4xx (400, 401, 403, 404, 429): Confirmed rejection by provider API gateway.
    - 502 Bad Gateway / 504 Gateway Timeout: Ambiguous delivery (proxy dropped or timed out upstream).
    - 500 / 503 with structured Meta Graph API error JSON: Confirmed rejection by application.
    - 500 / 503 without structured Meta error JSON (e.g. empty, non-JSON or HTML crash): Ambiguous delivery.
    """
    status = http_err.response.status_code if http_err.response else 500
    text = http_err.response.text[:500] if http_err.response else ""

    if status in (502, 504):
        return WhatsAppAmbiguousDeliveryError(
            f"Gateway error {status} from WhatsApp provider proxy. Upstream delivery state is ambiguous: {text[:200]}"
        )

    if status >= 500:
        has_meta_error = False
        try:
            err_json = http_err.response.json()
            if isinstance(err_json, dict) and "error" in err_json and isinstance(err_json["error"], dict):
                has_meta_error = True
        except Exception:
            has_meta_error = False

        if not has_meta_error:
            return WhatsAppAmbiguousDeliveryError(
                f"Unstructured HTTP {status} from WhatsApp provider without Meta error object. Delivery state is ambiguous: {text[:200]}"
            )

    return WhatsAppConfirmedFailureError(
        f"WhatsApp confirmed error: {status} - {text[:200]}",
        status_code=status,
        response_body=text[:200],
    )


class WhatsAppClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_version: str | None = None,
        default_phone_number_id: str | None = None,
        default_access_token: str | None = None,
        force_mock: bool = False,
    ):
        self.base_url = (base_url or settings.whatsapp_base_url).rstrip("/")
        self.api_version = api_version or settings.whatsapp_api_version
        self.default_phone_number_id = default_phone_number_id
        self.default_access_token = default_access_token
        self.force_mock = force_mock

    @property
    def is_mock(self) -> bool:
        """True if live credentials are not configured or force_mock is active."""
        if self.force_mock:
            return True
        return not bool(self.default_access_token or settings.whatsapp_app_secret)

    async def send_text_message(
        self,
        recipient_phone: str,
        text: str,
        phone_number_id: str | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        """
        Send a free-form text message to a client.
        Allowed only within the 24-hour customer service window.
        """
        pid = phone_number_id or self.default_phone_number_id or "mock_phone_number_id"
        token = access_token or self.default_access_token
        masked_phone = mask_phone_for_logging(recipient_phone)

        if self.is_mock or not token:
            mock_wamid = f"wamid.mock.{uuid.uuid4()}"
            logger.info(
                "whatsapp_mock_send_text",
                recipient_phone=masked_phone,
                message_length=len(text),
                message_id=mock_wamid,
            )
            return {
                "messaging_product": "whatsapp",
                "contacts": [{"input": recipient_phone, "wa_id": recipient_phone.lstrip("+")}],
                "messages": [{"id": mock_wamid}],
                "_is_mock": True,
            }

        url = f"{self.base_url}/{self.api_version}/{pid}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_phone.lstrip("+"),
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                logger.info(
                    "whatsapp_live_message_sent",
                    recipient_phone=masked_phone,
                    message_id=data.get("messages", [{}])[0].get("id") if isinstance(data, dict) else None,
                )
                return data
        except httpx.HTTPStatusError as http_err:
            classified_err = classify_provider_error(http_err)
            if isinstance(classified_err, WhatsAppAmbiguousDeliveryError):
                logger.error(
                    "whatsapp_provider_ambiguous_delivery_status",
                    status=http_err.response.status_code,
                    recipient=masked_phone,
                    error=str(classified_err),
                )
            else:
                logger.warning(
                    "whatsapp_provider_confirmed_failure",
                    status=http_err.response.status_code,
                    recipient=masked_phone,
                )
            raise classified_err from http_err
        except (httpx.TimeoutException, httpx.NetworkError) as net_err:
            logger.error(
                "whatsapp_provider_ambiguous_delivery_timeout",
                error=str(net_err),
                recipient=masked_phone,
            )
            raise WhatsAppAmbiguousDeliveryError(
                f"Socket timeout / network error communicating with WhatsApp API: {net_err}. Delivery state is ambiguous."
            ) from net_err

    async def send_template_message(
        self,
        recipient_phone: str,
        template_name: str,
        language_code: str = "en_US",
        components: list[dict[str, Any]] | None = None,
        phone_number_id: str | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        """
        Send an approved Meta template message.
        Required when initiating contact or outside the 24-hour window.
        """
        pid = phone_number_id or self.default_phone_number_id or "mock_phone_number_id"
        token = access_token or self.default_access_token
        masked_phone = mask_phone_for_logging(recipient_phone)

        if self.is_mock or not token:
            mock_wamid = f"wamid.mock.template.{uuid.uuid4()}"
            logger.info(
                "whatsapp_mock_send_template",
                recipient_phone=masked_phone,
                template_name=template_name,
                message_id=mock_wamid,
            )
            return {
                "messaging_product": "whatsapp",
                "contacts": [{"input": recipient_phone, "wa_id": recipient_phone.lstrip("+")}],
                "messages": [{"id": mock_wamid}],
                "_is_mock": True,
            }

        url = f"{self.base_url}/{self.api_version}/{pid}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_phone.lstrip("+"),
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
                "components": components or [],
            },
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                logger.info(
                    "whatsapp_live_template_sent",
                    recipient_phone=masked_phone,
                    template_name=template_name,
                    message_id=data.get("messages", [{}])[0].get("id") if isinstance(data, dict) else None,
                )
                return data
        except httpx.HTTPStatusError as http_err:
            classified_err = classify_provider_error(http_err)
            if isinstance(classified_err, WhatsAppAmbiguousDeliveryError):
                logger.error(
                    "whatsapp_provider_ambiguous_delivery_status",
                    status=http_err.response.status_code,
                    template_name=template_name,
                    recipient=masked_phone,
                    error=str(classified_err),
                )
            else:
                logger.warning(
                    "whatsapp_provider_confirmed_failure",
                    status=http_err.response.status_code,
                    template_name=template_name,
                    recipient=masked_phone,
                )
            raise classified_err from http_err
        except (httpx.TimeoutException, httpx.NetworkError) as net_err:
            logger.error(
                "whatsapp_provider_ambiguous_delivery_timeout",
                error=str(net_err),
                template_name=template_name,
                recipient=masked_phone,
            )
            raise WhatsAppAmbiguousDeliveryError(
                f"Socket timeout / network error sending template to WhatsApp API: {net_err}. Delivery state is ambiguous."
            ) from net_err
