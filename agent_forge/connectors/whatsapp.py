"""WhatsAppConnector — WhatsApp via Baileys Node.js sidecar."""

from __future__ import annotations

import logging
from typing import Any

from .base import BaseConnector, ConnectorType, OutboundMessage

logger = logging.getLogger(__name__)


class WhatsAppConnector(BaseConnector):
    """WhatsApp connector using a Baileys (Node.js) sidecar process with local HTTP bridge."""

    connector_type = ConnectorType.WHATSAPP

    def __init__(self, connector_id: str, config: dict[str, Any]) -> None:
        super().__init__(connector_id, config)
        self.phone_number: str = config.get("credentials", {}).get("phone_number", "")
        self.sidecar_port: int = int(
            config.get("settings", {}).get("sidecar_port", 3100)
        )

    async def start(self) -> None:
        logger.info("WhatsAppConnector '%s' not yet implemented", self.connector_id)
        self._running = True

    async def stop(self) -> None:
        self._running = False
        logger.info("WhatsAppConnector '%s' stopped", self.connector_id)

    async def send_message(self, message: OutboundMessage) -> bool:
        logger.warning("WhatsAppConnector.send_message not implemented")
        return False

    async def validate_channel(self, channel_id: str) -> bool:
        return False

    async def get_channel_info(self, channel_id: str) -> dict[str, Any]:
        return {}

    async def list_channels(self) -> list[dict[str, str]]:
        return []

    async def health_check(self) -> dict[str, Any]:
        return {"connected": False, "details": "Not yet implemented"}
