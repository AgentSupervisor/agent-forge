"""Tests for ConnectorManager routing, lifecycle, and command handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_forge.config import (
    ChannelBinding,
    ConnectorConfig,
    DefaultsConfig,
    ForgeConfig,
    ProjectConfig,
)
from agent_forge.connectors.base import ActionButton, ConnectorType, InboundMessage, OutboundMessage
from agent_forge.connectors.manager import ConnectorManager


@pytest.fixture
def mock_agent_manager(tmp_git_repo):
    mgr = MagicMock()
    mgr.registry = MagicMock()
    mgr.registry.list_projects.return_value = {
        "asn-api": MagicMock(description="ASN API"),
        "edgetimer": MagicMock(description="EdgeTimer"),
    }

    # Mock agent
    mock_agent = MagicMock()
    mock_agent.id = "abc123"
    mock_agent.project_name = "asn-api"
    mock_agent.status.value = "working"
    mock_agent.task_description = "Fix bug"
    mock_agent.worktree_path = str(tmp_git_repo)
    mock_agent.last_activity = 1000

    mgr.get_agent.return_value = mock_agent
    mgr.list_agents.return_value = [mock_agent]
    mgr.get_agents_by_project.return_value = {"asn-api": [mock_agent]}
    mgr.send_message = AsyncMock(return_value=True)
    mgr.send_message_with_media = AsyncMock(return_value=True)
    mgr.send_control = AsyncMock(return_value=True)
    mgr.spawn_agent = AsyncMock(return_value=mock_agent)
    mgr.kill_agent = AsyncMock(return_value=True)
    return mgr


@pytest.fixture
def config_with_connectors(tmp_git_repo):
    return ForgeConfig(
        connectors={
            "my-tg": ConnectorConfig(
                type="telegram",
                enabled=True,
                credentials={"bot_token": "fake-token"},
                settings={"allowed_users": []},
            ),
        },
        projects={
            "asn-api": ProjectConfig(
                path=str(tmp_git_repo),
                description="ASN API",
                channels=[
                    ChannelBinding(
                        connector_id="my-tg",
                        channel_id="-100123",
                        channel_name="ASN Dev",
                        inbound=True,
                        outbound=True,
                    ),
                ],
            ),
            "edgetimer": ProjectConfig(
                path=str(tmp_git_repo),
                description="EdgeTimer",
                channels=[],
            ),
        },
    )


@pytest.fixture
def connector_manager(mock_agent_manager, config_with_connectors):
    """ConnectorManager with mocked agent_manager and no real connectors started."""
    cm = ConnectorManager(mock_agent_manager, MagicMock(), config_with_connectors)
    cm._rebuild_channel_map()
    return cm


class TestChannelMap:
    def test_builds_map_from_bindings(self, connector_manager):
        assert ("my-tg", "-100123") in connector_manager._channel_map

    def test_inbound_only(self, connector_manager, config_with_connectors):
        """Channels with inbound=False should not appear in the map."""
        config_with_connectors.projects["asn-api"].channels[0].inbound = False
        connector_manager._rebuild_channel_map()
        assert ("my-tg", "-100123") not in connector_manager._channel_map


class TestInboundRouting:
    @pytest.mark.asyncio
    async def test_routes_via_channel_binding(self, connector_manager, mock_agent_manager):
        """Messages from a bound channel auto-route to the project."""
        # Add a mock connector so replies work
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            text="Hello agent",
        )
        await connector_manager._handle_inbound(msg)
        mock_agent_manager.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_routes_via_at_prefix(self, connector_manager, mock_agent_manager):
        """Messages with @project prefix route correctly even without binding."""
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-999",  # unbound channel
            sender_id="42",
            text="@asn-api Fix the login bug",
        )
        await connector_manager._handle_inbound(msg)
        mock_agent_manager.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_routes_with_agent_id(self, connector_manager, mock_agent_manager):
        """@project:agent_id syntax routes to a specific agent."""
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-999",
            sender_id="42",
            text="@asn-api:abc123 Check this out",
        )
        await connector_manager._handle_inbound(msg)
        mock_agent_manager.get_agent.assert_called_with("abc123")

    @pytest.mark.asyncio
    async def test_unknown_project_replies_error(self, connector_manager):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-999",
            sender_id="42",
            text="@nonexistent Hello",
        )
        await connector_manager._handle_inbound(msg)
        mock_conn.send_message.assert_called_once()
        reply_text = mock_conn.send_message.call_args[0][0].text
        assert "Unknown project" in reply_text

    @pytest.mark.asyncio
    async def test_no_binding_no_prefix_replies_usage(self, connector_manager):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-999",
            sender_id="42",
            text="Hello without prefix",
        )
        await connector_manager._handle_inbound(msg)
        mock_conn.send_message.assert_called_once()
        reply_text = mock_conn.send_message.call_args[0][0].text
        assert "Usage" in reply_text


class TestMediaRouting:
    @pytest.mark.asyncio
    async def test_media_message_stages_and_sends(
        self, connector_manager, mock_agent_manager, tmp_path
    ):
        """Media paths are processed, staged, and sent with media context."""
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        # Create a temp file to simulate a connector download
        temp_file = tmp_path / "forge_media_123" / "photo.png"
        temp_file.parent.mkdir(parents=True)
        temp_file.write_bytes(b"fake image")

        # Set up media handler mock
        mock_media = MagicMock()
        mock_media.process_and_stage = AsyncMock(
            return_value=([".media/1000_photo.png"], "image")
        )
        mock_media.build_media_reference = MagicMock(
            return_value="I've placed design mockups/images at: .media/1000_photo.png. Please analyze them."
        )
        connector_manager.media_handler = mock_media

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            text="Check this screenshot",
            media_paths=[str(temp_file)],
        )
        await connector_manager._handle_inbound(msg)

        # Verify process_and_stage was called with correct args
        mock_media.process_and_stage.assert_called_once_with(
            source_path=str(temp_file),
            agent_worktree=mock_agent_manager.get_agent.return_value.worktree_path,
        )
        # Verify build_media_reference was called
        mock_media.build_media_reference.assert_called_once()
        # Verify send_message_with_media was called with media_context
        mock_agent_manager.send_message_with_media.assert_called_once()
        call_kwargs = mock_agent_manager.send_message_with_media.call_args
        assert "media_context" in call_kwargs.kwargs or len(call_kwargs.args) >= 4

    @pytest.mark.asyncio
    async def test_media_message_cleans_up_temp_files(
        self, connector_manager, mock_agent_manager, tmp_path
    ):
        """Temp files from connectors are cleaned up after staging."""
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        temp_file = tmp_path / "forge_media_456" / "doc.pdf"
        temp_file.parent.mkdir(parents=True)
        temp_file.write_bytes(b"fake pdf")

        mock_media = MagicMock()
        mock_media.process_and_stage = AsyncMock(
            return_value=([".media/1000_doc.pdf"], "document")
        )
        mock_media.build_media_reference = MagicMock(return_value="doc ref")
        connector_manager.media_handler = mock_media

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            text="Here is the doc",
            media_paths=[str(temp_file)],
        )
        await connector_manager._handle_inbound(msg)

        # Temp file should be cleaned up
        assert not temp_file.exists()

    @pytest.mark.asyncio
    async def test_media_failure_still_cleans_up(
        self, connector_manager, mock_agent_manager, tmp_path
    ):
        """Temp files are cleaned up even when processing fails."""
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        temp_file = tmp_path / "forge_media_789" / "bad.png"
        temp_file.parent.mkdir(parents=True)
        temp_file.write_bytes(b"bad data")

        mock_media = MagicMock()
        mock_media.process_and_stage = AsyncMock(side_effect=RuntimeError("ffmpeg failed"))
        connector_manager.media_handler = mock_media

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            text="This will fail",
            media_paths=[str(temp_file)],
        )
        await connector_manager._handle_inbound(msg)

        # Temp file should still be cleaned up
        assert not temp_file.exists()
        # Error reply should have been sent
        reply_text = mock_conn.send_message.call_args[0][0].text
        assert "Failed" in reply_text


class TestOutbound:
    @pytest.mark.asyncio
    async def test_sends_to_outbound_channels(self, connector_manager):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        await connector_manager.send_to_project_channels("asn-api", "Agent stopped")
        mock_conn.send_message.assert_called_once()
        sent_msg = mock_conn.send_message.call_args[0][0]
        assert sent_msg.channel_id == "-100123"
        assert sent_msg.text == "Agent stopped"

    @pytest.mark.asyncio
    async def test_respects_outbound_false(self, connector_manager, config_with_connectors):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        config_with_connectors.projects["asn-api"].channels[0].outbound = False

        await connector_manager.send_to_project_channels("asn-api", "Agent stopped")
        mock_conn.send_message.assert_not_called()


class TestCommands:
    @pytest.mark.asyncio
    async def test_status_command(self, connector_manager, mock_agent_manager):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            is_command=True,
            command_name="status",
        )
        await connector_manager._handle_inbound(msg)
        mock_conn.send_message.assert_called_once()
        reply = mock_conn.send_message.call_args[0][0].text
        assert "asn-api" in reply

    @pytest.mark.asyncio
    async def test_projects_command(self, connector_manager):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            is_command=True,
            command_name="projects",
        )
        await connector_manager._handle_inbound(msg)
        mock_conn.send_message.assert_called_once()
        reply = mock_conn.send_message.call_args[0][0].text
        assert "asn-api" in reply
        assert "edgetimer" in reply

    @pytest.mark.asyncio
    async def test_spawn_command(self, connector_manager, mock_agent_manager):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            is_command=True,
            command_name="spawn",
            command_args=["asn-api", "fix", "login"],
        )
        await connector_manager._handle_inbound(msg)
        mock_agent_manager.spawn_agent.assert_called_once_with(
            "asn-api", task="fix login"
        )

    @pytest.mark.asyncio
    async def test_kill_command(self, connector_manager, mock_agent_manager):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            is_command=True,
            command_name="kill",
            command_args=["abc123"],
        )
        await connector_manager._handle_inbound(msg)
        mock_agent_manager.kill_agent.assert_called_once_with("abc123")

    @pytest.mark.asyncio
    async def test_unknown_command(self, connector_manager):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            is_command=True,
            command_name="foobar",
        )
        await connector_manager._handle_inbound(msg)
        reply = mock_conn.send_message.call_args[0][0].text
        assert "Unknown command" in reply


class TestParseTarget:
    def test_simple_project(self):
        p, a, t = ConnectorManager._parse_target("@myproject Hello world")
        assert p == "myproject"
        assert a == ""
        assert t == "Hello world"

    def test_project_with_agent(self):
        p, a, t = ConnectorManager._parse_target("@myproject:abc123 Do something")
        assert p == "myproject"
        assert a == "abc123"
        assert t == "Do something"

    def test_no_match(self):
        p, a, t = ConnectorManager._parse_target("Just a regular message")
        assert p == ""
        assert a == ""
        assert t == ""

    def test_multiline_message(self):
        p, a, t = ConnectorManager._parse_target("@proj Line 1\nLine 2\nLine 3")
        assert p == "proj"
        assert "Line 1" in t
        assert "Line 3" in t


class TestControlCommands:
    @pytest.mark.asyncio
    async def test_approve_with_explicit_agent_id(self, connector_manager, mock_agent_manager):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            is_command=True,
            command_name="approve",
            command_args=["abc123"],
        )
        await connector_manager._handle_inbound(msg)
        mock_agent_manager.send_control.assert_called_once_with("abc123", "approve")

    @pytest.mark.asyncio
    async def test_reject_with_sticky_context(self, connector_manager, mock_agent_manager):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        # Set sticky context first
        connector_manager._set_context("my-tg", "-100123", "abc123")

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            is_command=True,
            command_name="reject",
        )
        await connector_manager._handle_inbound(msg)
        mock_agent_manager.send_control.assert_called_once_with("abc123", "reject")

    @pytest.mark.asyncio
    async def test_interrupt_command(self, connector_manager, mock_agent_manager):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            is_command=True,
            command_name="interrupt",
            command_args=["abc123"],
        )
        await connector_manager._handle_inbound(msg)
        mock_agent_manager.send_control.assert_called_once_with("abc123", "interrupt")

    @pytest.mark.asyncio
    async def test_approve_all_command(self, connector_manager, mock_agent_manager):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            is_command=True,
            command_name="approve_all",
            command_args=["abc123"],
        )
        await connector_manager._handle_inbound(msg)
        mock_agent_manager.send_control.assert_called_once_with("abc123", "approve_all")

    @pytest.mark.asyncio
    async def test_control_no_context_replies_usage(self, connector_manager, mock_agent_manager):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        # No args, no context, unbound channel
        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-999",
            sender_id="42",
            is_command=True,
            command_name="approve",
        )
        await connector_manager._handle_inbound(msg)
        mock_agent_manager.send_control.assert_not_called()
        reply = mock_conn.send_message.call_args[0][0].text
        assert "Usage" in reply

    @pytest.mark.asyncio
    async def test_control_agent_not_found(self, connector_manager, mock_agent_manager):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        mock_agent_manager.get_agent.return_value = None

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            is_command=True,
            command_name="approve",
            command_args=["nonexistent"],
        )
        await connector_manager._handle_inbound(msg)
        mock_agent_manager.send_control.assert_not_called()
        reply = mock_conn.send_message.call_args[0][0].text
        assert "not found" in reply

    @pytest.mark.asyncio
    async def test_control_with_single_agent_shortcut(
        self, connector_manager, mock_agent_manager
    ):
        """When channel is bound to one project with one agent, resolve automatically."""
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        # No args, no sticky context, but bound channel with single agent
        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            is_command=True,
            command_name="approve",
        )
        await connector_manager._handle_inbound(msg)
        mock_agent_manager.send_control.assert_called_once_with("abc123", "approve")


class TestStickyContext:
    def test_set_and_get_context(self, connector_manager, mock_agent_manager):
        connector_manager._set_context("my-tg", "-100123", "abc123")
        assert connector_manager._get_context("my-tg", "-100123") == "abc123"

    def test_get_context_empty(self, connector_manager):
        assert connector_manager._get_context("my-tg", "-999") == ""

    def test_stale_agent_cleared(self, connector_manager, mock_agent_manager):
        connector_manager._set_context("my-tg", "-100123", "dead_agent")
        mock_agent_manager.get_agent.return_value = None

        result = connector_manager._get_context("my-tg", "-100123")
        assert result == ""
        assert ("my-tg", "-100123") not in connector_manager._context

    @pytest.mark.asyncio
    async def test_context_set_after_message_delivery(
        self, connector_manager, mock_agent_manager
    ):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            text="Hello agent",
        )
        await connector_manager._handle_inbound(msg)
        assert connector_manager._context[("my-tg", "-100123")] == "abc123"

    @pytest.mark.asyncio
    async def test_context_set_after_spawn(self, connector_manager, mock_agent_manager):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            is_command=True,
            command_name="spawn",
            command_args=["asn-api", "fix", "bug"],
        )
        await connector_manager._handle_inbound(msg)
        assert connector_manager._context[("my-tg", "-100123")] == "abc123"

    @pytest.mark.asyncio
    async def test_bare_text_routes_via_sticky_context(
        self, connector_manager, mock_agent_manager
    ):
        """Bare text (no @prefix) on unbound channel uses sticky context."""
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        # Set sticky context for an unbound channel
        connector_manager._set_context("my-tg", "-999", "abc123")

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-999",
            sender_id="42",
            text="Follow up message without prefix",
        )
        await connector_manager._handle_inbound(msg)
        mock_agent_manager.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_set_after_control_command(
        self, connector_manager, mock_agent_manager
    ):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            is_command=True,
            command_name="approve",
            command_args=["abc123"],
        )
        await connector_manager._handle_inbound(msg)
        assert connector_manager._context[("my-tg", "-100123")] == "abc123"


class TestRichOutbound:
    @pytest.mark.asyncio
    async def test_send_to_project_channels_rich(self, connector_manager):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        buttons = [
            ActionButton(label="Approve", action="approve", agent_id="abc123"),
            ActionButton(label="Reject", action="reject", agent_id="abc123"),
        ]
        extra = {"action_buttons": buttons, "notification_type": "waiting_input"}

        await connector_manager.send_to_project_channels_rich(
            "asn-api", "Agent waiting", extra=extra,
        )

        mock_conn.send_message.assert_called_once()
        sent_msg = mock_conn.send_message.call_args[0][0]
        assert sent_msg.channel_id == "-100123"
        assert sent_msg.text == "Agent waiting"
        assert sent_msg.extra["action_buttons"] == buttons
        assert sent_msg.extra["notification_type"] == "waiting_input"

    @pytest.mark.asyncio
    async def test_rich_outbound_respects_outbound_false(
        self, connector_manager, config_with_connectors
    ):
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        config_with_connectors.projects["asn-api"].channels[0].outbound = False

        await connector_manager.send_to_project_channels_rich(
            "asn-api", "Agent waiting", extra={"action_buttons": []},
        )
        mock_conn.send_message.assert_not_called()


class TestGetStatus:
    def test_returns_all_connectors(self, connector_manager, config_with_connectors):
        status = connector_manager.get_status()
        assert "my-tg" in status
        assert status["my-tg"]["type"] == "telegram"
        assert status["my-tg"]["enabled"] is True
        assert status["my-tg"]["running"] is False  # no real connector started
