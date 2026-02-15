"""IM connector abstraction layer."""

from .base import BaseConnector, ConnectorType, InboundMessage, OutboundMessage

try:
    from .manager import ConnectorManager
except ImportError:
    ConnectorManager = None  # type: ignore[assignment,misc]

__all__ = [
    "BaseConnector",
    "ConnectorManager",
    "ConnectorType",
    "InboundMessage",
    "OutboundMessage",
]
