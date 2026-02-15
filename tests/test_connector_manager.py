"""Tests for ConnectorManager routing, lifecycle, and command handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_forge.agent_manager import AgentStatus
from agent_forge.config import (
    ChannelBinding,
    ConnectorConfig,
    DefaultsConfig,
    ForgeConfig,
    ProjectConfig,
)
from agent_forge.connectors.base import ActionButton, ConnectorType, InboundMessage, OutboundMessage
from agent_forge.connectors.manager import ConnectorManager


def _make_mock_agent(
    agent_id="abc123",
    project_name="asn-api",
    status=AgentStatus.WORKING,
    task_description="Fix bug",
    worktree_path="/tmp/test",
    last_activity=1000,
):
    """Create a mock agent with configurable fields."""
    agent = MagicMock()
    agent.id = agent_id
    agent.project_name = project_name
    agent.status = status
    agent.task_description = task_description
    agent.worktree_path = worktree_path
    agent.last_activity = last_activity
    return agent


@pytest.fixture
def mock_agent_manager(tmp_git_repo):
    mgr = MagicMock()
    mgr.registry = MagicMock()
    mgr.registry.list_projects.return_value = {
        "asn-api": MagicMock(description="ASN API"),
        "edgetimer": MagicMock(description="EdgeTimer"),
    }
    # Configure max_agents via registry.config
    mgr.registry.config = MagicMock()
    mgr.registry.config.get_max_agents.return_value = 5

    # Mock agent
    mock_agent = _make_mock_agent(worktree_path=str(tmp_git_repo))

    mgr.get_agent.return_value = mock_agent
    mgr.list_agents.return_value = [mock_agent]
    mgr.get_agents_by_project.return_value = {"asn-api": [mock_agent]}
    mgr.send_message = AsyncMock(return_value=True)
    mgr.send_message_with_media = AsyncMock(return_value=True)
    mgr.send_control = AsyncMock(return_value=True)
    mgr.spawn_agent = AsyncMock(return_value=mock_agent)
    mgr.kill_agent = AsyncMock(return_value=True)
    mgr.clear_context = AsyncMock(return_value=True)
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

        # Make the agent IDLE so smart routing picks it (not spawns a new one)
        idle_agent = _make_mock_agent(agent_id="abc123", status=AgentStatus.IDLE)
        mock_agent_manager.list_agents.return_value = [idle_agent]
        mock_agent_manager.get_agent.return_value = idle_agent

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

        # Make the agent IDLE so smart routing picks it
        idle_agent = _make_mock_agent(agent_id="abc123", status=AgentStatus.IDLE)
        mock_agent_manager.list_agents.return_value = [idle_agent]
        mock_agent_manager.get_agent.return_value = idle_agent

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


class TestSmartRouting:
    """Tests for the smart load balancer that auto-spawns/assigns agents."""

    @pytest.mark.asyncio
    async def test_auto_spawns_when_no_agents(self, connector_manager, mock_agent_manager):
        """When no agents exist, auto-spawn one with the message as the task."""
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        # No agents for the project
        mock_agent_manager.list_agents.return_value = []

        new_agent = _make_mock_agent(agent_id="new123", status=AgentStatus.STARTING)
        mock_agent_manager.spawn_agent = AsyncMock(return_value=new_agent)

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            text="Fix the login bug",
        )
        await connector_manager._handle_inbound(msg)

        # Should have spawned an agent with the message as the task
        mock_agent_manager.spawn_agent.assert_called_once_with(
            "asn-api", task="Fix the login bug"
        )
        # Should NOT also call send_message (start sequence handles it)
        mock_agent_manager.send_message.assert_not_called()
        # Should report spawning to the user
        reply = mock_conn.send_message.call_args[0][0].text
        assert "Spawned" in reply
        assert "new123" in reply

    @pytest.mark.asyncio
    async def test_picks_idle_agent(self, connector_manager, mock_agent_manager):
        """When an IDLE agent exists, route to it after clearing context."""
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        idle_agent = _make_mock_agent(
            agent_id="idle01", status=AgentStatus.IDLE, last_activity=2000
        )
        mock_agent_manager.list_agents.return_value = [idle_agent]
        mock_agent_manager.get_agent.return_value = idle_agent

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            text="New task for idle agent",
        )
        await connector_manager._handle_inbound(msg)

        # Should clear context before sending
        mock_agent_manager.clear_context.assert_called_once_with("idle01")
        # Should send message normally
        mock_agent_manager.send_message.assert_called_once_with(
            "idle01", "New task for idle agent"
        )
        # Should NOT spawn a new agent
        mock_agent_manager.spawn_agent.assert_not_called()
        # Task description should be updated
        assert idle_agent.task_description == "New task for idle agent"

    @pytest.mark.asyncio
    async def test_prefers_idle_over_working(self, connector_manager, mock_agent_manager):
        """When both IDLE and WORKING agents exist, prefer the IDLE one."""
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        working_agent = _make_mock_agent(
            agent_id="work01", status=AgentStatus.WORKING, last_activity=3000
        )
        idle_agent = _make_mock_agent(
            agent_id="idle01", status=AgentStatus.IDLE, last_activity=1000
        )
        mock_agent_manager.list_agents.return_value = [working_agent, idle_agent]
        mock_agent_manager.get_agent.return_value = idle_agent

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            text="Use the idle one",
        )
        await connector_manager._handle_inbound(msg)

        mock_agent_manager.clear_context.assert_called_once_with("idle01")
        mock_agent_manager.send_message.assert_called_once_with(
            "idle01", "Use the idle one"
        )

    @pytest.mark.asyncio
    async def test_spawns_when_all_busy(self, connector_manager, mock_agent_manager):
        """When all agents are WORKING, spawn a new one if under limit."""
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        working_agent = _make_mock_agent(
            agent_id="work01", status=AgentStatus.WORKING
        )
        mock_agent_manager.list_agents.return_value = [working_agent]
        mock_agent_manager.registry.config.get_max_agents.return_value = 5

        new_agent = _make_mock_agent(agent_id="new01", status=AgentStatus.STARTING)
        mock_agent_manager.spawn_agent = AsyncMock(return_value=new_agent)

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            text="Busy agents, need a new one",
        )
        await connector_manager._handle_inbound(msg)

        mock_agent_manager.spawn_agent.assert_called_once_with(
            "asn-api", task="Busy agents, need a new one"
        )
        mock_agent_manager.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_reports_busy_at_limit(self, connector_manager, mock_agent_manager):
        """When at agent limit and all busy, report to the user."""
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        working_agent = _make_mock_agent(
            agent_id="work01", status=AgentStatus.WORKING
        )
        mock_agent_manager.list_agents.return_value = [working_agent]
        mock_agent_manager.registry.config.get_max_agents.return_value = 1

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            text="No room left",
        )
        await connector_manager._handle_inbound(msg)

        mock_agent_manager.spawn_agent.assert_not_called()
        mock_agent_manager.send_message.assert_not_called()
        reply = mock_conn.send_message.call_args[0][0].text
        assert "busy" in reply.lower()
        assert "1/1" in reply

    @pytest.mark.asyncio
    async def test_skips_waiting_input_agents(self, connector_manager, mock_agent_manager):
        """WAITING_INPUT agents should not be picked — spawn a new one instead."""
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        waiting_agent = _make_mock_agent(
            agent_id="wait01", status=AgentStatus.WAITING_INPUT
        )
        mock_agent_manager.list_agents.return_value = [waiting_agent]
        mock_agent_manager.registry.config.get_max_agents.return_value = 5

        new_agent = _make_mock_agent(agent_id="new01", status=AgentStatus.STARTING)
        mock_agent_manager.spawn_agent = AsyncMock(return_value=new_agent)

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            text="Don't inject into waiting agent",
        )
        await connector_manager._handle_inbound(msg)

        # Should NOT send to the waiting agent
        mock_agent_manager.send_message.assert_not_called()
        # Should spawn a new one
        mock_agent_manager.spawn_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignores_stopped_agents(self, connector_manager, mock_agent_manager):
        """STOPPED agents should be ignored — auto-spawn if no active ones."""
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        stopped_agent = _make_mock_agent(
            agent_id="stop01", status=AgentStatus.STOPPED
        )
        mock_agent_manager.list_agents.return_value = [stopped_agent]

        new_agent = _make_mock_agent(agent_id="new01", status=AgentStatus.STARTING)
        mock_agent_manager.spawn_agent = AsyncMock(return_value=new_agent)

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            text="Stopped agents dont count",
        )
        await connector_manager._handle_inbound(msg)

        mock_agent_manager.spawn_agent.assert_called_once()

    @pytest.mark.asyncio
    async def test_spawn_failure_reports_error(self, connector_manager, mock_agent_manager):
        """When spawn fails, report the error to the user."""
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        mock_agent_manager.list_agents.return_value = []
        mock_agent_manager.spawn_agent = AsyncMock(
            side_effect=RuntimeError("Agent limit reached")
        )

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-100123",
            sender_id="42",
            text="This will fail",
        )
        await connector_manager._handle_inbound(msg)

        reply = mock_conn.send_message.call_args[0][0].text
        assert "Failed to spawn" in reply

    @pytest.mark.asyncio
    async def test_explicit_agent_id_bypasses_smart_routing(
        self, connector_manager, mock_agent_manager
    ):
        """@project:agent_id should route directly, even if that agent is busy."""
        mock_conn = AsyncMock()
        mock_conn.send_message = AsyncMock(return_value=True)
        connector_manager.connectors["my-tg"] = mock_conn

        busy_agent = _make_mock_agent(
            agent_id="busy01", status=AgentStatus.WORKING
        )
        mock_agent_manager.get_agent.return_value = busy_agent

        msg = InboundMessage(
            connector_id="my-tg",
            channel_id="-999",
            sender_id="42",
            text="@asn-api:busy01 Direct message",
        )
        await connector_manager._handle_inbound(msg)

        # Should send directly, no smart routing
        mock_agent_manager.send_message.assert_called_once()
        mock_agent_manager.spawn_agent.assert_not_called()
        mock_agent_manager.clear_context.assert_not_called()
