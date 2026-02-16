"""SlackConnector — Slack bot connector (requires slack-bolt>=1.18)."""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from .base import ActionButton, BaseConnector, ConnectorType, InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)


class SlackConnector(BaseConnector):
    """Slack bot connector using Socket Mode. Requires ``slack-bolt>=1.18``."""

    connector_type = ConnectorType.SLACK
    CHUNK_LIMIT = 3000

    def __init__(self, connector_id: str, config: dict[str, Any]) -> None:
        super().__init__(connector_id, config)
        self.bot_token: str = config.get("credentials", {}).get("bot_token", "")
        self.app_token: str = config.get("credentials", {}).get("app_token", "")
        self.allowed_users: list[str] = config.get("settings", {}).get(
            "allowed_users", []
        )
        self._app: Any = None
        self._handler: Any = None
        self._client: Any = None
        self._bot_user_id: str = ""

    async def start(self) -> None:
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_bolt.async_app import AsyncApp

        self._app = AsyncApp(token=self.bot_token)
        self._client = self._app.client

        # Register event handlers
        @self._app.event("message")
        async def handle_message(event: dict, say: Any) -> None:
            await self._handle_message(event)

        @self._app.event("app_mention")
        async def handle_app_mention(event: dict, say: Any) -> None:
            await self._handle_app_mention(event)

        # Register Block Kit action handler for control buttons
        @self._app.action(re.compile(r"^ctrl_"))
        async def handle_block_action(ack: Any, body: dict) -> None:
            await ack()
            await self._handle_block_action(body)

        # Get bot user ID via auth.test
        auth_response = await self._client.auth_test()
        self._bot_user_id = auth_response["user_id"]

        # Start Socket Mode
        self._handler = AsyncSocketModeHandler(self._app, self.app_token)
        await self._handler.start_async()
        self._running = True
        logger.info(
            "SlackConnector '%s' started (bot_user_id=%s)",
            self.connector_id,
            self._bot_user_id,
        )

    async def stop(self) -> None:
        if self._handler:
            try:
                await self._handler.close_async()
            except Exception:
                logger.exception("Error stopping SlackConnector '%s'", self.connector_id)
        self._running = False
        self._app = None
        self._client = None
        self._handler = None
        self._bot_user_id = ""
        logger.info("SlackConnector '%s' stopped", self.connector_id)

    async def send_message(self, message: OutboundMessage) -> bool:
        if not self._client:
            return False
        try:
            # Build Block Kit blocks if action buttons are present
            buttons: list[ActionButton] = message.extra.get("action_buttons", [])
            blocks = None
            if buttons:
                blocks = [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": message.text},
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": btn.label},
                                "action_id": f"ctrl_{btn.agent_id}_{btn.action}",
                                "value": f"{btn.agent_id}:{btn.action}",
                            }
                            for btn in buttons
                        ],
                    },
                ]

            chunks = self._chunk_text(message.text)
            for i, chunk in enumerate(chunks):
                chunk_blocks = blocks if (blocks and i == len(chunks) - 1) else None
                await self._client.chat_postMessage(
                    channel=message.channel_id,
                    text=chunk,
                    blocks=chunk_blocks,
                )

            # Upload media files
            for path in message.media_paths:
                await self._client.files_upload_v2(
                    channel=message.channel_id,
                    file=path,
                )

            return True
        except Exception:
            logger.exception("Failed to send Slack message to %s", message.channel_id)
            return False

    async def validate_channel(self, channel_id: str) -> bool:
        if not self._client:
            return False
        try:
            await self._client.conversations_info(channel=channel_id)
            return True
        except Exception:
            return False

    async def get_channel_info(self, channel_id: str) -> dict[str, Any]:
        if not self._client:
            return {}
        try:
            response = await self._client.conversations_info(channel=channel_id)
            channel = response["channel"]
            if channel.get("is_channel"):
                ch_type = "channel"
            elif channel.get("is_group"):
                ch_type = "group"
            else:
                ch_type = "im"
            return {
                "id": channel["id"],
                "name": channel.get("name", ""),
                "type": ch_type,
            }
        except Exception:
            return {}

    async def list_channels(self) -> list[dict[str, str]]:
        if not self._client:
            return []
        channels: list[dict[str, str]] = []
        try:
            cursor = None
            while True:
                kwargs: dict[str, Any] = {
                    "types": "public_channel,private_channel",
                    "limit": 200,
                }
                if cursor:
                    kwargs["cursor"] = cursor
                response = await self._client.conversations_list(**kwargs)
                for ch in response.get("channels", []):
                    channels.append({
                        "id": ch["id"],
                        "name": ch.get("name", ""),
                        "type": "channel",
                    })
                cursor = response.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
        except Exception:
            logger.exception("Failed to list Slack channels")
        return channels

    async def health_check(self) -> dict[str, Any]:
        if not self._client:
            return {"connected": False, "details": "Client not started"}
        try:
            response = await self._client.auth_test()
            return {
                "connected": True,
                "bot_user_id": response["user_id"],
                "team": response.get("team", ""),
            }
        except Exception as exc:
            return {"connected": False, "details": str(exc)}

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    def _check_authorized(self, user_id: str) -> bool:
        if not self.allowed_users:
            return True
        return user_id in self.allowed_users

    async def _handle_message(self, event: dict) -> None:
        """Handle incoming message events."""
        # Ignore bot messages and message subtypes (edits, joins, etc.)
        if event.get("subtype") or event.get("bot_id"):
            return
        if event.get("user") == self._bot_user_id:
            return

        user_id = event.get("user", "")
        if not self._check_authorized(user_id):
            return

        text = event.get("text", "")
        channel_id = event.get("channel", "")

        # Check for /command prefix in message text
        is_command = False
        command_name = ""
        command_args: list[str] = []
        if text.startswith("/"):
            parts = text.split()
            command_name = parts[0].lstrip("/")
            command_args = parts[1:]
            is_command = True

        # Parse routing (@project[:agent] prefix)
        project_name = ""
        agent_id = ""
        if not is_command:
            project_name, agent_id = self._parse_routing(text)
            if project_name:
                match = re.match(r"^@[\w-]+(?::[\w-]+)?\s+(.*)", text, re.DOTALL)
                text = match.group(1).strip() if match else text

        # Handle file attachments
        media_paths = await self._download_files(event.get("files", []))

        msg = InboundMessage(
            connector_id=self.connector_id,
            channel_id=channel_id,
            sender_id=user_id,
            sender_name=user_id,
            text=text,
            media_paths=media_paths,
            project_name=project_name,
            agent_id=agent_id,
            is_command=is_command,
            command_name=command_name,
            command_args=command_args,
            raw=event,
        )

        if self._message_callback:
            await self._message_callback(msg)

    async def _handle_app_mention(self, event: dict) -> None:
        """Handle @bot mentions in channels."""
        if event.get("subtype") or event.get("bot_id"):
            return

        user_id = event.get("user", "")
        if not self._check_authorized(user_id):
            return

        text = event.get("text", "")
        channel_id = event.get("channel", "")

        # Strip the <@BOT_ID> mention from text
        text = re.sub(rf"<@{re.escape(self._bot_user_id)}>\s*", "", text).strip()

        # Parse routing from remaining text
        project_name, agent_id = self._parse_routing(text)
        if project_name:
            match = re.match(r"^@[\w-]+(?::[\w-]+)?\s+(.*)", text, re.DOTALL)
            text = match.group(1).strip() if match else text

        media_paths = await self._download_files(event.get("files", []))

        msg = InboundMessage(
            connector_id=self.connector_id,
            channel_id=channel_id,
            sender_id=user_id,
            sender_name=user_id,
            text=text,
            media_paths=media_paths,
            project_name=project_name,
            agent_id=agent_id,
            raw=event,
        )

        if self._message_callback:
            await self._message_callback(msg)

    async def _handle_block_action(self, body: dict) -> None:
        """Handle Block Kit button clicks (ctrl_* action_ids)."""
        user = body.get("user", {})
        user_id = user.get("id", "")
        if not self._check_authorized(user_id):
            return

        actions = body.get("actions", [])
        if not actions:
            return

        action = actions[0]
        action_id = action.get("action_id", "")
        # Parse action_id: "ctrl_{agent_id}_{action}"
        match = re.match(r"^ctrl_([^_]+)_(.+)$", action_id)
        if not match:
            return

        agent_id = match.group(1)
        action_name = match.group(2)
        channel_id = body.get("channel", {}).get("id", "")

        msg = InboundMessage(
            connector_id=self.connector_id,
            channel_id=channel_id,
            sender_id=user_id,
            sender_name=user.get("name", ""),
            is_command=True,
            command_name=action_name,
            command_args=[agent_id],
            raw=body,
        )

        if self._message_callback:
            await self._message_callback(msg)

    async def _download_files(self, files: list[dict]) -> list[str]:
        """Download Slack file attachments using httpx."""
        if not files:
            return []
        media_paths: list[str] = []
        try:
            import httpx

            async with httpx.AsyncClient() as client:
                for file_info in files:
                    url = file_info.get("url_private_download") or file_info.get(
                        "url_private"
                    )
                    if not url:
                        continue
                    response = await client.get(
                        url,
                        headers={"Authorization": f"Bearer {self.bot_token}"},
                    )
                    if response.status_code == 200:
                        tmp_dir = tempfile.mkdtemp(prefix="forge_slack_")
                        file_name = file_info.get("name", "attachment")
                        tmp_path = Path(tmp_dir) / file_name
                        tmp_path.write_bytes(response.content)
                        media_paths.append(str(tmp_path))
        except Exception:
            logger.exception("Failed to download Slack files")
        return media_paths

    @staticmethod
    def _parse_routing(text: str) -> tuple[str, str]:
        """Extract @project[:agent_id] from text. Returns (project, agent_id)."""
        match = re.match(r"^@([\w-]+)(?::([\w-]+))?\s", text)
        if not match:
            return "", ""
        return match.group(1), match.group(2) or ""
