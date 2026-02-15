"""SlackConnector — Slack bot connector (requires slack-bolt>=1.18)."""

from __future__ import annotations

import logging
from typing import Any

from .base import BaseConnector, ConnectorType, OutboundMessage

logger = logging.getLogger(__name__)


class SlackConnector(BaseConnector):
    """Slack bot connector using Socket Mode. Requires ``slack-bolt>=1.18``."""

    connector_type = ConnectorType.SLACK

    def __init__(self, connector_id: str, config: dict[str, Any]) -> None:
        super().__init__(connector_id, config)
        self.bot_token: str = config.get("credentials", {}).get("bot_token", "")
        self.app_token: str = config.get("credentials", {}).get("app_token", "")

    async def start(self) -> None:
        logger.info("SlackConnector '%s' not yet implemented", self.connector_id)
        self._running = True

    async def stop(self) -> None:
        self._running = False
        logger.info("SlackConnector '%s' stopped", self.connector_id)

    async def send_message(self, message: OutboundMessage) -> bool:
        # Append text hint when action buttons are present
        if message.extra.get("action_buttons"):
            message.text += "\n\nReply: /approve | /reject | /interrupt"
        logger.warning("SlackConnector.send_message not implemented")
        return False

    async def validate_channel(self, channel_id: str) -> bool:
        return False

    async def get_channel_info(self, channel_id: str) -> dict[str, Any]:
        return {}

    async def list_channels(self) -> list[dict[str, str]]:
        return []

    async def health_check(self) -> dict[str, Any]:
        return {"connected": False, "details": "Not yet implemented"}
