"""In-memory log buffer with WebSocket streaming for the Console Logs page."""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from typing import Any

from fastapi import WebSocket


class LogRecord:
    """Simplified log record for serialization."""

    __slots__ = ("timestamp", "level", "name", "message")

    def __init__(self, timestamp: str, level: str, name: str, message: str) -> None:
        self.timestamp = timestamp
        self.level = level
        self.name = name
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "name": self.name,
            "message": self.message,
        }


class LogManager(logging.Handler):
    """Logging handler that buffers recent records and streams to WebSocket clients."""

    def __init__(self, buffer_size: int = 2000) -> None:
        super().__init__()
        self.buffer: deque[LogRecord] = deque(maxlen=buffer_size)
        self.connections: list[WebSocket] = []
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = LogRecord(
                timestamp=datetime.fromtimestamp(record.created).strftime("%H:%M:%S.%f")[:-3],
                level=record.levelname,
                name=record.name,
                message=self.format(record),
            )
            self.buffer.append(entry)
            # Schedule broadcast to connected clients (fire-and-forget)
            self._notify(entry)
        except Exception:
            self.handleError(record)

    def _notify(self, entry: LogRecord) -> None:
        """Best-effort push to all connected WebSocket clients."""
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._broadcast(entry))

    async def _broadcast(self, entry: LogRecord) -> None:
        dead: list[WebSocket] = []
        msg = {"type": "log", **entry.to_dict()}
        for ws in self.connections:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.connections:
                self.connections.remove(ws)

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.connections.append(ws)
        # Send buffered history
        history = [{"type": "log", **r.to_dict()} for r in self.buffer]
        try:
            await ws.send_json({"type": "history", "logs": history})
        except Exception:
            if ws in self.connections:
                self.connections.remove(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.connections:
            self.connections.remove(ws)

    def get_history(self) -> list[dict[str, str]]:
        return [r.to_dict() for r in self.buffer]
