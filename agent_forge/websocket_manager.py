"""WebSocket connection manager for real-time UI updates."""

from __future__ import annotations

import logging

from fastapi import WebSocket

from .agent_manager import Agent

logger = logging.getLogger(__name__)


class WebSocketManager:
    """Manages WebSocket connections and broadcasts updates to all clients."""

    def __init__(self) -> None:
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        """Accept and register a new WebSocket connection."""
        await ws.accept()
        self.connections.append(ws)
        logger.debug("WebSocket connected (%d total)", len(self.connections))

    def disconnect(self, ws: WebSocket) -> None:
        """Remove a WebSocket connection."""
        if ws in self.connections:
            self.connections.remove(ws)
            logger.debug("WebSocket disconnected (%d remaining)", len(self.connections))

    async def broadcast(self, message: dict) -> None:
        """Send a JSON message to all connected clients. Remove dead connections."""
        dead: list[WebSocket] = []
        for ws in self.connections:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.connections.remove(ws)
        if dead:
            logger.debug("Removed %d dead WebSocket connections", len(dead))

    async def broadcast_agent_update(self, agent: Agent) -> None:
        """Broadcast an agent status update to all clients."""
        await self.broadcast({
            "type": "agent_update",
            "agent_id": agent.id,
            "project": agent.project_name,
            "status": agent.status.value,
            "last_output": agent.last_output[-2000:] if agent.last_output else "",
            "last_activity": agent.last_activity.isoformat(),
            "task": agent.task_description,
            "sub_agent_count": agent.sub_agent_count,
            "needs_attention": agent.needs_attention,
            "parked": agent.parked,
        })

    async def broadcast_terminal_output(self, agent_id: str, output: str) -> None:
        """Broadcast raw terminal output for a specific agent."""
        await self.broadcast({
            "type": "terminal_output",
            "agent_id": agent_id,
            "output": output,
        })
