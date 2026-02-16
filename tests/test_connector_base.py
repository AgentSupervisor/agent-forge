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

class TestMessageChunking:
    """Tests for BaseConnector._chunk_text and _find_split_point methods."""

    @staticmethod
    def _create_test_connector(chunk_limit: int = 50):
        """Create a concrete connector for testing with a small chunk limit."""

        class TestConnector(BaseConnector):
            connector_type = ConnectorType.TELEGRAM
            CHUNK_LIMIT = chunk_limit

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

        return TestConnector("test-id", {})

    def test_short_text_no_chunking(self):
        """Text under limit returns single item."""
        conn = self._create_test_connector(chunk_limit=100)
        text = "Short message"
        chunks = conn._chunk_text(text)
        assert len(chunks) == 1
        assert chunks[0] == "Short message"

    def test_splits_at_paragraph_breaks(self):
        """Prefer splitting at paragraph breaks (\\n\\n)."""
        conn = self._create_test_connector(chunk_limit=50)
        text = "First paragraph here.\n\nSecond paragraph starts now."
        chunks = conn._chunk_text(text)
        assert len(chunks) == 2
        assert "First paragraph" in chunks[0]
        assert "Second paragraph" in chunks[1]
        assert "[1/2]" in chunks[0]
        assert "[2/2]" in chunks[1]

    def test_splits_at_line_breaks(self):
        """Use line breaks (\\n) when no paragraph break available."""
        conn = self._create_test_connector(chunk_limit=50)
        text = "First line here with more text.\nSecond line starts."
        chunks = conn._chunk_text(text)
        assert len(chunks) == 2
        assert "First line" in chunks[0]
        assert "Second line" in chunks[1]

    def test_splits_at_sentence_ends(self):
        """Use sentence ends (. ) when no line break available."""
        conn = self._create_test_connector(chunk_limit=50)
        text = "First sentence goes here. Second sentence starts now and continues."
        chunks = conn._chunk_text(text)
        assert len(chunks) == 2
        assert "First sentence" in chunks[0]
        assert "Second sentence" in chunks[1]

    def test_hard_split(self):
        """Fall back to hard split at limit when no natural break."""
        conn = self._create_test_connector(chunk_limit=50)
        text = "A" * 100  # No natural breaks
        chunks = conn._chunk_text(text)
        assert len(chunks) == 3
        # First two chunks should be about 42 chars (50 - 8 for indicator reserve)
        assert len(chunks[0]) <= 50
        assert len(chunks[1]) <= 50
        assert "[1/3]" in chunks[0]
        assert "[3/3]" in chunks[2]

    def test_chunk_indicators_added(self):
        """Multi-chunk messages get [1/N] indicators."""
        conn = self._create_test_connector(chunk_limit=50)
        text = "Part one with enough text to split.\n\nPart two with enough text to split.\n\nPart three with enough text to split."
        chunks = conn._chunk_text(text)
        assert len(chunks) >= 2  # At least 2 chunks
        # Check that indicators are present
        assert "[1/" in chunks[0]
        assert f"/{len(chunks)}]" in chunks[-1]

    def test_empty_text(self):
        """Empty string returns single empty chunk."""
        conn = self._create_test_connector(chunk_limit=100)
        chunks = conn._chunk_text("")
        assert len(chunks) == 1
        assert chunks[0] == ""

    def test_find_split_point_paragraph_break(self):
        """_find_split_point prefers paragraph breaks."""
        text = "Some text here.\n\nMore text after paragraph break."
        pos = BaseConnector._find_split_point(text, 30)
        # Should split after the "\n\n" which is at position 17
        assert pos == 17  # position after "\n\n"
        assert text[:pos] == "Some text here.\n\n"

    def test_find_split_point_line_break(self):
        """_find_split_point uses line breaks when no paragraph break."""
        text = "First line here.\nSecond line starts now."
        pos = BaseConnector._find_split_point(text, 30)
        # Should split after the "\n" which is at position 17
        assert pos == 17  # position after "\n"
        assert text[:pos] == "First line here.\n"

    def test_find_split_point_sentence_end(self):
        """_find_split_point uses sentence ends when no line breaks."""
        text = "First sentence. Second sentence here."
        pos = BaseConnector._find_split_point(text, 30)
        # Should split after ". " which is at position 16
        assert pos == 16  # position after ". "
        assert text[:pos] == "First sentence. "

    def test_find_split_point_hard_split(self):
        """_find_split_point falls back to limit when no natural break."""
        text = "A" * 100
        pos = BaseConnector._find_split_point(text, 50)
        assert pos == 50

    def test_find_split_point_respects_quarter_threshold(self):
        """Split points before limit//4 are rejected."""
        conn = self._create_test_connector(chunk_limit=100)
        # Put a paragraph break very early (before 1/4 mark at 25)
        text = "A\n\n" + "B" * 100
        pos = BaseConnector._find_split_point(text, 100)
        # Should not use the early paragraph break at position 3
        # Should fall back to hard split at 100
        assert pos == 100

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