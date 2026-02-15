"""Tests for WhatsAppConnector — sidecar management, messaging, polling."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest

from agent_forge.connectors.whatsapp import WhatsAppConnector
from agent_forge.connectors.base import ActionButton, InboundMessage, OutboundMessage


@pytest.fixture
def wa_config():
    return {
        "credentials": {"phone_number": "+1234567890"},
        "settings": {
            "sidecar_port": 3100,
            "allowed_users": [],
            "known_chats": {},
        },
    }


@pytest.fixture
def wa_config_restricted():
    return {
        "credentials": {"phone_number": "+1234567890"},
        "settings": {
            "sidecar_port": 3100,
            "allowed_users": ["1111111111@s.whatsapp.net", "2222222222@s.whatsapp.net"],
            "known_chats": {},
        },
    }


@pytest.fixture
def connector(wa_config):
    return WhatsAppConnector("test-wa", wa_config)


@pytest.fixture
def connector_restricted(wa_config_restricted):
    return WhatsAppConnector("test-wa", wa_config_restricted)


# ------------------------------------------------------------------
# Initialization
# ------------------------------------------------------------------


class TestInit:
    def test_constructor_defaults(self, connector):
        assert connector.phone_number == "+1234567890"
        assert connector.sidecar_port == 3100
        assert connector.allowed_users == []
        assert connector._recent_chats == {}
        assert connector._session_dir == Path.home() / ".agent-forge" / "whatsapp_sessions" / "1234567890"

    def test_constructor_with_known_chats(self):
        config = {
            "credentials": {"phone_number": "+1234567890"},
            "settings": {
                "sidecar_port": 3100,
                "allowed_users": [],
                "known_chats": {
                    "1111111111@s.whatsapp.net": "John Doe",
                    "2222222222@s.whatsapp.net": "Jane Smith",
                },
            },
        }
        connector = WhatsAppConnector("test-wa", config)
        assert len(connector._recent_chats) == 2
        assert connector._recent_chats["1111111111@s.whatsapp.net"] == "John Doe"

    def test_session_dir_path(self, connector):
        expected_path = Path.home() / ".agent-forge" / "whatsapp_sessions" / "1234567890"
        assert connector._session_dir == expected_path


# ------------------------------------------------------------------
# JID Conversion
# ------------------------------------------------------------------


class TestJidConversion:
    def test_jid_to_channel_id_personal(self):
        result = WhatsAppConnector._jid_to_channel_id("1234567890@s.whatsapp.net")
        assert result == "1234567890"

    def test_jid_to_channel_id_group(self):
        result = WhatsAppConnector._jid_to_channel_id("120363xxx@g.us")
        assert result == "120363xxx"

    def test_channel_id_to_jid_personal(self):
        result = WhatsAppConnector._channel_id_to_jid("1234567890")
        assert result == "1234567890@s.whatsapp.net"

    def test_channel_id_to_jid_group(self):
        result = WhatsAppConnector._channel_id_to_jid("120363-xxx")
        assert result == "120363-xxx@g.us"

    def test_channel_id_to_jid_already_jid(self):
        result = WhatsAppConnector._channel_id_to_jid("1234567890@s.whatsapp.net")
        assert result == "1234567890@s.whatsapp.net"


# ------------------------------------------------------------------
# Authorization
# ------------------------------------------------------------------


class TestAuthorization:
    def test_allow_all_when_empty(self, connector):
        assert connector._check_authorized("any@s.whatsapp.net") is True

    def test_allowed_user(self, connector_restricted):
        assert connector_restricted._check_authorized("1111111111@s.whatsapp.net") is True

    def test_denied_user(self, connector_restricted):
        assert connector_restricted._check_authorized("9999999999@s.whatsapp.net") is False


# ------------------------------------------------------------------
# Routing parsing
# ------------------------------------------------------------------


class TestParseRouting:
    def test_project_prefix(self):
        result = WhatsAppConnector._parse_routing("@my-project fix bug")
        assert result == ("my-project", "")

    def test_project_agent_prefix(self):
        result = WhatsAppConnector._parse_routing("@proj:abc123 do it")
        assert result == ("proj", "abc123")

    def test_no_prefix(self):
        result = WhatsAppConnector._parse_routing("just text")
        assert result == ("", "")

    def test_at_no_space(self):
        result = WhatsAppConnector._parse_routing("@project")
        assert result == ("", "")


# ------------------------------------------------------------------
# Chat tracking
# ------------------------------------------------------------------


class TestChatTracking:
    def test_track_new_chat(self, connector):
        connector._track_chat("1234567890@s.whatsapp.net", "John Doe", "private")
        assert "1234567890" in connector._recent_chats
        assert connector._recent_chats["1234567890"]["name"] == "John Doe"
        assert connector._recent_chats["1234567890"]["type"] == "private"

    def test_track_updates_existing(self, connector):
        connector._track_chat("1234567890@s.whatsapp.net", "John", "private")
        connector._track_chat("1234567890@s.whatsapp.net", "John Doe", "private")
        assert connector._recent_chats["1234567890"]["name"] == "John Doe"

    def test_get_known_chats(self, connector):
        connector._track_chat("1111111111@s.whatsapp.net", "John", "private")
        connector._track_chat("2222222222@s.whatsapp.net", "Jane", "private")
        chats = connector.get_known_chats()
        assert isinstance(chats, dict)
        assert len(chats) == 2
        assert chats["1111111111"]["name"] == "John"

    @pytest.mark.asyncio
    async def test_list_channels(self, connector):
        connector._track_chat("1111111111@s.whatsapp.net", "John Doe", "private")
        connector._track_chat("2222222222@s.whatsapp.net", "Jane Smith", "private")
        channels = await connector.list_channels()
        assert len(channels) == 2
        names = {ch["name"] for ch in channels}
        assert "John Doe" in names
        assert "Jane Smith" in names


# ------------------------------------------------------------------
# Health check
# ------------------------------------------------------------------


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_health_connected(self, connector):
        mock_response = MagicMock()
        mock_response.json.return_value = {"connected": True, "phone": "+1234567890"}

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        connector._http_client = mock_client

        result = await connector.health_check()
        assert result["connected"] is True
        mock_client.get.assert_awaited_once_with("/health", timeout=2.0)

    @pytest.mark.asyncio
    async def test_health_disconnected(self, connector):
        mock_response = MagicMock()
        mock_response.json.return_value = {"connected": False, "qr_available": True}

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        connector._http_client = mock_client

        result = await connector.health_check()
        assert result["connected"] is False
        assert result["qr_available"] is True

    @pytest.mark.asyncio
    async def test_health_unreachable(self, connector):
        import httpx
        mock_client = MagicMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))
        connector._http_client = mock_client

        result = await connector.health_check()
        assert result["connected"] is False
        assert "details" in result

    @pytest.mark.asyncio
    async def test_health_no_client(self, connector):
        connector._http_client = None
        result = await connector.health_check()
        assert result["connected"] is False
        assert result["details"] == "HTTP client not initialized"


# ------------------------------------------------------------------
# Send message
# ------------------------------------------------------------------


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_send_text(self, connector):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        connector._http_client = mock_client

        message = OutboundMessage(channel_id="1234567890", text="hello")
        result = await connector.send_message(message)

        assert result is True
        mock_client.post.assert_awaited_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "/send"
        assert call_args[1]["json"]["jid"] == "1234567890@s.whatsapp.net"
        assert call_args[1]["json"]["text"] == "hello"

    @pytest.mark.asyncio
    async def test_send_with_buttons(self, connector):
        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        connector._http_client = mock_client

        button = ActionButton("Approve", "approve", "abc123")
        message = OutboundMessage(
            channel_id="1234567890",
            text="Confirm action?",
            extra={"action_buttons": [button]}
        )
        result = await connector.send_message(message)

        assert result is True
        call_args = mock_client.post.call_args
        assert "buttons" in call_args[1]["json"]

    @pytest.mark.asyncio
    async def test_send_media(self, connector):
        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        connector._http_client = mock_client

        message = OutboundMessage(
            channel_id="1234567890",
            text="Check this out",
            media_paths=["/tmp/photo.jpg"]
        )
        result = await connector.send_message(message)

        assert result is True
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "http://127.0.0.1:3100/send_media"

    @pytest.mark.asyncio
    async def test_send_failure(self, connector):
        import httpx
        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=httpx.HTTPError("Request failed"))
        connector._http_client = mock_client

        message = OutboundMessage(channel_id="1234567890", text="hello")
        result = await connector.send_message(message)

        assert result is False

    @pytest.mark.asyncio
    async def test_send_no_client(self, connector):
        connector._http_client = None
        message = OutboundMessage(channel_id="1234567890", text="hello")
        result = await connector.send_message(message)
        assert result is False


# ------------------------------------------------------------------
# Process message
# ------------------------------------------------------------------


class TestProcessMessage:
    @pytest.mark.asyncio
    async def test_text_message(self, connector):
        connector._message_callback = AsyncMock()

        data = {
            "from": "1234567890@s.whatsapp.net",
            "pushName": "John",
            "text": "hello",
            "chatJid": "1234567890@s.whatsapp.net",
            "isGroup": False,
            "timestamp": 1700000000,
        }

        await connector._process_message(data)

        connector._message_callback.assert_awaited_once()
        msg = connector._message_callback.call_args[0][0]
        assert isinstance(msg, InboundMessage)
        assert msg.connector_id == "test-wa"
        assert msg.channel_id == "1234567890"
        assert msg.sender_id == "1234567890"
        assert msg.sender_name == "John"
        assert msg.text == "hello"

    @pytest.mark.asyncio
    async def test_command_message(self, connector):
        connector._message_callback = AsyncMock()

        data = {
            "from": "1234567890@s.whatsapp.net",
            "pushName": "John",
            "text": "/status",
            "chatJid": "1234567890@s.whatsapp.net",
            "isGroup": False,
            "timestamp": 1700000000,
        }

        await connector._process_message(data)

        msg = connector._message_callback.call_args[0][0]
        assert msg.is_command is True
        assert msg.command_name == "status"

    @pytest.mark.asyncio
    async def test_command_with_args(self, connector):
        connector._message_callback = AsyncMock()

        data = {
            "from": "1234567890@s.whatsapp.net",
            "pushName": "John",
            "text": "/spawn myproject fix bug",
            "chatJid": "1234567890@s.whatsapp.net",
            "isGroup": False,
            "timestamp": 1700000000,
        }

        await connector._process_message(data)

        msg = connector._message_callback.call_args[0][0]
        assert msg.command_name == "spawn"
        assert msg.command_args == ["myproject", "fix", "bug"]

    @pytest.mark.asyncio
    async def test_routing_prefix(self, connector):
        connector._message_callback = AsyncMock()

        data = {
            "from": "1234567890@s.whatsapp.net",
            "pushName": "John",
            "text": "@myproject do the thing",
            "chatJid": "1234567890@s.whatsapp.net",
            "isGroup": False,
            "timestamp": 1700000000,
        }

        await connector._process_message(data)

        msg = connector._message_callback.call_args[0][0]
        assert msg.project_name == "myproject"
        assert msg.text == "do the thing"

    @pytest.mark.asyncio
    async def test_group_message(self, connector):
        connector._message_callback = AsyncMock()

        data = {
            "from": "1234567890@s.whatsapp.net",
            "pushName": "John",
            "text": "hello group",
            "chatJid": "120363-xxx@g.us",
            "isGroup": True,
            "timestamp": 1700000000,
        }

        await connector._process_message(data)

        msg = connector._message_callback.call_args[0][0]
        assert msg.channel_id == "120363-xxx"

    @pytest.mark.asyncio
    async def test_button_response(self, connector):
        connector._message_callback = AsyncMock()

        data = {
            "from": "1234567890@s.whatsapp.net",
            "pushName": "John",
            "text": "",
            "chatJid": "1234567890@s.whatsapp.net",
            "isGroup": False,
            "timestamp": 1700000000,
            "selectedButtonId": "ctrl:abc123:approve",
        }

        await connector._process_message(data)

        msg = connector._message_callback.call_args[0][0]
        assert msg.is_command is True
        assert msg.command_name == "approve"
        assert msg.command_args == ["abc123"]

    @pytest.mark.asyncio
    async def test_unauthorized_skipped(self, connector_restricted):
        connector_restricted._message_callback = AsyncMock()

        data = {
            "from": "9999999999@s.whatsapp.net",
            "pushName": "Unauthorized",
            "text": "hello",
            "chatJid": "9999999999@s.whatsapp.net",
            "isGroup": False,
            "timestamp": 1700000000,
        }

        await connector_restricted._process_message(data)

        connector_restricted._message_callback.assert_not_awaited()


# ------------------------------------------------------------------
# Validate channel
# ------------------------------------------------------------------


class TestValidateChannel:
    @pytest.mark.asyncio
    async def test_valid(self, connector):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": "1234567890@s.whatsapp.net", "name": "John"}

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        connector._http_client = mock_client

        result = await connector.validate_channel("1234567890")
        assert result is True

    @pytest.mark.asyncio
    async def test_invalid(self, connector):
        mock_response = MagicMock()
        mock_response.status_code = 404

        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        connector._http_client = mock_client

        result = await connector.validate_channel("invalid")
        assert result is False


# ------------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_spawns_sidecar(self, connector):
        with patch("asyncio.create_subprocess_exec") as mock_subprocess, \
             patch("httpx.AsyncClient") as mock_httpx, \
             patch("asyncio.create_task") as mock_create_task:

            mock_process = MagicMock()
            mock_process.returncode = None
            mock_subprocess.return_value = mock_process

            await connector.start()

            assert connector._running is True
            mock_subprocess.assert_awaited_once()
            mock_httpx.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_cleanup(self, connector):
        # Setup mocks
        connector._poll_task = AsyncMock()
        connector._poll_task.cancel = MagicMock()
        connector._poll_task.__await__ = MagicMock(return_value=iter([]))

        mock_process = MagicMock()
        mock_process.returncode = None
        mock_process.terminate = MagicMock()
        mock_process.wait = AsyncMock()
        connector._sidecar_process = mock_process

        mock_client = AsyncMock()
        mock_client.post = AsyncMock()
        mock_client.aclose = AsyncMock()
        connector._http_client = mock_client
        connector._running = True

        await connector.stop()

        assert connector._running is False
        mock_client.aclose.assert_awaited_once()
