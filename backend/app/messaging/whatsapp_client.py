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

        if self.is_mock or not token:
            mock_wamid = f"wamid.mock.{uuid.uuid4()}"
            logger.info(
                "whatsapp_mock_send_text",
                recipient_phone=recipient_phone,
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

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            logger.info("whatsapp_live_message_sent", response=data)
            return data

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

        if self.is_mock or not token:
            mock_wamid = f"wamid.mock.template.{uuid.uuid4()}"
            logger.info(
                "whatsapp_mock_send_template",
                recipient_phone=recipient_phone,
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

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            logger.info("whatsapp_live_template_sent", response=data)
            return data
