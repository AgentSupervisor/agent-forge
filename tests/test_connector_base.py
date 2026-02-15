"""Tests for connector base types and abstract class."""

import pytest

from agent_forge.connectors.base import (
    BaseConnector,
    ConnectorType,
    InboundMessage,
    OutboundMessage,
    extract_agent_from_text,
)


class TestConnectorType:
    def test_enum_values(self):
        assert ConnectorType.TELEGRAM == "telegram"
        assert ConnectorType.DISCORD == "discord"
        assert ConnectorType.SLACK == "slack"
        assert ConnectorType.WHATSAPP == "whatsapp"
        assert ConnectorType.SIGNAL == "signal"

    def test_enum_count(self):
        assert len(ConnectorType) == 5


class TestInboundMessage:
    def test_defaults(self):
        msg = InboundMessage(connector_id="tg1", channel_id="123", sender_id="42")
        assert msg.connector_id == "tg1"
        assert msg.channel_id == "123"
        assert msg.sender_id == "42"
        assert msg.text == ""
        assert msg.media_paths == []
        assert msg.project_name == ""
        assert msg.agent_id == ""
        assert msg.is_command is False
        assert msg.command_name == ""
        assert msg.command_args == []
        assert msg.raw is None

    def test_command_message(self):
        msg = InboundMessage(
            connector_id="tg1",
            channel_id="123",
            sender_id="42",
            is_command=True,
            command_name="status",
            command_args=["asn-api"],
        )
        assert msg.is_command is True
        assert msg.command_name == "status"
        assert msg.command_args == ["asn-api"]

    def test_media_message(self):
        msg = InboundMessage(
            connector_id="tg1",
            channel_id="123",
            sender_id="42",
            text="check this",
            media_paths=["/tmp/img.png", "/tmp/doc.pdf"],
        )
        assert len(msg.media_paths) == 2

    def test_media_paths_not_shared(self):
        """Each instance should have its own media_paths list."""
        msg1 = InboundMessage(connector_id="a", channel_id="1", sender_id="1")
        msg2 = InboundMessage(connector_id="b", channel_id="2", sender_id="2")
        msg1.media_paths.append("test.png")
        assert msg2.media_paths == []


class TestOutboundMessage:
    def test_defaults(self):
        msg = OutboundMessage(channel_id="456")
        assert msg.channel_id == "456"
        assert msg.text == ""
        assert msg.media_paths == []
        assert msg.parse_mode == ""

    def test_with_content(self):
        msg = OutboundMessage(
            channel_id="456",
            text="Hello world",
            media_paths=["/tmp/img.png"],
            parse_mode="Markdown",
        )
        assert msg.text == "Hello world"
        assert len(msg.media_paths) == 1
        assert msg.parse_mode == "Markdown"


class TestBaseConnector:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            BaseConnector("test-id", {})

    def test_concrete_subclass(self):
        """A concrete subclass with all methods implemented can be instantiated."""

        class DummyConnector(BaseConnector):
            connector_type = ConnectorType.TELEGRAM

            async def start(self):
                pass

            async def stop(self):
                pass

            async def send_message(self, message):
                return True

            async def validate_channel(self, channel_id):
                return True

            async def get_channel_info(self, channel_id):
                return {}

            async def list_channels(self):
                return []

            async def health_check(self):
                return {"connected": True}

        conn = DummyConnector("my-id", {"credentials": {"token": "abc"}})
        assert conn.connector_id == "my-id"
        assert conn.config == {"credentials": {"token": "abc"}}
        assert conn._running is False
        assert conn._message_callback is None

    def test_set_message_callback(self):
        class DummyConnector(BaseConnector):
            connector_type = ConnectorType.DISCORD

            async def start(self):
                pass

            async def stop(self):
                pass

            async def send_message(self, message):
                return True

            async def validate_channel(self, channel_id):
                return True

            async def get_channel_info(self, channel_id):
                return {}

            async def list_channels(self):
                return []

            async def health_check(self):
                return {}

        conn = DummyConnector("d1", {})

        async def callback(msg):
            pass

        conn.set_message_callback(callback)
        assert conn._message_callback is callback


class TestExtractAgentFromText:
    def test_extracts_agent_id_from_status_message(self):
        text = "Agent `a1b2c3` (asn-api): IDLE -> WORKING"
        assert extract_agent_from_text(text) == "a1b2c3"

    def test_extracts_from_sent_message(self):
        text = "Sent to `ff0011` (edgetimer)"
        assert extract_agent_from_text(text) == "ff0011"

    def test_extracts_from_waiting_input(self):
        text = "Agent `dead99` (myproject) is waiting for input"
        assert extract_agent_from_text(text) == "dead99"

    def test_returns_empty_on_no_match(self):
        assert extract_agent_from_text("No agent here") == ""

    def test_returns_empty_on_empty_string(self):
        assert extract_agent_from_text("") == ""

    def test_ignores_non_hex_ids(self):
        assert extract_agent_from_text("Agent `ghijkl` (proj)") == ""

    def test_ignores_wrong_length(self):
        assert extract_agent_from_text("Agent `abc12` (proj)") == ""
        assert extract_agent_from_text("Agent `abc1234` (proj)") == ""

    def test_first_match_wins(self):
        text = "Sent to `aaa111` then `bbb222`"
        assert extract_agent_from_text(text) == "aaa111"
