"""Comprehensive test suite for DiscordConnector.

Tests Discord bot connector implementation including:
- Config parsing and initialization
- Authorization checks
- Message routing and parsing
- Event handlers (on_message, on_interaction)
- Message sending with buttons and media
- Text splitting for Discord's 2000-char limit
- Channel operations
- Health checks
- Lifecycle management
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

# Mock discord module before importing DiscordConnector
mock_discord = MagicMock()
mock_discord.Intents = MagicMock()
mock_discord.Intents.default = MagicMock(return_value=MagicMock())
mock_discord.Client = MagicMock()
mock_discord.ui = MagicMock()
mock_discord.ui.View = MagicMock()
mock_discord.ui.Button = MagicMock()
mock_discord.ButtonStyle = MagicMock()
mock_discord.ButtonStyle.success = MagicMock()
mock_discord.ButtonStyle.danger = MagicMock()
mock_discord.ButtonStyle.primary = MagicMock()
mock_discord.File = MagicMock()
sys.modules["discord"] = mock_discord

from agent_forge.connectors.base import ActionButton, InboundMessage, OutboundMessage
from agent_forge.connectors.discord import DiscordConnector


# ------------------------------------------------------------------
# Mock Factories
# ------------------------------------------------------------------


def _make_config(bot_token="test-token", guild_ids=None, allowed_users=None):
    """Create a Discord connector config dict."""
    return {
        "credentials": {"bot_token": bot_token},
        "settings": {
            "guild_ids": guild_ids or [],
            "allowed_users": allowed_users or [],
        },
    }


def _make_mock_message(
    author_id=111,
    author_bot=False,
    content="hello",
    guild_id=1000,
    channel_id=2000,
    attachments=None,
):
    """Create a mock discord.Message."""
    msg = MagicMock()
    msg.author = MagicMock()
    msg.author.id = author_id
    msg.author.bot = author_bot
    msg.content = content
    msg.guild = MagicMock()
    msg.guild.id = guild_id
    msg.channel = MagicMock()
    msg.channel.id = channel_id
    msg.attachments = attachments or []
    return msg


def _make_mock_attachment(filename="test.jpg", url="https://cdn.discord.com/test.jpg"):
    """Create a mock discord.Attachment."""
    attachment = MagicMock()
    attachment.filename = filename
    attachment.url = url
    attachment.save = AsyncMock()
    return attachment


def _make_mock_interaction(
    user_id=111, custom_id="ctrl:abc123:approve", guild_id=1000
):
    """Create a mock discord.Interaction for button click."""
    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.user.name = "TestUser"
    interaction.guild = MagicMock()
    interaction.guild.id = guild_id
    interaction.channel_id = 2000
    interaction.data = {"custom_id": custom_id}
    # Mock interaction type as component interaction
    interaction.type = MagicMock()
    interaction.type.name = "component"
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _make_mock_channel(channel_id=2000, name="general", type_value=0, guild_id=1000):
    """Create a mock discord.TextChannel."""
    channel = MagicMock()
    channel.id = channel_id
    channel.name = name
    channel.type = MagicMock()
    channel.type.value = type_value
    channel.guild = MagicMock()
    channel.guild.id = guild_id
    channel.guild.name = f"Guild-{guild_id}"
    channel.send = AsyncMock()
    return channel


# ------------------------------------------------------------------
# TestInit — Config parsing, default values
# ------------------------------------------------------------------


class TestInit:
    """Test DiscordConnector initialization and config parsing."""

    def test_init_basic(self):
        """Test basic initialization with minimal config."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        assert connector.connector_id == "disc1"
        assert connector.bot_token == "test-token"
        assert connector.guild_ids == []
        assert connector.allowed_users == []
        assert connector._client is None
        assert connector._task is None
        assert isinstance(connector._ready_event, asyncio.Event)
        assert connector._recent_channels == {}

    def test_init_with_guild_ids(self):
        """Test guild_ids are parsed and converted to int."""
        config = _make_config(guild_ids=["1000", "2000"])
        connector = DiscordConnector(connector_id="disc1", config=config)

        assert connector.guild_ids == [1000, 2000]

    def test_init_with_allowed_users(self):
        """Test allowed_users are parsed and converted to int."""
        config = _make_config(allowed_users=["111", "222", "333"])
        connector = DiscordConnector(connector_id="disc1", config=config)

        assert connector.allowed_users == [111, 222, 333]

    def test_init_with_all_settings(self):
        """Test full config with all settings."""
        config = _make_config(
            bot_token="secret-token",
            guild_ids=["1000"],
            allowed_users=["111"],
        )
        connector = DiscordConnector(connector_id="disc1", config=config)

        assert connector.bot_token == "secret-token"
        assert connector.guild_ids == [1000]
        assert connector.allowed_users == [111]

    def test_init_missing_credentials(self):
        """Test initialization with missing credentials section."""
        config = {"settings": {}}
        connector = DiscordConnector(connector_id="disc1", config=config)

        assert connector.bot_token == ""
        assert connector.guild_ids == []
        assert connector.allowed_users == []

    def test_init_integer_ids_preserved(self):
        """Test that integer IDs in config are preserved."""
        config = _make_config(guild_ids=[1000, 2000], allowed_users=[111, 222])
        connector = DiscordConnector(connector_id="disc1", config=config)

        assert connector.guild_ids == [1000, 2000]
        assert connector.allowed_users == [111, 222]

    def test_init_persisted_known_channels(self):
        """Test that persisted known_channels are loaded from config."""
        config = _make_config()
        config["settings"]["known_channels"] = {
            "2000": {"name": "general", "type": "text", "guild": "MyGuild"}
        }
        connector = DiscordConnector(connector_id="disc1", config=config)

        assert "2000" in connector._recent_channels
        assert connector._recent_channels["2000"]["name"] == "general"


# ------------------------------------------------------------------
# TestCheckAuthorized — Authorization logic
# ------------------------------------------------------------------


class TestCheckAuthorized:
    """Test user authorization checks."""

    def test_empty_allowlist_permits_all(self):
        """Test that empty allowed_users list permits all users."""
        config = _make_config(allowed_users=[])
        connector = DiscordConnector(connector_id="disc1", config=config)

        assert connector._check_authorized(999) is True
        assert connector._check_authorized(111) is True
        assert connector._check_authorized(0) is True

    def test_allowlist_permits_whitelisted_users(self):
        """Test that whitelisted users are authorized."""
        config = _make_config(allowed_users=[111, 222])
        connector = DiscordConnector(connector_id="disc1", config=config)

        assert connector._check_authorized(111) is True
        assert connector._check_authorized(222) is True

    def test_allowlist_rejects_non_whitelisted_users(self):
        """Test that non-whitelisted users are rejected."""
        config = _make_config(allowed_users=[111, 222])
        connector = DiscordConnector(connector_id="disc1", config=config)

        assert connector._check_authorized(333) is False
        assert connector._check_authorized(999) is False


# ------------------------------------------------------------------
# TestParseRouting — Message routing extraction
# ------------------------------------------------------------------


class TestParseRouting:
    """Test @project[:agent_id] routing pattern parsing."""

    def test_parse_routing_project_only(self):
        """Test parsing @project format."""
        project, agent_id = DiscordConnector._parse_routing("@my-project hello")

        assert project == "my-project"
        assert agent_id == ""

    def test_parse_routing_project_and_agent(self):
        """Test parsing @project:agent_id format."""
        project, agent_id = DiscordConnector._parse_routing("@my-project:abc123 hello")

        assert project == "my-project"
        assert agent_id == "abc123"

    def test_parse_routing_no_prefix(self):
        """Test text without @project prefix returns empty."""
        project, agent_id = DiscordConnector._parse_routing("just plain text")

        assert project == ""
        assert agent_id == ""

    def test_parse_routing_at_without_space(self):
        """Test @project without trailing space is not matched."""
        project, agent_id = DiscordConnector._parse_routing("@project")

        assert project == ""
        assert agent_id == ""

    def test_parse_routing_multiline(self):
        """Test routing extraction works with multiline text."""
        project, agent_id = DiscordConnector._parse_routing(
            "@proj do this\nand that\nand more"
        )

        assert project == "proj"
        assert agent_id == ""

    def test_parse_routing_with_hyphens_and_underscores(self):
        """Test project names with hyphens and agent IDs work."""
        project, agent_id = DiscordConnector._parse_routing("@my-api-v2:x1y2z3 go")

        assert project == "my-api-v2"
        assert agent_id == "x1y2z3"

    def test_parse_routing_only_at_beginning(self):
        """Test routing only matches at the beginning of text."""
        project, agent_id = DiscordConnector._parse_routing(
            "hello @project this should not match"
        )

        assert project == ""
        assert agent_id == ""


# ------------------------------------------------------------------
# TestOnMessage — Message event handler
# ------------------------------------------------------------------


class TestOnMessage:
    """Test on_message handler for processing incoming Discord messages."""

    @pytest.mark.asyncio
    async def test_ignores_own_messages(self):
        """Test that messages from the bot itself are ignored."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.user = MagicMock()
        mock_client.user.id = 999
        connector._client = mock_client

        callback = AsyncMock()
        connector.set_message_callback(callback)

        # Message from the bot itself
        message = _make_mock_message(author_id=999, author_bot=True)
        message.author = mock_client.user

        await connector._on_message(message)

        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_filters_by_guild_ids(self):
        """Test that messages from non-matching guilds are ignored when guild_ids is set."""
        config = _make_config(guild_ids=[1000])
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.user = MagicMock()
        mock_client.user.id = 999
        connector._client = mock_client

        callback = AsyncMock()
        connector.set_message_callback(callback)

        # Message from guild 2000 (not in guild_ids)
        message = _make_mock_message(guild_id=2000)

        await connector._on_message(message)

        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allows_message_from_matching_guild(self):
        """Test that messages from matching guilds are processed."""
        config = _make_config(guild_ids=[1000], allowed_users=[])
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.user = MagicMock()
        mock_client.user.id = 999
        connector._client = mock_client

        callback = AsyncMock()
        connector.set_message_callback(callback)

        # Message from guild 1000 (in guild_ids)
        message = _make_mock_message(guild_id=1000, content="@proj hello")

        await connector._on_message(message)

        callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_checks_authorization(self):
        """Test that unauthorized users are rejected."""
        config = _make_config(allowed_users=[111])
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.user = MagicMock()
        mock_client.user.id = 999
        connector._client = mock_client

        callback = AsyncMock()
        connector.set_message_callback(callback)

        # Message from unauthorized user
        message = _make_mock_message(author_id=222, content="hello")

        await connector._on_message(message)

        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tracks_recent_channels(self):
        """Test that message channels are tracked in _recent_channels."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.user = MagicMock()
        mock_client.user.id = 999
        connector._client = mock_client

        callback = AsyncMock()
        connector.set_message_callback(callback)

        message = _make_mock_message(channel_id=2000, content="@proj hello")
        message.channel.name = "general"
        message.channel.type = MagicMock()
        message.channel.type.__str__ = MagicMock(return_value="text")
        message.channel.guild = MagicMock()
        message.channel.guild.name = "MyGuild"

        await connector._on_message(message)

        assert "2000" in connector._recent_channels
        # Name includes guild prefix
        assert "general" in connector._recent_channels["2000"]["name"]
        assert "MyGuild" in connector._recent_channels["2000"]["name"]

    @pytest.mark.asyncio
    async def test_command_detection_slash_prefix(self):
        """Test that messages starting with / are treated as commands."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.user = MagicMock()
        mock_client.user.id = 999
        connector._client = mock_client

        callback = AsyncMock()
        connector.set_message_callback(callback)

        message = _make_mock_message(content="/status abc123")

        await connector._on_message(message)

        callback.assert_awaited_once()
        inbound = callback.await_args[0][0]
        assert inbound.is_command is True
        assert inbound.command_name == "status"
        assert inbound.command_args == ["abc123"]

    @pytest.mark.asyncio
    async def test_command_with_multiple_args(self):
        """Test command parsing with multiple arguments."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.user = MagicMock()
        mock_client.user.id = 999
        connector._client = mock_client

        callback = AsyncMock()
        connector.set_message_callback(callback)

        message = _make_mock_message(content="/spawn project task description")

        await connector._on_message(message)

        inbound = callback.await_args[0][0]
        assert inbound.is_command is True
        assert inbound.command_name == "spawn"
        assert inbound.command_args == ["project", "task", "description"]

    @pytest.mark.asyncio
    async def test_routing_via_at_project(self):
        """Test @project routing extraction."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.user = MagicMock()
        mock_client.user.id = 999
        connector._client = mock_client

        callback = AsyncMock()
        connector.set_message_callback(callback)

        message = _make_mock_message(content="@my-project fix the bug")

        await connector._on_message(message)

        inbound = callback.await_args[0][0]
        assert inbound.project_name == "my-project"
        assert inbound.agent_id == ""
        assert inbound.text == "fix the bug"

    @pytest.mark.asyncio
    async def test_routing_via_at_project_agent(self):
        """Test @project:agent_id routing extraction."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.user = MagicMock()
        mock_client.user.id = 999
        connector._client = mock_client

        callback = AsyncMock()
        connector.set_message_callback(callback)

        message = _make_mock_message(content="@my-project:abc123 deploy now")

        await connector._on_message(message)

        inbound = callback.await_args[0][0]
        assert inbound.project_name == "my-project"
        assert inbound.agent_id == "abc123"
        assert inbound.text == "deploy now"

    @pytest.mark.asyncio
    async def test_callback_invoked_with_correct_inbound_message(self):
        """Test that InboundMessage is built correctly."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.user = MagicMock()
        mock_client.user.id = 999
        connector._client = mock_client

        callback = AsyncMock()
        connector.set_message_callback(callback)

        message = _make_mock_message(
            author_id=111,
            content="@proj hello world",
            channel_id=2000,
        )
        message.author.name = "TestUser"

        await connector._on_message(message)

        callback.assert_awaited_once()
        inbound = callback.await_args[0][0]
        assert isinstance(inbound, InboundMessage)
        assert inbound.connector_id == "disc1"
        assert inbound.channel_id == "2000"
        assert inbound.sender_id == "111"
        assert inbound.sender_name == "TestUser"
        assert inbound.text == "hello world"
        assert inbound.project_name == "proj"
        assert inbound.agent_id == ""
        assert inbound.is_command is False
        assert inbound.raw == message

    @pytest.mark.asyncio
    async def test_attachments_downloaded(self):
        """Test that message attachments are downloaded to temp files."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.user = MagicMock()
        mock_client.user.id = 999
        connector._client = mock_client

        callback = AsyncMock()
        connector.set_message_callback(callback)

        attachment = _make_mock_attachment(filename="test.jpg")
        message = _make_mock_message(
            content="@proj check this", attachments=[attachment]
        )

        await connector._on_message(message)

        attachment.save.assert_awaited_once()
        inbound = callback.await_args[0][0]
        assert len(inbound.media_paths) == 1
        assert "test.jpg" in inbound.media_paths[0]

    @pytest.mark.asyncio
    async def test_multiple_attachments_downloaded(self):
        """Test that multiple attachments are all downloaded."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.user = MagicMock()
        mock_client.user.id = 999
        connector._client = mock_client

        callback = AsyncMock()
        connector.set_message_callback(callback)

        attachment1 = _make_mock_attachment(filename="img1.jpg")
        attachment2 = _make_mock_attachment(filename="img2.png")
        message = _make_mock_message(
            content="@proj images", attachments=[attachment1, attachment2]
        )

        await connector._on_message(message)

        assert attachment1.save.await_count == 1
        assert attachment2.save.await_count == 1
        inbound = callback.await_args[0][0]
        assert len(inbound.media_paths) == 2

    @pytest.mark.asyncio
    async def test_attachment_download_failure_handled(self):
        """Test that attachment download failures don't crash the handler."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.user = MagicMock()
        mock_client.user.id = 999
        connector._client = mock_client

        callback = AsyncMock()
        connector.set_message_callback(callback)

        attachment = _make_mock_attachment()
        attachment.save.side_effect = Exception("Download failed")
        message = _make_mock_message(
            content="@proj hello", attachments=[attachment]
        )

        await connector._on_message(message)

        # Callback should still be called, just without media
        callback.assert_awaited_once()
        inbound = callback.await_args[0][0]
        assert len(inbound.media_paths) == 0


# ------------------------------------------------------------------
# TestSendMessage — Outbound message delivery
# ------------------------------------------------------------------


class TestSendMessage:
    """Test send_message for delivering outbound messages to Discord."""

    @pytest.mark.asyncio
    async def test_returns_false_when_client_not_running(self):
        """Test that send_message returns False when client is not started."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        message = OutboundMessage(channel_id="2000", text="hello")
        result = await connector.send_message(message)

        assert result is False

    @pytest.mark.asyncio
    async def test_text_delivery(self):
        """Test basic text message delivery."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_channel = _make_mock_channel(channel_id=2000)
        mock_client.get_channel.return_value = mock_channel
        connector._client = mock_client

        message = OutboundMessage(channel_id="2000", text="Hello, Discord!")

        result = await connector.send_message(message)

        assert result is True
        mock_channel.send.assert_awaited_once()
        call_kwargs = mock_channel.send.call_args[1]
        assert call_kwargs["content"] == "Hello, Discord!"
        assert call_kwargs.get("view") is None

    @pytest.mark.asyncio
    async def test_button_rendering_as_view(self):
        """Test that ActionButtons are rendered as discord.ui.View."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_channel = _make_mock_channel()
        mock_client.get_channel.return_value = mock_channel
        connector._client = mock_client

        buttons = [
            ActionButton(label="Approve", action="approve", agent_id="abc123"),
            ActionButton(label="Reject", action="reject", agent_id="abc123"),
        ]
        message = OutboundMessage(
            channel_id="2000", text="Review needed", extra={"action_buttons": buttons}
        )

        # Reset mock counters
        mock_discord.ui.View.reset_mock()
        mock_discord.ui.Button.reset_mock()

        mock_view = MagicMock()
        mock_button = MagicMock()
        mock_discord.ui.View.return_value = mock_view
        mock_discord.ui.Button.return_value = mock_button

        result = await connector.send_message(message)

        assert result is True
        # View should be created
        mock_discord.ui.View.assert_called_once()
        # Buttons should be added with correct custom_id format
        assert mock_view.add_item.call_count == 2

    @pytest.mark.asyncio
    async def test_text_splitting_at_2000_chars(self):
        """Test that long text is split at Discord's 2000-char limit."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_channel = _make_mock_channel()
        mock_client.get_channel.return_value = mock_channel
        connector._client = mock_client

        # Create text longer than 2000 chars
        long_text = "Line\n" * 500  # 2500 chars
        message = OutboundMessage(channel_id="2000", text=long_text)

        result = await connector.send_message(message)

        assert result is True
        # Should send multiple chunks
        assert mock_channel.send.await_count > 1

    @pytest.mark.asyncio
    async def test_view_attached_to_last_chunk(self):
        """Test that when text is split, the View is attached only to the last chunk."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_channel = _make_mock_channel()
        mock_client.get_channel.return_value = mock_channel
        connector._client = mock_client

        long_text = "Line\n" * 500  # Forces split
        buttons = [ActionButton(label="OK", action="ok", agent_id="abc123")]
        message = OutboundMessage(
            channel_id="2000", text=long_text, extra={"action_buttons": buttons}
        )

        # Reset mock counters
        mock_discord.ui.View.reset_mock()
        mock_discord.ui.Button.reset_mock()

        mock_view = MagicMock()
        mock_button = MagicMock()
        mock_discord.ui.View.return_value = mock_view
        mock_discord.ui.Button.return_value = mock_button

        result = await connector.send_message(message)

        assert result is True
        # Last call should have view parameter
        last_call_kwargs = mock_channel.send.call_args_list[-1][1]
        # View attached to final text chunk, not to media sends

    @pytest.mark.asyncio
    async def test_media_files_sent(self):
        """Test that media files are sent as discord.File."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_channel = _make_mock_channel()
        mock_client.get_channel.return_value = mock_channel
        connector._client = mock_client

        # Create temp file
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as tmp:
            tmp.write("test content")
            tmp_path = tmp.name

        try:
            message = OutboundMessage(
                channel_id="2000", text="See attachment", media_paths=[tmp_path]
            )

            # Reset mock
            mock_discord.File.reset_mock()
            mock_file = MagicMock()
            mock_discord.File.return_value = mock_file

            result = await connector.send_message(message)

            assert result is True
            # File should be created and sent
            mock_discord.File.assert_called_once_with(tmp_path)
            # Channel.send called for text + media
            assert mock_channel.send.await_count == 2
        finally:
            Path(tmp_path).unlink()

    @pytest.mark.asyncio
    async def test_multiple_media_files_sent(self):
        """Test that multiple media files are all sent."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_channel = _make_mock_channel()
        mock_client.get_channel.return_value = mock_channel
        connector._client = mock_client

        # Create temp files
        tmp_files = []
        for i in range(3):
            tmp = tempfile.NamedTemporaryFile(mode="w", delete=False)
            tmp.write(f"content {i}")
            tmp.close()
            tmp_files.append(tmp.name)

        try:
            message = OutboundMessage(
                channel_id="2000", text="Files", media_paths=tmp_files
            )

            result = await connector.send_message(message)

            assert result is True
            # 1 text + 3 media = 4 calls
            assert mock_channel.send.await_count == 4
        finally:
            for path in tmp_files:
                Path(path).unlink()

    @pytest.mark.asyncio
    async def test_fetch_channel_fallback(self):
        """Test that fetch_channel is used as fallback when get_channel returns None."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_channel = _make_mock_channel()
        mock_client.get_channel.return_value = None
        mock_client.fetch_channel = AsyncMock(return_value=mock_channel)
        connector._client = mock_client

        message = OutboundMessage(channel_id="2000", text="hello")

        result = await connector.send_message(message)

        assert result is True
        mock_client.fetch_channel.assert_awaited_once_with(2000)

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        """Test that send_message returns False when an exception occurs."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.get_channel.side_effect = Exception("API error")
        connector._client = mock_client

        message = OutboundMessage(channel_id="2000", text="hello")
        result = await connector.send_message(message)

        assert result is False


# ------------------------------------------------------------------
# TestSplitMessage — Text splitting utility
# ------------------------------------------------------------------


class TestSplitMessage:
    """Test _split_message utility for splitting long text."""

    def test_short_text_unchanged(self):
        """Test that text shorter than limit is returned as-is."""
        connector = DiscordConnector(connector_id="disc1", config=_make_config())
        text = "Short message"
        result = connector._split_message(text)

        assert result == ["Short message"]

    def test_long_text_split_at_newlines(self):
        """Test that long text is split at newlines."""
        connector = DiscordConnector(connector_id="disc1", config=_make_config())
        text = "Line 1\n" * 300  # ~2100 chars
        result = connector._split_message(text)

        assert len(result) == 2
        assert all(len(chunk) <= 2000 for chunk in result)

    def test_very_long_single_line_split_at_limit(self):
        """Test that a single very long line is split at the limit."""
        connector = DiscordConnector(connector_id="disc1", config=_make_config())
        text = "a" * 3000
        result = connector._split_message(text)

        assert len(result) == 2
        assert len(result[0]) == 2000
        assert len(result[1]) == 1000

    def test_preserves_content(self):
        """Test that splitting preserves all content."""
        connector = DiscordConnector(connector_id="disc1", config=_make_config())
        text = "Line 1\nLine 2\nLine 3\n" * 200
        result = connector._split_message(text)

        reconstructed = "".join(result)
        assert reconstructed == text

    def test_empty_string(self):
        """Test that empty string returns single empty chunk."""
        connector = DiscordConnector(connector_id="disc1", config=_make_config())
        result = connector._split_message("")

        assert result == [""]

    def test_exact_limit(self):
        """Test text exactly at limit is not split."""
        connector = DiscordConnector(connector_id="disc1", config=_make_config())
        text = "a" * 2000
        result = connector._split_message(text)

        assert len(result) == 1
        assert result[0] == text


# ------------------------------------------------------------------
# TestOnInteraction — Button click handler
# ------------------------------------------------------------------


class TestOnInteraction:
    """Test on_interaction handler for processing button clicks."""

    @pytest.mark.asyncio
    async def test_parses_ctrl_custom_id_format(self):
        """Test that ctrl:agent:action format is parsed correctly."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        callback = AsyncMock()
        connector.set_message_callback(callback)

        interaction = _make_mock_interaction(custom_id="ctrl:abc123:approve")

        await connector._on_interaction(interaction)

        callback.assert_awaited_once()
        inbound = callback.await_args[0][0]
        assert inbound.is_command is True
        assert inbound.command_name == "approve"
        assert inbound.command_args == ["abc123"]

    @pytest.mark.asyncio
    async def test_unauthorized_user_rejected(self):
        """Test that unauthorized users cannot use buttons."""
        config = _make_config(allowed_users=[111])
        connector = DiscordConnector(connector_id="disc1", config=config)

        callback = AsyncMock()
        connector.set_message_callback(callback)

        interaction = _make_mock_interaction(
            user_id=222, custom_id="ctrl:abc123:approve"
        )

        await connector._on_interaction(interaction)

        callback.assert_not_awaited()
        interaction.response.send_message.assert_awaited_once()
        # Check both positional and keyword arguments
        call_args = interaction.response.send_message.call_args
        if call_args[0]:  # Positional args
            assert "not authorized" in call_args[0][0].lower()
        assert call_args[1]["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_invalid_format_ignored(self):
        """Test that invalid custom_id format is ignored."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        callback = AsyncMock()
        connector.set_message_callback(callback)

        interaction = _make_mock_interaction(custom_id="invalid_format")

        await connector._on_interaction(interaction)

        callback.assert_not_awaited()
        # No response sent for invalid format

    @pytest.mark.asyncio
    async def test_ephemeral_response_sent(self):
        """Test that ephemeral confirmation is sent after button click."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        callback = AsyncMock()
        connector.set_message_callback(callback)

        interaction = _make_mock_interaction(custom_id="ctrl:abc123:approve")

        await connector._on_interaction(interaction)

        interaction.response.send_message.assert_awaited_once()
        call_args = interaction.response.send_message.call_args
        assert call_args[1]["ephemeral"] is True
        # Check positional arg for content
        if call_args[0]:
            assert "approve" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_different_actions_parsed(self):
        """Test that different action types are parsed correctly."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        callback = AsyncMock()
        connector.set_message_callback(callback)

        actions = ["approve", "reject", "interrupt", "custom_action"]

        for action in actions:
            callback.reset_mock()
            interaction = _make_mock_interaction(
                custom_id=f"ctrl:abc123:{action}"
            )

            await connector._on_interaction(interaction)

            inbound = callback.await_args[0][0]
            assert inbound.command_name == action

    @pytest.mark.asyncio
    async def test_inbound_message_fields_populated(self):
        """Test that InboundMessage is populated correctly from interaction."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        callback = AsyncMock()
        connector.set_message_callback(callback)

        interaction = _make_mock_interaction(
            user_id=111, custom_id="ctrl:abc123:approve", guild_id=1000
        )
        interaction.channel = MagicMock()
        interaction.channel.id = 2000
        interaction.user.name = "TestUser"

        await connector._on_interaction(interaction)

        inbound = callback.await_args[0][0]
        assert inbound.connector_id == "disc1"
        assert inbound.channel_id == "2000"
        assert inbound.sender_id == "111"
        assert inbound.sender_name == "TestUser"
        assert inbound.raw == interaction


# ------------------------------------------------------------------
# TestChannelOps — Channel operations
# ------------------------------------------------------------------


class TestChannelOps:
    """Test channel validation, info, and listing operations."""

    @pytest.mark.asyncio
    async def test_validate_channel_exists(self):
        """Test validate_channel returns True for existing channel."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_channel = _make_mock_channel()
        mock_client.get_channel.return_value = mock_channel
        connector._client = mock_client

        result = await connector.validate_channel("2000")

        assert result is True

    @pytest.mark.asyncio
    async def test_validate_channel_not_found(self):
        """Test validate_channel returns False for non-existent channel."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.get_channel.return_value = None
        mock_client.fetch_channel = AsyncMock(return_value=None)
        connector._client = mock_client

        result = await connector.validate_channel("9999")

        assert result is False

    @pytest.mark.asyncio
    async def test_validate_channel_fetch_fallback(self):
        """Test validate_channel uses fetch_channel as fallback."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_channel = _make_mock_channel()
        mock_client.get_channel.return_value = None
        mock_client.fetch_channel = AsyncMock(return_value=mock_channel)
        connector._client = mock_client

        result = await connector.validate_channel("2000")

        assert result is True
        mock_client.fetch_channel.assert_awaited_once_with(2000)

    @pytest.mark.asyncio
    async def test_get_channel_info_structure(self):
        """Test get_channel_info returns correct structure."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_channel = _make_mock_channel(
            channel_id=2000, name="general", type_value=0, guild_id=1000
        )
        # Mock channel.type to return a type with string representation
        mock_channel.type = MagicMock()
        mock_channel.type.__str__ = MagicMock(return_value="text")
        mock_client.get_channel.return_value = mock_channel
        connector._client = mock_client

        result = await connector.get_channel_info("2000")

        assert result["id"] == "2000"
        assert result["name"] == "general"
        assert result["type"] == "text"
        assert "guild" in result

    @pytest.mark.asyncio
    async def test_get_channel_info_not_found(self):
        """Test get_channel_info returns empty dict for non-existent channel."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.get_channel.return_value = None
        mock_client.fetch_channel = AsyncMock(return_value=None)
        connector._client = mock_client

        result = await connector.get_channel_info("9999")

        assert result == {}

    @pytest.mark.asyncio
    async def test_list_channels_with_guilds(self):
        """Test list_channels returns accessible text channels from guilds."""
        config = _make_config(guild_ids=[1000])
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_guild = MagicMock()
        mock_guild.id = 1000
        mock_guild.name = "MyGuild"
        mock_guild.me = MagicMock()  # Bot member object

        channel1 = _make_mock_channel(channel_id=2000, name="general")
        channel2 = _make_mock_channel(channel_id=2001, name="random")

        # Mock permissions
        mock_perms = MagicMock()
        mock_perms.send_messages = True
        channel1.permissions_for = MagicMock(return_value=mock_perms)
        channel2.permissions_for = MagicMock(return_value=mock_perms)

        # Mock channel.type
        channel1.type = MagicMock()
        channel1.type.__str__ = MagicMock(return_value="text")
        channel2.type = MagicMock()
        channel2.type.__str__ = MagicMock(return_value="text")

        mock_guild.text_channels = [channel1, channel2]

        mock_client.guilds = [mock_guild]
        connector._client = mock_client

        result = await connector.list_channels()

        assert len(result) == 2
        assert result[0]["id"] == "2000"
        assert "general" in result[0]["name"]
        assert result[1]["id"] == "2001"
        assert "random" in result[1]["name"]

    @pytest.mark.asyncio
    async def test_list_channels_recent_channels_fallback(self):
        """Test list_channels falls back to _recent_channels when no guilds."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        connector._recent_channels = {
            "2000": {"name": "general", "type": "text", "guild": "MyGuild"},
            "2001": {"name": "random", "type": "text", "guild": "MyGuild"},
        }

        mock_client = MagicMock()
        mock_client.guilds = []
        connector._client = mock_client

        result = await connector.list_channels()

        assert len(result) == 2
        ids = {ch["id"] for ch in result}
        assert "2000" in ids
        assert "2001" in ids

    @pytest.mark.asyncio
    async def test_list_channels_not_started(self):
        """Test list_channels returns empty list when not started."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        result = await connector.list_channels()

        assert result == []

    @pytest.mark.asyncio
    async def test_list_channels_filters_by_guild_ids(self):
        """Test list_channels only returns channels from configured guilds."""
        config = _make_config(guild_ids=[1000])
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()

        guild1 = MagicMock()
        guild1.id = 1000
        channel1 = _make_mock_channel(channel_id=2000, name="allowed")
        guild1.text_channels = [channel1]

        guild2 = MagicMock()
        guild2.id = 2000
        channel2 = _make_mock_channel(channel_id=3000, name="not-allowed")
        guild2.text_channels = [channel2]

        mock_client.guilds = [guild1, guild2]
        connector._client = mock_client

        result = await connector.list_channels()

        assert len(result) == 1
        assert result[0]["id"] == "2000"


# ------------------------------------------------------------------
# TestHealthCheck — Health status reporting
# ------------------------------------------------------------------


class TestHealthCheck:
    """Test health_check status reporting."""

    @pytest.mark.asyncio
    async def test_connected_state_with_bot_info(self):
        """Test health_check returns connected state with bot info when running."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.user = MagicMock()
        mock_client.user.name = "TestBot"
        mock_client.user.id = 999
        mock_client.guilds = [MagicMock(), MagicMock()]
        mock_client.latency = 0.123
        connector._client = mock_client
        connector._ready_event = asyncio.Event()
        connector._ready_event.set()

        result = await connector.health_check()

        assert result["connected"] is True
        assert result["bot_username"] == "TestBot"
        assert result["bot_id"] == 999
        assert result["guild_count"] == 2
        assert result["latency_ms"] == 123

    @pytest.mark.asyncio
    async def test_not_started_state(self):
        """Test health_check returns not connected when not started."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        result = await connector.health_check()

        assert result["connected"] is False
        assert "details" in result

    @pytest.mark.asyncio
    async def test_not_ready_state(self):
        """Test health_check returns not connected when client user is None."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.user = None
        connector._client = mock_client

        result = await connector.health_check()

        assert result["connected"] is False

    @pytest.mark.asyncio
    async def test_latency_conversion_to_ms(self):
        """Test that latency is converted from seconds to milliseconds."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.user = MagicMock()
        mock_client.user.name = "TestBot"
        mock_client.user.id = 999
        mock_client.guilds = []
        mock_client.latency = 0.456  # seconds
        connector._client = mock_client
        connector._ready_event = asyncio.Event()
        connector._ready_event.set()

        result = await connector.health_check()

        assert result["latency_ms"] == 456


# ------------------------------------------------------------------
# TestLifecycle — Start/stop operations
# ------------------------------------------------------------------


class TestLifecycle:
    """Test start() and stop() lifecycle methods."""

    @pytest.mark.asyncio
    async def test_start_creates_client_and_task(self):
        """Test that start() creates discord.Client and background task."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.start = AsyncMock()

        # Mock the ready event to be set immediately
        async def mock_start_impl(token):
            if connector._ready_event:
                connector._ready_event.set()
            # Keep task running
            await asyncio.sleep(100)

        mock_client.start.side_effect = mock_start_impl

        with patch("discord.Client") as mock_client_class:
            mock_client_class.return_value = mock_client

            await connector.start()

            # Client created
            mock_client_class.assert_called_once()
            # Background task created
            assert connector._task is not None
            # Ready event set
            assert connector._ready_event.is_set()

            # Cleanup
            await connector.stop()

    @pytest.mark.asyncio
    async def test_start_awaits_ready_event(self):
        """Test that start() waits for ready event."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.start = AsyncMock()

        ready_set = False

        async def mock_start_impl(token):
            nonlocal ready_set
            await asyncio.sleep(0.1)
            if connector._ready_event:
                connector._ready_event.set()
                ready_set = True
            # Keep task running
            await asyncio.sleep(100)

        mock_client.start.side_effect = mock_start_impl

        with patch("discord.Client") as mock_client_class:
            mock_client_class.return_value = mock_client

            await connector.start()

            # Ready should be set
            assert ready_set is True

            # Cleanup
            await connector.stop()

    @pytest.mark.asyncio
    async def test_start_timeout_handling(self):
        """Test that start() handles timeout if client doesn't become ready."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.close = AsyncMock()
        # Never set ready event, so it times out
        mock_client.start = AsyncMock(side_effect=asyncio.sleep(100))

        with patch("discord.Client") as mock_client_class:
            mock_client_class.return_value = mock_client

            # Patch asyncio.wait_for to timeout immediately
            with patch("asyncio.wait_for") as mock_wait_for:
                mock_wait_for.side_effect = asyncio.TimeoutError()

                with pytest.raises(RuntimeError, match="failed to connect"):
                    await connector.start()

                # Should have attempted to close client
                mock_client.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_closes_client_and_cancels_task(self):
        """Test that stop() closes client and cancels background task."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.close = AsyncMock()
        connector._client = mock_client

        # Create a real asyncio task that can be cancelled
        async def dummy_task():
            await asyncio.sleep(100)

        mock_task = asyncio.create_task(dummy_task())
        connector._task = mock_task

        await connector.stop()

        mock_client.close.assert_awaited_once()
        assert mock_task.cancelled()
        assert connector._client is None
        assert connector._task is None

    @pytest.mark.asyncio
    async def test_stop_when_not_started(self):
        """Test that stop() is safe to call when not started."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        # Should not raise
        await connector.stop()

        assert connector._client is None
        assert connector._task is None

    @pytest.mark.asyncio
    async def test_stop_handles_exceptions(self):
        """Test that stop() handles exceptions during cleanup."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        mock_client = MagicMock()
        mock_client.close = AsyncMock(side_effect=Exception("Close failed"))
        connector._client = mock_client

        # Should not raise
        await connector.stop()

        # Cleanup should still happen
        assert connector._client is None


# ------------------------------------------------------------------
# TestGetKnownChats — Known channels persistence
# ------------------------------------------------------------------


class TestGetKnownChats:
    """Test get_known_chats for channel persistence."""

    def test_returns_recent_channels_dict(self):
        """Test that get_known_chats returns _recent_channels."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        connector._recent_channels = {
            "2000": {"name": "general", "type": "text", "guild": "MyGuild"},
            "2001": {"name": "random", "type": "text", "guild": "MyGuild"},
        }

        result = connector.get_known_channels()

        assert result == connector._recent_channels
        assert "2000" in result
        assert "2001" in result

    def test_returns_empty_dict_when_no_channels(self):
        """Test that get_known_chats returns empty dict when no channels tracked."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        result = connector.get_known_channels()

        assert result == {}

    def test_returns_plain_string_values(self):
        """Test that all values are plain strings for JSON serialization."""
        config = _make_config()
        connector = DiscordConnector(connector_id="disc1", config=config)

        connector._recent_channels = {
            "2000": {"name": "general", "type": "text", "guild": "MyGuild"}
        }

        result = connector.get_known_channels()

        for channel_id, info in result.items():
            assert isinstance(channel_id, str)
            for key, value in info.items():
                assert isinstance(value, str)
