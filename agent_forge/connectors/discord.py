"""DiscordConnector — Discord bot connector (requires discord.py>=2.3)."""

from __future__ import annotations

import asyncio
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from .base import ActionButton, BaseConnector, ConnectorType, InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)


class DiscordConnector(BaseConnector):
    """Discord bot connector using discord.py."""

    connector_type = ConnectorType.DISCORD

    def __init__(self, connector_id: str, config: dict[str, Any]) -> None:
        super().__init__(connector_id, config)
        self.bot_token: str = config.get("credentials", {}).get("bot_token", "")

        # Parse guild_ids and allowed_users as integers
        guild_ids_raw = config.get("settings", {}).get("guild_ids", [])
        self.guild_ids: list[int] = [
            int(gid) for gid in guild_ids_raw if str(gid).strip()
        ]

        allowed_users_raw = config.get("settings", {}).get("allowed_users", [])
        self.allowed_users: list[int] = [
            int(uid) for uid in allowed_users_raw if str(uid).strip()
        ]

        self._client: Any = None
        self._task: asyncio.Task | None = None
        self._ready_event: asyncio.Event = asyncio.Event()

        # Load persisted recent channels from settings, fall back to empty
        self._recent_channels: dict[str, dict[str, str]] = config.get("settings", {}).get(
            "known_channels", {}
        )

    async def start(self) -> None:
        """Start the Discord client as a background task on FastAPI's event loop."""
        import discord

        # Configure intents
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True

        self._client = discord.Client(intents=intents)
        self._ready_event.clear()

        # Register event handlers
        @self._client.event
        async def on_ready() -> None:
            await self._on_ready()

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            await self._on_message(message)

        @self._client.event
        async def on_interaction(interaction: discord.Interaction) -> None:
            await self._on_interaction(interaction)

        # Launch client in background
        self._task = asyncio.create_task(self._client.start(self.bot_token))

        # Wait for on_ready (with timeout)
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=30.0)
            self._running = True
            logger.info("DiscordConnector '%s' started", self.connector_id)
        except asyncio.TimeoutError:
            logger.error("DiscordConnector '%s' timed out waiting for on_ready", self.connector_id)
            await self.stop()
            raise RuntimeError("Discord client failed to connect within 30s")

    async def stop(self) -> None:
        """Stop the Discord client and cancel background task."""
        if self._client:
            try:
                await self._client.close()
            except Exception:
                logger.exception("Error closing Discord client '%s'", self.connector_id)

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        self._running = False
        self._client = None
        self._task = None
        logger.info("DiscordConnector '%s' stopped", self.connector_id)

    async def _on_ready(self) -> None:
        """Handle on_ready event — seed recent channels and signal ready."""
        if not self._client or not self._client.user:
            return

        logger.info(
            "Discord bot logged in as %s (ID: %s)",
            self._client.user.name,
            self._client.user.id,
        )

        # Seed recent channels from accessible guild text channels
        for guild in self._client.guilds:
            # Skip guilds not in filter list (if configured)
            if self.guild_ids and guild.id not in self.guild_ids:
                continue

            for channel in guild.text_channels:
                # Check if bot has send_messages permission
                perms = channel.permissions_for(guild.me)
                if perms.send_messages:
                    self._track_channel(channel)

        if self._recent_channels:
            logger.info("Seeded %d channel(s) from guilds", len(self._recent_channels))

        self._ready_event.set()

    async def _on_message(self, message: Any) -> None:
        """Handle incoming messages."""
        # Ignore own messages
        if message.author == self._client.user:
            return

        # Filter by guild_ids if configured
        if message.guild and self.guild_ids and message.guild.id not in self.guild_ids:
            return

        # Check authorization
        if not self._check_authorized(message.author.id):
            logger.debug(
                "Ignored message from unauthorized user %s (ID: %s)",
                message.author.name,
                message.author.id,
            )
            return

        # Track channel
        if hasattr(message.channel, "name"):
            self._track_channel(message.channel)

        # Extract text content
        text = message.content or ""

        # Detect commands
        is_command = False
        command_name = ""
        command_args: list[str] = []

        if text.startswith("/"):
            is_command = True
            parts = text.split()
            command_name = parts[0].lstrip("/")
            command_args = parts[1:]

        # Download attachments
        media_paths: list[str] = []
        if message.attachments:
            media_paths = await self._download_attachments(message.attachments)

        # Parse routing (@project[:agent_id])
        project_name, agent_id = self._parse_routing(text)
        if project_name:
            # Strip the @project[:agent] prefix from the text
            match = re.match(r"^@[\w-]+(?::[\w-]+)?[:\s]\s*(.*)", text, re.DOTALL)
            text = match.group(1).strip() if match else text

        # Build inbound message
        msg = InboundMessage(
            connector_id=self.connector_id,
            channel_id=str(message.channel.id),
            sender_id=str(message.author.id),
            sender_name=message.author.name or "",
            text=text,
            media_paths=media_paths,
            project_name=project_name,
            agent_id=agent_id,
            is_command=is_command,
            command_name=command_name,
            command_args=command_args,
            raw=message,
        )

        if self._message_callback:
            await self._message_callback(msg)

    async def _on_interaction(self, interaction: Any) -> None:
        """Handle button interactions via custom_id parsing."""
        if interaction.type.name != "component":
            return

        # Check authorization
        if not self._check_authorized(interaction.user.id):
            await interaction.response.send_message("Not authorized.", ephemeral=True)
            return

        # Parse custom_id: "ctrl:{agent_id}:{action}"
        custom_id = interaction.data.get("custom_id", "")
        parts = custom_id.split(":", 2)

        if len(parts) != 3 or parts[0] != "ctrl":
            await interaction.response.send_message("Invalid action.", ephemeral=True)
            return

        _, agent_id, action = parts

        # Build inbound message
        msg = InboundMessage(
            connector_id=self.connector_id,
            channel_id=str(interaction.channel_id),
            sender_id=str(interaction.user.id),
            sender_name=interaction.user.name or "",
            is_command=True,
            command_name=action,
            command_args=[agent_id],
            raw=interaction,
        )

        if self._message_callback:
            await self._message_callback(msg)

        # Send ephemeral confirmation
        await interaction.response.send_message(f"{action} sent", ephemeral=True)

    async def send_message(self, message: OutboundMessage) -> bool:
        """Send a message to a Discord channel."""
        if not self._client:
            return False

        try:
            # Get channel
            channel_id_int = int(message.channel_id)
            channel = self._client.get_channel(channel_id_int)

            if not channel:
                # Try fetching if not in cache
                channel = await self._client.fetch_channel(channel_id_int)

            if not channel:
                logger.error("Channel %s not found", message.channel_id)
                return False

            # Build view from action buttons
            view = None
            buttons: list[ActionButton] = message.extra.get("action_buttons", [])
            if buttons:
                import discord

                view = discord.ui.View(timeout=None)
                for btn in buttons:
                    button = discord.ui.Button(
                        label=btn.label,
                        style=self._button_style(btn.action),
                        custom_id=f"ctrl:{btn.agent_id}:{btn.action}",
                    )
                    view.add_item(button)

            # Split text at 2000-char limit
            text_chunks = self._split_message(message.text)

            # Send text chunks
            for i, chunk in enumerate(text_chunks):
                # Attach view to last chunk only
                chunk_view = view if i == len(text_chunks) - 1 else None
                await channel.send(content=chunk, view=chunk_view)

            # Send media files
            import discord

            for path in message.media_paths:
                await channel.send(file=discord.File(path))

            return True

        except Exception:
            logger.exception("Failed to send Discord message to %s", message.channel_id)
            return False

    async def validate_channel(self, channel_id: str) -> bool:
        """Check if a channel ID is valid and reachable."""
        if not self._client:
            return False

        try:
            channel_id_int = int(channel_id)
            channel = self._client.get_channel(channel_id_int)

            if not channel:
                channel = await self._client.fetch_channel(channel_id_int)

            return channel is not None
        except Exception:
            return False

    async def get_channel_info(self, channel_id: str) -> dict[str, Any]:
        """Get channel details."""
        if not self._client:
            return {}

        try:
            channel_id_int = int(channel_id)
            channel = self._client.get_channel(channel_id_int)

            if not channel:
                channel = await self._client.fetch_channel(channel_id_int)

            if not channel:
                return {}

            info: dict[str, Any] = {
                "id": str(channel.id),
                "name": getattr(channel, "name", ""),
                "type": str(channel.type),
            }

            if hasattr(channel, "guild"):
                info["guild"] = channel.guild.name if channel.guild else ""

            return info

        except Exception:
            return {}

    async def list_channels(self) -> list[dict[str, str]]:
        """List accessible text channels from guilds, with recent channels fallback."""
        if not self._client or not self._client.guilds:
            # Fall back to persisted recent channels
            return [
                {"id": ch_id, "name": info.get("name", ch_id), "type": info.get("type", "")}
                for ch_id, info in self._recent_channels.items()
            ]

        channels: list[dict[str, str]] = []

        for guild in self._client.guilds:
            # Skip guilds not in filter list (if configured)
            if self.guild_ids and guild.id not in self.guild_ids:
                continue

            for channel in guild.text_channels:
                # Check send_messages permission
                perms = channel.permissions_for(guild.me)
                if not perms.send_messages:
                    continue

                channels.append({
                    "id": str(channel.id),
                    "name": f"{guild.name} / {channel.name}",
                    "type": str(channel.type),
                })

        return channels

    async def health_check(self) -> dict[str, Any]:
        """Return connector health status."""
        if not self._client or not self._client.user:
            return {"connected": False, "details": "Client not started"}

        try:
            return {
                "connected": True,
                "bot_username": self._client.user.name,
                "bot_id": self._client.user.id,
                "guild_count": len(self._client.guilds),
                "latency_ms": round(self._client.latency * 1000, 2),
            }
        except Exception as exc:
            return {"connected": False, "details": str(exc)}

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    def _check_authorized(self, user_id: int) -> bool:
        """Check if a user is authorized (empty allowed_users = allow all)."""
        if not self.allowed_users:
            return True
        return user_id in self.allowed_users

    @staticmethod
    def _parse_routing(text: str) -> tuple[str, str]:
        """Extract @project[:agent_id] from text. Returns (project, agent_id)."""
        match = re.match(r"^@([\w-]+)(?::([\w-]+))?[:\s]", text)
        if not match:
            return "", ""
        return match.group(1), match.group(2) or ""

    def _track_channel(self, channel: Any) -> None:
        """Record a channel in the recent channels map."""
        channel_id = str(channel.id)
        name = getattr(channel, "name", channel_id)

        # Add guild prefix if available
        if hasattr(channel, "guild") and channel.guild:
            name = f"{channel.guild.name} / {name}"

        is_new = channel_id not in self._recent_channels
        self._recent_channels[channel_id] = {
            "name": name,
            "type": str(channel.type),
        }

        if is_new:
            logger.info("Tracked new channel: id=%s name='%s' type=%s", channel_id, name, channel.type)

    def _split_message(self, text: str) -> list[str]:
        """Split text at Discord's 2000-char limit, breaking at newlines when possible."""
        if len(text) <= 2000:
            return [text]

        chunks: list[str] = []
        remaining = text

        while remaining:
            if len(remaining) <= 2000:
                chunks.append(remaining)
                break

            # Find last newline before 2000 chars
            split_pos = remaining.rfind("\n", 0, 2000)
            if split_pos == -1:
                # No newline found, hard split at 2000
                split_pos = 2000
            else:
                # Include the newline in the chunk
                split_pos += 1

            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos:]

        return chunks

    @staticmethod
    def _button_style(action: str) -> Any:
        """Map action name to Discord ButtonStyle."""
        import discord

        action_lower = action.lower()

        if action_lower in ("approve", "approve_all"):
            return discord.ButtonStyle.success
        elif action_lower in ("reject", "kill", "interrupt"):
            return discord.ButtonStyle.danger
        else:
            return discord.ButtonStyle.primary

    async def _download_attachments(self, attachments: list[Any]) -> list[str]:
        """Download Discord attachments to temp files."""
        from .base import ensure_extension

        media_paths: list[str] = []

        for attachment in attachments:
            try:
                tmp_dir = tempfile.mkdtemp(prefix="forge_media_")
                file_name = attachment.filename or "attachment"
                content_type = getattr(attachment, "content_type", "") or ""
                file_name = ensure_extension(file_name, content_type)
                tmp_path = Path(tmp_dir) / file_name

                await attachment.save(tmp_path)
                media_paths.append(str(tmp_path))
            except Exception:
                logger.exception("Failed to download Discord attachment '%s'", attachment.filename)

        return media_paths

    def get_known_channels(self) -> dict[str, dict[str, str]]:
        """Return recent channels dict for persistence (plain strings only)."""
        return {
            ch_id: {k: str(v) for k, v in info.items()}
            for ch_id, info in self._recent_channels.items()
        }
