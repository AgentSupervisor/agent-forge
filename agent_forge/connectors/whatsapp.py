"""WhatsAppConnector — WhatsApp via Baileys Node.js sidecar."""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import re
import shutil
import signal
import tempfile
from pathlib import Path
from typing import Any

import httpx

from .base import ActionButton, BaseConnector, ConnectorType, InboundMessage, OutboundMessage, extract_agent_from_text

logger = logging.getLogger(__name__)

# Path to the Node.js sidecar code
_SIDECAR_DIR = Path(__file__).parent / "whatsapp_sidecar"


class WhatsAppConnector(BaseConnector):
    """WhatsApp connector using a Baileys (Node.js) sidecar process with local HTTP bridge."""

    connector_type = ConnectorType.WHATSAPP

    def __init__(self, connector_id: str, config: dict[str, Any]) -> None:
        super().__init__(connector_id, config)
        self.phone_number: str = config.get("credentials", {}).get("phone_number", "")
        self.sidecar_port: int = int(
            config.get("settings", {}).get("sidecar_port", 3100)
        )
        self.allowed_users: list[str] = config.get("settings", {}).get(
            "allowed_users", []
        )
        self._sidecar_process: asyncio.subprocess.Process | None = None
        self._session_dir = (
            Path.home() / ".agent-forge" / "whatsapp_sessions" / self.phone_number.lstrip("+")
        )
        # Load persisted recent chats from settings, fall back to empty
        self._recent_chats: dict[str, dict[str, str]] = config.get("settings", {}).get(
            "known_chats", {}
        )
        self._poll_task: asyncio.Task[None] | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._base_url = f"http://127.0.0.1:{self.sidecar_port}"

    async def start(self) -> None:
        """Start the WhatsApp connector by launching the sidecar and beginning message polling."""
        # Create session directory
        self._session_dir.mkdir(parents=True, exist_ok=True)

        # Start the sidecar process
        await self._start_sidecar()

        # Create HTTP client
        self._http_client = httpx.AsyncClient(base_url=self._base_url, timeout=10.0)

        # Wait for sidecar to be ready
        ready = False
        for _ in range(60):  # 30 seconds total (60 * 0.5s)
            try:
                resp = await self._http_client.get("/health")
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("connected"):
                        ready = True
                        logger.info("WhatsApp sidecar ready and connected")
                        break
                    elif data.get("qr"):
                        logger.info("WhatsApp QR code available at http://127.0.0.1:%d/qr", self.sidecar_port)
            except Exception:
                pass
            await asyncio.sleep(0.5)

        if not ready:
            logger.warning("WhatsApp sidecar did not become ready within 30s, continuing anyway")

        # Start polling for messages
        self._poll_task = asyncio.create_task(self._poll_messages())

        self._running = True
        logger.info("WhatsAppConnector '%s' started (port %d)", self.connector_id, self.sidecar_port)

    async def stop(self) -> None:
        """Gracefully stop the connector."""
        # Cancel polling task
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        # Stop sidecar process
        await self._stop_sidecar()

        # Close HTTP client
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        self._running = False
        logger.info("WhatsAppConnector '%s' stopped", self.connector_id)

    async def send_message(self, message: OutboundMessage) -> bool:
        """Send a message to a WhatsApp chat. Returns True on success."""
        if not self._http_client:
            return False

        try:
            jid = self._channel_id_to_jid(message.channel_id)

            # Build payload with text and optional buttons
            payload: dict[str, Any] = {
                "jid": jid,
                "text": message.text,
            }

            # Add action buttons if present (max 3)
            buttons: list[ActionButton] = message.extra.get("action_buttons", [])
            if buttons:
                payload["buttons"] = [
                    {
                        "id": f"ctrl:{btn.agent_id}:{btn.action}",
                        "text": btn.label,
                    }
                    for btn in buttons[:3]
                ]

            # Send text message (with optional buttons)
            resp = await self._http_client.post("/send", json=payload)
            resp.raise_for_status()

            # Send media files separately
            for media_path in message.media_paths:
                path = Path(media_path)
                if not path.exists():
                    logger.warning("Media file not found: %s", media_path)
                    continue

                # Guess MIME type
                mime_type, _ = mimetypes.guess_type(str(path))
                if not mime_type:
                    mime_type = "application/octet-stream"

                with open(path, "rb") as f:
                    files = {"file": (path.name, f, mime_type)}
                    data = {"jid": jid}
                    resp = await self._http_client.post("/send_media", files=files, data=data)
                    resp.raise_for_status()

            return True
        except Exception:
            logger.exception("Failed to send WhatsApp message to %s", message.channel_id)
            return False

    async def validate_channel(self, channel_id: str) -> bool:
        """Check if a channel ID is valid and reachable."""
        if not self._http_client:
            return False
        try:
            jid = self._channel_id_to_jid(channel_id)
            resp = await self._http_client.get(f"/chat/{jid}")
            return resp.status_code == 200
        except Exception:
            return False

    async def get_channel_info(self, channel_id: str) -> dict[str, Any]:
        """Get channel details (name, type, etc). Returns empty dict on failure."""
        if not self._http_client:
            return {}
        try:
            jid = self._channel_id_to_jid(channel_id)
            resp = await self._http_client.get(f"/chat/{jid}")
            resp.raise_for_status()
            data = resp.json()
            return {
                "id": channel_id,
                "name": data.get("name", channel_id),
                "type": "group" if data.get("isGroup") else "private",
            }
        except Exception:
            return {}

    async def list_channels(self) -> list[dict[str, str]]:
        """List available channels (for UI picker). Returns list of {id, name, type}."""
        return [
            {"id": chat_id, "name": info.get("name", chat_id), "type": info.get("type", "")}
            for chat_id, info in self._recent_chats.items()
        ]

    async def health_check(self) -> dict[str, Any]:
        """Return connector health status."""
        if not self._http_client:
            return {"connected": False, "details": "HTTP client not initialized"}
        try:
            resp = await self._http_client.get("/health", timeout=2.0)
            return resp.json()
        except Exception as exc:
            return {"connected": False, "details": str(exc)}

    def get_known_chats(self) -> dict[str, dict[str, str]]:
        """Return recent chats dict for persistence (plain strings only)."""
        return {
            cid: {k: str(v) for k, v in info.items()}
            for cid, info in self._recent_chats.items()
        }

    # ------------------------------------------------------------------
    # Internal sidecar management
    # ------------------------------------------------------------------

    async def _start_sidecar(self) -> None:
        """Launch the Node.js sidecar process."""
        self._sidecar_process = await asyncio.create_subprocess_exec(
            "node",
            "index.js",
            "--port",
            str(self.sidecar_port),
            "--session-dir",
            str(self._session_dir),
            cwd=str(_SIDECAR_DIR),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Log sidecar output in background
        if self._sidecar_process.stdout:
            asyncio.create_task(self._log_stream(self._sidecar_process.stdout, "stdout"))
        if self._sidecar_process.stderr:
            asyncio.create_task(self._log_stream(self._sidecar_process.stderr, "stderr"))

        logger.info("Started WhatsApp sidecar process (PID: %d)", self._sidecar_process.pid)

    async def _stop_sidecar(self) -> None:
        """Gracefully stop the sidecar process."""
        if not self._sidecar_process:
            return

        # Try graceful shutdown via API
        try:
            if self._http_client:
                await self._http_client.post("/shutdown")
        except Exception:
            pass

        # Wait up to 5 seconds for process to exit
        try:
            await asyncio.wait_for(self._sidecar_process.wait(), timeout=5.0)
            logger.info("WhatsApp sidecar exited gracefully")
            self._sidecar_process = None
            return
        except asyncio.TimeoutError:
            pass

        # Send SIGTERM and wait 2 seconds
        try:
            self._sidecar_process.send_signal(signal.SIGTERM)
            await asyncio.wait_for(self._sidecar_process.wait(), timeout=2.0)
            logger.info("WhatsApp sidecar terminated via SIGTERM")
            self._sidecar_process = None
            return
        except asyncio.TimeoutError:
            pass

        # Last resort: SIGKILL
        try:
            self._sidecar_process.kill()
            await self._sidecar_process.wait()
            logger.warning("WhatsApp sidecar killed via SIGKILL")
        except Exception:
            logger.exception("Failed to kill sidecar process")

        self._sidecar_process = None

    async def _log_stream(self, stream: asyncio.StreamReader, level: str) -> None:
        """Log output from sidecar stdout/stderr."""
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if level == "stderr":
                logger.warning("sidecar: %s", text)
            else:
                logger.debug("sidecar: %s", text)

    # ------------------------------------------------------------------
    # Message polling and processing
    # ------------------------------------------------------------------

    async def _poll_messages(self) -> None:
        """Continuously poll for new messages from the sidecar."""
        while self._running:
            try:
                resp = await self._http_client.get("/messages")
                if resp.status_code == 200:
                    messages = resp.json()
                    for data in messages:
                        await self._process_message(data)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Poll error", exc_info=True)
            await asyncio.sleep(1.0)

    async def _process_message(self, data: dict[str, Any]) -> None:
        """Process a single incoming message from the sidecar."""
        sender_jid = data.get("from", "")
        push_name = data.get("pushName", "")
        chat_jid = data.get("chatJid", sender_jid)
        is_group = data.get("isGroup", False)
        channel_id = self._jid_to_channel_id(chat_jid)

        # Track this chat
        self._track_chat(chat_jid, push_name, "group" if is_group else "private")

        # Check authorization
        if not self._check_authorized(sender_jid):
            return

        text = data.get("text", "")

        # Handle media
        media_paths: list[str] = []
        if data.get("media"):
            from .base import ensure_extension

            media_info = data["media"]
            src = Path(media_info["path"])
            if src.exists():
                tmp_dir = tempfile.mkdtemp(prefix="forge_wa_media_")
                file_name = media_info.get("filename") or src.name
                content_type = media_info.get("mimetype", "")
                file_name = ensure_extension(file_name, content_type)
                dest = Path(tmp_dir) / file_name
                shutil.copy2(str(src), str(dest))
                media_paths.append(str(dest))

        # Handle button response
        if data.get("selectedButtonId"):
            parts = data["selectedButtonId"].split(":", 2)
            if len(parts) == 3 and parts[0] == "ctrl":
                msg = InboundMessage(
                    connector_id=self.connector_id,
                    channel_id=channel_id,
                    sender_id=self._jid_to_channel_id(sender_jid),
                    sender_name=push_name,
                    is_command=True,
                    command_name=parts[2],
                    command_args=[parts[1]],
                    raw=data,
                )
                if self._message_callback:
                    await self._message_callback(msg)
                return

        # Handle commands (text starts with "/")
        if text.startswith("/"):
            parts = text.split()
            command_name = parts[0].lstrip("/")
            command_args = parts[1:]
            msg = InboundMessage(
                connector_id=self.connector_id,
                channel_id=channel_id,
                sender_id=self._jid_to_channel_id(sender_jid),
                sender_name=push_name,
                is_command=True,
                command_name=command_name,
                command_args=command_args,
                raw=data,
            )
            if self._message_callback:
                await self._message_callback(msg)
            return

        # Handle regular text with optional routing
        project_name, agent_id = self._parse_routing(text)
        if project_name:
            match = re.match(r"^@[\w-]+(?::[\w-]+)?[:\s]\s*(.*)", text, re.DOTALL)
            text = match.group(1).strip() if match else text

        # Extract agent_id from quoted (replied-to) bot message
        if not agent_id:
            quoted_text = data.get("quotedMessage", {}).get("text", "")
            if quoted_text:
                agent_id = extract_agent_from_text(quoted_text)

        msg = InboundMessage(
            connector_id=self.connector_id,
            channel_id=channel_id,
            sender_id=self._jid_to_channel_id(sender_jid),
            sender_name=push_name,
            text=text,
            media_paths=media_paths,
            project_name=project_name,
            agent_id=agent_id,
            raw=data,
        )
        if self._message_callback:
            await self._message_callback(msg)

    def _track_chat(self, jid: str, name: str, chat_type: str) -> None:
        """Record a chat in the recent chats map."""
        channel_id = self._jid_to_channel_id(jid)
        is_new = channel_id not in self._recent_chats
        self._recent_chats[channel_id] = {"name": name or channel_id, "type": chat_type}
        if is_new:
            logger.info("Tracked new chat: id=%s name='%s' type=%s", channel_id, name, chat_type)

    def _check_authorized(self, sender_jid: str) -> bool:
        """Check if a sender JID is authorized."""
        if not self.allowed_users:
            return True
        return sender_jid in self.allowed_users

    @staticmethod
    def _parse_routing(text: str) -> tuple[str, str]:
        """Extract @project[:agent_id] from text. Returns (project, agent_id)."""
        match = re.match(r"^@([\w-]+)(?::([\w-]+))?[:\s]", text)
        if not match:
            return "", ""
        return match.group(1), match.group(2) or ""

    # ------------------------------------------------------------------
    # JID conversion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _jid_to_channel_id(jid: str) -> str:
        """Convert WhatsApp JID to channel ID by stripping domain suffixes."""
        return jid.replace("@s.whatsapp.net", "").replace("@g.us", "")

    @staticmethod
    def _channel_id_to_jid(channel_id: str) -> str:
        """Convert channel ID to WhatsApp JID by adding appropriate domain suffix."""
        if "@" in channel_id:
            return channel_id  # Already a JID
        if "-" in channel_id:
            return f"{channel_id}@g.us"  # Group
        return f"{channel_id}@s.whatsapp.net"  # Private chat
