"""Abstract base connector and platform-agnostic message types."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class ConnectorType(str, Enum):
    TELEGRAM = "telegram"
    DISCORD = "discord"
    SLACK = "slack"
    WHATSAPP = "whatsapp"
    SIGNAL = "signal"


@dataclass
class ActionButton:
    """Platform-agnostic interactive button definition."""

    label: str      # Display text, e.g. "Approve", "Reject"
    action: str     # Control action, e.g. "approve", "reject"
    agent_id: str   # Target agent


@dataclass
class InboundMessage:
    """Platform-agnostic incoming message."""

    connector_id: str
    channel_id: str
    sender_id: str
    sender_name: str = ""
    text: str = ""
    media_paths: list[str] = field(default_factory=list)
    project_name: str = ""
    agent_id: str = ""
    is_command: bool = False
    command_name: str = ""
    command_args: list[str] = field(default_factory=list)
    raw: Any = None


@dataclass
class OutboundMessage:
    """Platform-agnostic outgoing message."""

    channel_id: str
    text: str = ""
    media_paths: list[str] = field(default_factory=list)
    parse_mode: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class BaseConnector(ABC):
    """Abstract base class for IM connectors."""

    connector_type: ConnectorType
    CHUNK_LIMIT: int = 4096

    def __init__(self, connector_id: str, config: dict[str, Any]) -> None:
        self.connector_id = connector_id
        self.config = config
        self._message_callback: Callable[[InboundMessage], Awaitable[None]] | None = None
        self._running = False

    def set_message_callback(
        self, callback: Callable[[InboundMessage], Awaitable[None]]
    ) -> None:
        """Set the callback the ConnectorManager uses to receive inbound messages."""
        self._message_callback = callback

    @abstractmethod
    async def start(self) -> None:
        """Start the connector (connect to platform, begin polling/listening)."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully stop the connector."""

    @abstractmethod
    async def send_message(self, message: OutboundMessage) -> bool:
        """Send a message to a channel. Returns True on success."""

    @abstractmethod
    async def validate_channel(self, channel_id: str) -> bool:
        """Check if a channel ID is valid and reachable."""

    @abstractmethod
    async def get_channel_info(self, channel_id: str) -> dict[str, Any]:
        """Get channel details (name, type, etc). Returns empty dict on failure."""

    @abstractmethod
    async def list_channels(self) -> list[dict[str, str]]:
        """List available channels (for UI picker). Returns list of {id, name}."""

    @abstractmethod
    async def health_check(self) -> dict[str, Any]:
        """Return connector health status."""

    def _chunk_text(self, text: str) -> list[str]:
        """Split text into chunks that fit within CHUNK_LIMIT.

        Smart splitting prefers: paragraph breaks > line breaks > sentence ends > hard split.
        Adds chunk indicators [1/N] when multi-part.
        """
        limit = self.CHUNK_LIMIT
        if len(text) <= limit:
            return [text]

        indicator_reserve = 8
        effective_limit = limit - indicator_reserve

        chunks: list[str] = []
        remaining = text

        while remaining:
            if len(remaining) <= effective_limit:
                chunks.append(remaining)
                break

            split_pos = self._find_split_point(remaining, effective_limit)
            chunks.append(remaining[:split_pos].rstrip())
            remaining = remaining[split_pos:].lstrip()

        if len(chunks) > 1:
            total = len(chunks)
            chunks = [f"{chunk} [{i+1}/{total}]" for i, chunk in enumerate(chunks)]

        return chunks

    @staticmethod
    def _find_split_point(text: str, limit: int) -> int:
        """Find the best split point within limit chars."""
        pos = text.rfind("\n\n", 0, limit)
        if pos > limit // 4:
            return pos + 2

        pos = text.rfind("\n", 0, limit)
        if pos > limit // 4:
            return pos + 1

        pos = text.rfind(". ", 0, limit)
        if pos > limit // 4:
            return pos + 2

        return limit

    async def send_test_message(self, channel_id: str) -> dict[str, Any]:
        """Send a test message to a channel. Returns result dict."""
        msg = OutboundMessage(
            channel_id=channel_id,
            text="Agent Forge test message — your connector is working!",
        )
        success = await self.send_message(msg)
        if success:
            return {"sent": True}
        return {"sent": False, "detail": "send_message returned False"}
