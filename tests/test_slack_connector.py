"""Tests for SlackConnector — authorization, parsing, send, inbound handlers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_forge.connectors.base import ActionButton, ConnectorType, InboundMessage, OutboundMessage
from agent_forge.connectors.slack import SlackConnector


def _make_connector(
    allowed_users: list[str] | None = None,
) -> SlackConnector:
    """Create a SlackConnector with test config."""
    config = {
        "credentials": {
            "bot_token": "xoxb-test-token",
            "app_token": "xapp-test-token",
        },
        "settings": {
            "allowed_users": allowed_users if allowed_users is not None else ["U024BE7LH"],
        },
    }
    return SlackConnector("test-slack", config)


def _connected_connector(
    allowed_users: list[str] | None = None,
) -> SlackConnector:
    """Create a SlackConnector with a mocked client (simulates connected state)."""
    conn = _make_connector(allowed_users)
    conn._client = AsyncMock()
    conn._bot_user_id = "UBOTID"
    conn._running = True
    return conn


# ------------------------------------------------------------------
# Init
# ------------------------------------------------------------------


class TestInit:
    def test_token_extraction(self):
        conn = _make_connector()
        assert conn.bot_token == "xoxb-test-token"
        assert conn.app_token == "xapp-test-token"

    def test_allowed_users(self):
        conn = _make_connector(allowed_users=["U1", "U2"])
        assert conn.allowed_users == ["U1", "U2"]

    def test_connector_type(self):
        conn = _make_connector()
        assert conn.connector_type == ConnectorType.SLACK


# ------------------------------------------------------------------
# Authorization
# ------------------------------------------------------------------


class TestAuthorization:
    def test_authorized_user(self):
        conn = _make_connector(allowed_users=["U024BE7LH"])
        assert conn._check_authorized("U024BE7LH") is True

    def test_unauthorized_user(self):
        conn = _make_connector(allowed_users=["U024BE7LH"])
        assert conn._check_authorized("U_OTHER") is False

    def test_empty_allows_all(self):
        conn = _make_connector(allowed_users=[])
        assert conn._check_authorized("UANYONE") is True


# ------------------------------------------------------------------
# Parse routing
# ------------------------------------------------------------------


class TestParseRouting:
    def test_simple_project(self):
        project, agent_id = SlackConnector._parse_routing("@my-project fix the bug")
        assert project == "my-project"
        assert agent_id == ""

    def test_project_and_agent(self):
        project, agent_id = SlackConnector._parse_routing("@my-project:abc123 deploy it")
        assert project == "my-project"
        assert agent_id == "abc123"

    def test_no_match(self):
        project, agent_id = SlackConnector._parse_routing("just a plain message")
        assert project == ""
        assert agent_id == ""


# ------------------------------------------------------------------
# send_message
# ------------------------------------------------------------------


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_plain_text(self):
        conn = _connected_connector()
        msg = OutboundMessage(channel_id="C024BE91L", text="Hello from Agent Forge")
        result = await conn.send_message(msg)
        assert result is True
        conn._client.chat_postMessage.assert_awaited_once_with(
            channel="C024BE91L",
            text="Hello from Agent Forge",
            blocks=None,
        )

    @pytest.mark.asyncio
    async def test_with_action_buttons(self):
        conn = _connected_connector()
        buttons = [
            ActionButton(label="Approve", action="approve", agent_id="abc123"),
            ActionButton(label="Reject", action="reject", agent_id="abc123"),
        ]
        msg = OutboundMessage(
            channel_id="C024BE91L",
            text="Agent needs approval",
            extra={"action_buttons": buttons},
        )
        result = await conn.send_message(msg)
        assert result is True

        call_kwargs = conn._client.chat_postMessage.call_args[1]
        blocks = call_kwargs["blocks"]
        assert len(blocks) == 2
        assert blocks[0]["type"] == "section"
        assert blocks[1]["type"] == "actions"
        elements = blocks[1]["elements"]
        assert len(elements) == 2
        assert elements[0]["action_id"] == "ctrl_abc123_approve"
        assert elements[0]["value"] == "abc123:approve"
        assert elements[1]["action_id"] == "ctrl_abc123_reject"

    @pytest.mark.asyncio
    async def test_with_media_files(self):
        conn = _connected_connector()
        msg = OutboundMessage(
            channel_id="C024BE91L",
            text="See attached",
            media_paths=["/tmp/file1.png", "/tmp/file2.pdf"],
        )
        result = await conn.send_message(msg)
        assert result is True
        assert conn._client.files_upload_v2.await_count == 2
        conn._client.files_upload_v2.assert_any_await(
            channel="C024BE91L", file="/tmp/file1.png"
        )
        conn._client.files_upload_v2.assert_any_await(
            channel="C024BE91L", file="/tmp/file2.pdf"
        )

    @pytest.mark.asyncio
    async def test_not_connected_returns_false(self):
        conn = _make_connector()
        msg = OutboundMessage(channel_id="C024BE91L", text="Hello")
        result = await conn.send_message(msg)
        assert result is False


# ------------------------------------------------------------------
# Inbound message handler
# ------------------------------------------------------------------


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_ignores_bot_messages(self):
        conn = _connected_connector()
        callback = AsyncMock()
        conn.set_message_callback(callback)

        event = {"subtype": "bot_message", "user": "U024BE7LH", "text": "hi", "channel": "C1"}
        await conn._handle_message(event)
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ignores_own_messages(self):
        conn = _connected_connector()
        callback = AsyncMock()
        conn.set_message_callback(callback)

        event = {"user": "UBOTID", "text": "self echo", "channel": "C1"}
        await conn._handle_message(event)
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ignores_message_subtypes(self):
        conn = _connected_connector()
        callback = AsyncMock()
        conn.set_message_callback(callback)

        event = {"subtype": "channel_join", "user": "U024BE7LH", "text": "", "channel": "C1"}
        await conn._handle_message(event)
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_command_detection(self):
        conn = _connected_connector(allowed_users=[])
        callback = AsyncMock()
        conn.set_message_callback(callback)

        event = {"user": "U1", "text": "/status abc123", "channel": "C1"}
        await conn._handle_message(event)

        callback.assert_awaited_once()
        msg: InboundMessage = callback.call_args[0][0]
        assert msg.is_command is True
        assert msg.command_name == "status"
        assert msg.command_args == ["abc123"]

    @pytest.mark.asyncio
    async def test_text_routing(self):
        conn = _connected_connector(allowed_users=[])
        callback = AsyncMock()
        conn.set_message_callback(callback)

        event = {"user": "U1", "text": "@my-project fix the login bug", "channel": "C1"}
        await conn._handle_message(event)

        callback.assert_awaited_once()
        msg: InboundMessage = callback.call_args[0][0]
        assert msg.project_name == "my-project"
        assert msg.text == "fix the login bug"
        assert msg.is_command is False

    @pytest.mark.asyncio
    async def test_text_routing_with_agent(self):
        conn = _connected_connector(allowed_users=[])
        callback = AsyncMock()
        conn.set_message_callback(callback)

        event = {"user": "U1", "text": "@proj:abc123 deploy it", "channel": "C1"}
        await conn._handle_message(event)

        msg: InboundMessage = callback.call_args[0][0]
        assert msg.project_name == "proj"
        assert msg.agent_id == "abc123"
        assert msg.text == "deploy it"

    @pytest.mark.asyncio
    async def test_unauthorized_ignored(self):
        conn = _connected_connector(allowed_users=["U_ALLOWED"])
        callback = AsyncMock()
        conn.set_message_callback(callback)

        event = {"user": "U_NOT_ALLOWED", "text": "hello", "channel": "C1"}
        await conn._handle_message(event)
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_file_handling(self):
        conn = _connected_connector(allowed_users=[])
        callback = AsyncMock()
        conn.set_message_callback(callback)

        conn._download_files = AsyncMock(return_value=["/tmp/forge_slack_x/photo.jpg"])

        event = {
            "user": "U1",
            "text": "check this",
            "channel": "C1",
            "files": [{"url_private": "https://files.slack.com/x", "name": "photo.jpg"}],
        }
        await conn._handle_message(event)

        msg: InboundMessage = callback.call_args[0][0]
        assert msg.media_paths == ["/tmp/forge_slack_x/photo.jpg"]


# ------------------------------------------------------------------
# App mention handler
# ------------------------------------------------------------------


class TestHandleAppMention:
    @pytest.mark.asyncio
    async def test_strips_bot_mention(self):
        conn = _connected_connector(allowed_users=[])
        callback = AsyncMock()
        conn.set_message_callback(callback)

        event = {
            "user": "U1",
            "text": "<@UBOTID> @my-project do stuff",
            "channel": "C1",
        }
        await conn._handle_app_mention(event)

        msg: InboundMessage = callback.call_args[0][0]
        assert msg.project_name == "my-project"
        assert msg.text == "do stuff"

    @pytest.mark.asyncio
    async def test_ignores_subtypes(self):
        conn = _connected_connector(allowed_users=[])
        callback = AsyncMock()
        conn.set_message_callback(callback)

        event = {"subtype": "bot_message", "user": "U1", "text": "<@UBOTID> hi", "channel": "C1"}
        await conn._handle_app_mention(event)
        callback.assert_not_awaited()


# ------------------------------------------------------------------
# Block action handler
# ------------------------------------------------------------------


class TestHandleBlockAction:
    @pytest.mark.asyncio
    async def test_button_creates_command(self):
        conn = _connected_connector(allowed_users=[])
        callback = AsyncMock()
        conn.set_message_callback(callback)

        body = {
            "user": {"id": "U1", "name": "testuser"},
            "channel": {"id": "C1"},
            "actions": [
                {"action_id": "ctrl_abc123_approve", "value": "abc123:approve"},
            ],
        }
        await conn._handle_block_action(body)

        callback.assert_awaited_once()
        msg: InboundMessage = callback.call_args[0][0]
        assert msg.is_command is True
        assert msg.command_name == "approve"
        assert msg.command_args == ["abc123"]

    @pytest.mark.asyncio
    async def test_unauthorized_ignored(self):
        conn = _connected_connector(allowed_users=["U_ALLOWED"])
        callback = AsyncMock()
        conn.set_message_callback(callback)

        body = {
            "user": {"id": "U_NOT_ALLOWED", "name": "hacker"},
            "channel": {"id": "C1"},
            "actions": [
                {"action_id": "ctrl_abc123_approve", "value": "abc123:approve"},
            ],
        }
        await conn._handle_block_action(body)
        callback.assert_not_awaited()


# ------------------------------------------------------------------
# Channel operations
# ------------------------------------------------------------------


class TestChannelOps:
    @pytest.mark.asyncio
    async def test_validate_channel_success(self):
        conn = _connected_connector()
        conn._client.conversations_info = AsyncMock(return_value={"channel": {"id": "C1"}})
        assert await conn.validate_channel("C1") is True

    @pytest.mark.asyncio
    async def test_validate_channel_failure(self):
        conn = _connected_connector()
        conn._client.conversations_info = AsyncMock(side_effect=Exception("not found"))
        assert await conn.validate_channel("C_BAD") is False

    @pytest.mark.asyncio
    async def test_validate_channel_not_connected(self):
        conn = _make_connector()
        assert await conn.validate_channel("C1") is False

    @pytest.mark.asyncio
    async def test_list_channels_with_pagination(self):
        conn = _connected_connector()

        # First page returns channels + cursor, second page returns channels + no cursor
        page1 = {
            "channels": [{"id": "C1", "name": "general"}],
            "response_metadata": {"next_cursor": "abc123"},
        }
        page2 = {
            "channels": [{"id": "C2", "name": "random"}],
            "response_metadata": {"next_cursor": ""},
        }
        conn._client.conversations_list = AsyncMock(side_effect=[page1, page2])

        channels = await conn.list_channels()
        assert len(channels) == 2
        assert channels[0]["id"] == "C1"
        assert channels[1]["id"] == "C2"
        assert conn._client.conversations_list.await_count == 2

    @pytest.mark.asyncio
    async def test_get_channel_info(self):
        conn = _connected_connector()
        conn._client.conversations_info = AsyncMock(
            return_value={
                "channel": {"id": "C1", "name": "general", "is_channel": True},
            }
        )
        info = await conn.get_channel_info("C1")
        assert info["id"] == "C1"
        assert info["name"] == "general"
        assert info["type"] == "channel"

    @pytest.mark.asyncio
    async def test_health_check_connected(self):
        conn = _connected_connector()
        conn._client.auth_test = AsyncMock(
            return_value={"user_id": "UBOTID", "team": "Test Team"}
        )
        result = await conn.health_check()
        assert result["connected"] is True
        assert result["bot_user_id"] == "UBOTID"
        assert result["team"] == "Test Team"

    @pytest.mark.asyncio
    async def test_health_check_not_connected(self):
        conn = _make_connector()
        result = await conn.health_check()
        assert result["connected"] is False


# ------------------------------------------------------------------
# File downloads
# ------------------------------------------------------------------


class TestDownloadFiles:
    @pytest.mark.asyncio
    async def test_empty_files_list(self):
        conn = _connected_connector()
        result = await conn._download_files([])
        assert result == []

    @pytest.mark.asyncio
    async def test_download_success(self):
        conn = _connected_connector()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"file content"

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient", return_value=mock_client):
            files = [{"url_private_download": "https://files.slack.com/x", "name": "test.png"}]
            result = await conn._download_files(files)

        assert len(result) == 1
        assert "test.png" in result[0]
