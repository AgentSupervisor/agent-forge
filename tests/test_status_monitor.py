"""Tests for StatusMonitor.detect_status, polling logic, and prompt extraction."""

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_forge.agent_manager import Agent, AgentStatus
from agent_forge.config import DefaultsConfig, ForgeConfig, ResponseRelayConfig, SummaryConfig
from agent_forge.connectors.base import ActionButton
from agent_forge.status_monitor import StatusMonitor


class TestDetectStatus:
    """Test detect_status with various terminal outputs."""

    def test_allow_prompt(self):
        output = "Some output\nAllow this action? (y/n)"
        assert StatusMonitor.detect_status(output, "") == AgentStatus.WAITING_INPUT

    def test_y_n_prompt(self):
        output = "Proceed? Y/n"
        assert StatusMonitor.detect_status(output, "") == AgentStatus.WAITING_INPUT

    def test_yes_no_prompt(self):
        output = "Do you want to continue? yes/no"
        assert StatusMonitor.detect_status(output, "") == AgentStatus.WAITING_INPUT

    def test_do_you_want_prompt(self):
        output = "Do you want to install dependencies?"
        assert StatusMonitor.detect_status(output, "") == AgentStatus.WAITING_INPUT

    def test_bracket_yn_prompt(self):
        output = "Overwrite file? [y/n]"
        assert StatusMonitor.detect_status(output, "") == AgentStatus.WAITING_INPUT

    def test_error_keyword(self):
        output = "Compiling...\nError: cannot find module 'foo'"
        assert StatusMonitor.detect_status(output, "") == AgentStatus.ERROR

    def test_fatal_keyword(self):
        output = "fatal: not a git repository"
        assert StatusMonitor.detect_status(output, "") == AgentStatus.ERROR

    def test_failed_keyword(self):
        output = "Build FAILED with 3 errors"
        assert StatusMonitor.detect_status(output, "") == AgentStatus.ERROR

    def test_idle_prompt_angle_bracket(self):
        output = "claude >"
        assert StatusMonitor.detect_status(output, "") == AgentStatus.IDLE

    def test_idle_prompt_chevron(self):
        output = "some prompt ❯"
        assert StatusMonitor.detect_status(output, "") == AgentStatus.IDLE

    def test_idle_prompt_dollar(self):
        output = "user@host:~$ "
        assert StatusMonitor.detect_status(output, "") == AgentStatus.IDLE

    def test_output_changed_means_working(self):
        previous = "line 1\nline 2"
        current = "line 1\nline 2\nline 3"
        assert StatusMonitor.detect_status(current, previous) == AgentStatus.WORKING

    def test_output_unchanged_means_idle(self):
        output = "some regular output without prompts"
        assert StatusMonitor.detect_status(output, output) == AgentStatus.IDLE

    def test_empty_output(self):
        assert StatusMonitor.detect_status("", "") == AgentStatus.IDLE

    def test_input_prompt_takes_priority_over_error(self):
        """Input prompts should win when both patterns match."""
        output = "Error: file not found\nDo you want to retry? Y/n"
        assert StatusMonitor.detect_status(output, "") == AgentStatus.WAITING_INPUT

    def test_error_takes_priority_over_idle_prompt(self):
        """Error should win over idle prompt characters."""
        output = "Error: something broke\n>"
        assert StatusMonitor.detect_status(output, "") == AgentStatus.ERROR


class TestStatusMonitorPoll:
    """Test the polling loop integration."""

    @pytest.fixture
    def agent(self):
        return Agent(
            id="abc123",
            project_name="test-project",
            session_name="forge__test-project__abc123",
            worktree_path="/tmp/worktree",
            branch_name="agent/abc123/task",
            status=AgentStatus.WORKING,
            created_at=datetime.now(),
            last_activity=datetime.now(),
            last_output="previous output",
            task_description="fix a bug",
        )

    @pytest.fixture
    def monitor(self, agent):
        manager = MagicMock()
        manager.list_agents.return_value = [agent]
        ws = MagicMock()
        ws.broadcast_agent_update = AsyncMock()
        ws.broadcast_terminal_output = AsyncMock()
        return StatusMonitor(agent_manager=manager, ws_manager=ws)

    @pytest.mark.asyncio
    async def test_poll_detects_session_gone(self, monitor, agent):
        """When tmux session disappears, agent status becomes STOPPED."""
        with (
            patch("agent_forge.tmux_utils.capture_pane", return_value=""),
            patch("agent_forge.tmux_utils.session_exists", return_value=False),
        ):
            await monitor._poll()

        assert agent.status == AgentStatus.STOPPED

    @pytest.mark.asyncio
    async def test_poll_detects_status_change(self, monitor, agent):
        """Poll updates agent status based on output."""
        new_output = "Proceed? Y/n"
        with (
            patch("agent_forge.tmux_utils.capture_pane", return_value=new_output),
            patch("agent_forge.tmux_utils.session_exists", return_value=True),
        ):
            await monitor._poll()

        assert agent.status == AgentStatus.WAITING_INPUT
        assert agent.last_output == new_output

    @pytest.mark.asyncio
    async def test_poll_broadcasts_updates(self, monitor, agent):
        """Poll should broadcast both agent update and terminal output."""
        output = "working on stuff..."
        with (
            patch("agent_forge.tmux_utils.capture_pane", return_value=output),
            patch("agent_forge.tmux_utils.session_exists", return_value=True),
        ):
            await monitor._poll()

        monitor.ws_manager.broadcast_agent_update.assert_called_once_with(agent)
        monitor.ws_manager.broadcast_terminal_output.assert_called_once_with(
            agent.id, output,
        )

    @pytest.mark.asyncio
    async def test_poll_skips_stopped_agents(self, monitor, agent):
        """Stopped agents should not be polled."""
        agent.status = AgentStatus.STOPPED
        with (
            patch("agent_forge.tmux_utils.capture_pane") as mock_capture,
            patch("agent_forge.tmux_utils.session_exists") as mock_exists,
        ):
            await monitor._poll()

        mock_capture.assert_not_called()
        mock_exists.assert_not_called()

    @pytest.mark.asyncio
    async def test_poll_logs_event_on_status_change(self, monitor, agent):
        """When db is set, status changes should be logged."""
        mock_db = MagicMock()
        monitor.db = mock_db
        new_output = "fatal: something broke"
        with (
            patch("agent_forge.tmux_utils.capture_pane", return_value=new_output),
            patch("agent_forge.tmux_utils.session_exists", return_value=True),
            patch("agent_forge.status_monitor.log_event", new_callable=AsyncMock) as mock_log,
            patch("agent_forge.status_monitor.save_snapshot", new_callable=AsyncMock),
        ):
            await monitor._poll()

        mock_log.assert_called_once_with(
            mock_db, agent.id, agent.project_name,
            "status_change", {"status": AgentStatus.ERROR.value},
        )


class TestExtractPromptText:
    """Test extract_prompt_text with various terminal outputs."""

    def test_yn_prompt(self):
        output = "Some build output\nMore output\nProceed? Y/n"
        result = StatusMonitor.extract_prompt_text(output)
        assert "Y/n" in result

    def test_allow_prompt_with_context(self):
        output = (
            "line 1\nline 2\nline 3\n"
            "Edit file src/main.py\n"
            "Create new file tests/test_new.py\n"
            "Allow these actions? (y/n)"
        )
        result = StatusMonitor.extract_prompt_text(output)
        assert "Allow" in result
        assert "Edit file" in result

    def test_empty_output(self):
        assert StatusMonitor.extract_prompt_text("") == ""

    def test_no_prompt_found(self):
        output = "Compiling...\nDone.\nAll tests passed."
        assert StatusMonitor.extract_prompt_text(output) == ""

    def test_ansi_stripping(self):
        output = "\x1b[32mSuccess\x1b[0m\n\x1b[1mAllow this action? Y/n\x1b[0m"
        result = StatusMonitor.extract_prompt_text(output)
        assert "Allow" in result
        assert "\x1b" not in result

    def test_do_you_want_prompt(self):
        output = "Installing packages...\nDo you want to continue?"
        result = StatusMonitor.extract_prompt_text(output)
        assert "Do you want" in result

    def test_bracket_yn(self):
        output = "Overwrite existing file? [y/n]"
        result = StatusMonitor.extract_prompt_text(output)
        assert "[y/n]" in result


class TestExtractActivitySummary:
    """Test extract_activity_summary with various terminal outputs."""

    def test_empty_input(self):
        assert StatusMonitor.extract_activity_summary("") == ""

    def test_whitespace_only(self):
        assert StatusMonitor.extract_activity_summary("   \n\n  \n") == ""

    def test_strips_ansi_codes(self):
        output = "\x1b[32mCompiling main.swift\x1b[0m\n\x1b[1mBuild succeeded\x1b[0m"
        result = StatusMonitor.extract_activity_summary(output)
        assert "Compiling main.swift" in result
        assert "Build succeeded" in result
        assert "\x1b" not in result

    def test_filters_prompt_lines(self):
        output = "Ran 5 tests\nAll passed\n> \n$  \n❯ "
        result = StatusMonitor.extract_activity_summary(output)
        assert "Ran 5 tests" in result
        assert "All passed" in result
        # Bare prompt lines should be excluded
        assert result.strip().endswith("All passed")

    def test_filters_spinner_lines(self):
        output = "⠋ Building...\nCompiled 3 files\nDone."
        result = StatusMonitor.extract_activity_summary(output)
        assert "Compiled 3 files" in result
        assert "Done." in result
        assert "⠋" not in result

    def test_returns_last_meaningful_lines(self):
        lines = [f"line {i}" for i in range(30)]
        output = "\n".join(lines)
        result = StatusMonitor.extract_activity_summary(output)
        result_lines = result.splitlines()
        assert len(result_lines) <= 15
        assert "line 29" in result_lines[-1]

    def test_truncates_long_lines(self):
        long_line = "x" * 200
        output = f"short line\n{long_line}\nend"
        result = StatusMonitor.extract_activity_summary(output)
        for line in result.splitlines():
            assert len(line) <= 120

    def test_filters_separator_lines(self):
        output = (
            "────────────────────────────────────────\n"
            "Because light attracts bugs.\n"
            "────────────────────────────────────────\n"
            "❯ tell me another one\n"
            "────────────────────────────────────────\n"
            "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
        )
        result = StatusMonitor.extract_activity_summary(output)
        assert "Because light attracts bugs." in result
        assert "────" not in result
        assert "⏵⏵" not in result

    def test_filters_dash_separators(self):
        output = "result A\n----------------------------------------\nresult B"
        result = StatusMonitor.extract_activity_summary(output)
        assert "result A" in result
        assert "result B" in result
        assert "---" not in result

    def test_filters_equals_separators(self):
        output = "header\n========================================\nbody"
        result = StatusMonitor.extract_activity_summary(output)
        assert "header" in result
        assert "===" not in result

    def test_none_like_input(self):
        assert StatusMonitor.extract_activity_summary(None) == ""


class TestWaitingInputNotification:
    """Test that WAITING_INPUT triggers rich notification path."""

    @pytest.fixture
    def agent(self):
        return Agent(
            id="abc123",
            project_name="test-project",
            session_name="forge__test-project__abc123",
            worktree_path="/tmp/worktree",
            branch_name="agent/abc123/task",
            status=AgentStatus.WORKING,
            created_at=datetime.now(),
            last_activity=datetime.now(),
            last_output="previous output",
            task_description="fix a bug",
        )

    @pytest.fixture
    def monitor_with_connector(self, agent):
        manager = MagicMock()
        manager.list_agents.return_value = [agent]
        ws = MagicMock()
        ws.broadcast_agent_update = AsyncMock()
        ws.broadcast_terminal_output = AsyncMock()
        connector_mgr = MagicMock()
        connector_mgr.send_to_project_channels_rich = AsyncMock()
        connector_mgr.send_to_project_channels = AsyncMock()
        return StatusMonitor(
            agent_manager=manager,
            ws_manager=ws,
            connector_manager=connector_mgr,
        )

    @pytest.mark.asyncio
    async def test_waiting_input_sends_rich_notification(
        self, monitor_with_connector, agent
    ):
        new_output = "Do you want to proceed? Y/n"
        with (
            patch("agent_forge.tmux_utils.capture_pane", return_value=new_output),
            patch("agent_forge.tmux_utils.session_exists", return_value=True),
        ):
            await monitor_with_connector._poll()

        assert agent.status == AgentStatus.WAITING_INPUT
        cm = monitor_with_connector.connector_manager
        cm.send_to_project_channels_rich.assert_called_once()
        call_kwargs = cm.send_to_project_channels_rich.call_args
        text = call_kwargs[0][1]
        extra = call_kwargs[1]["extra"]
        assert "waiting for input" in text
        assert "action_buttons" in extra
        buttons = extra["action_buttons"]
        assert len(buttons) == 3
        assert buttons[0].action == "approve"
        assert buttons[0].agent_id == "abc123"

    @pytest.mark.asyncio
    async def test_non_waiting_status_uses_plain_notify(
        self, monitor_with_connector, agent
    ):
        """Non-WAITING_INPUT transitions use the regular notify path."""
        new_output = "fatal: something broke"
        with (
            patch("agent_forge.tmux_utils.capture_pane", return_value=new_output),
            patch("agent_forge.tmux_utils.session_exists", return_value=True),
        ):
            await monitor_with_connector._poll()

        assert agent.status == AgentStatus.ERROR
        cm = monitor_with_connector.connector_manager
        cm.send_to_project_channels_rich.assert_not_called()
        cm.send_to_project_channels.assert_called_once()


class TestGetActivitySummary:
    """Test _get_activity_summary: LLM path + fallback."""

    @pytest.fixture
    def config_with_summary(self):
        return ForgeConfig(
            defaults=DefaultsConfig(
                summary=SummaryConfig(enabled=True, api_key="test-key"),
            ),
        )

    @pytest.fixture
    def config_disabled(self):
        return ForgeConfig(
            defaults=DefaultsConfig(
                summary=SummaryConfig(enabled=False),
            ),
        )

    @pytest.fixture
    def monitor(self, config_with_summary):
        manager = MagicMock()
        ws = MagicMock()
        return StatusMonitor(
            agent_manager=manager, ws_manager=ws, config=config_with_summary,
        )

    @pytest.mark.asyncio
    async def test_llm_summary_used_when_available(self, monitor):
        with patch(
            "agent_forge.status_monitor.summarize_output",
            new_callable=AsyncMock,
            return_value="LLM summary of agent activity.",
        ) as mock_summarize:
            result = await monitor._get_activity_summary("some terminal output")

        assert result == "LLM summary of agent activity."
        mock_summarize.assert_called_once()

    @pytest.mark.asyncio
    async def test_falls_back_on_llm_failure(self, monitor):
        with patch(
            "agent_forge.status_monitor.summarize_output",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await monitor._get_activity_summary("Compiled 3 files\nDone.")

        # Should fall back to regex-based extraction
        assert "Done." in result

    @pytest.mark.asyncio
    async def test_falls_back_when_disabled(self, config_disabled):
        monitor = StatusMonitor(
            agent_manager=MagicMock(),
            ws_manager=MagicMock(),
            config=config_disabled,
        )
        with patch(
            "agent_forge.status_monitor.summarize_output",
            new_callable=AsyncMock,
        ) as mock_summarize:
            result = await monitor._get_activity_summary("Build complete.\nAll passing.")

        mock_summarize.assert_not_called()
        assert "All passing." in result

    @pytest.mark.asyncio
    async def test_falls_back_when_no_api_key(self):
        config = ForgeConfig(
            defaults=DefaultsConfig(
                summary=SummaryConfig(enabled=True, api_key=""),
            ),
        )
        monitor = StatusMonitor(
            agent_manager=MagicMock(),
            ws_manager=MagicMock(),
            config=config,
        )
        with patch(
            "agent_forge.status_monitor.summarize_output",
            new_callable=AsyncMock,
        ) as mock_summarize:
            result = await monitor._get_activity_summary("Test output line.")

        mock_summarize.assert_not_called()
        assert "Test output line." in result

    @pytest.mark.asyncio
    async def test_falls_back_when_no_config(self):
        monitor = StatusMonitor(
            agent_manager=MagicMock(), ws_manager=MagicMock(), config=None,
        )
        result = await monitor._get_activity_summary("Some output here.")
        assert "Some output here." in result

    @pytest.mark.asyncio
    async def test_env_var_api_key_used(self, config_disabled):
        """When config api_key is empty but env var is set, LLM path activates."""
        config = ForgeConfig(
            defaults=DefaultsConfig(
                summary=SummaryConfig(enabled=True, api_key=""),
            ),
        )
        monitor = StatusMonitor(
            agent_manager=MagicMock(), ws_manager=MagicMock(), config=config,
        )
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "env-key"}),
            patch(
                "agent_forge.status_monitor.summarize_output",
                new_callable=AsyncMock,
                return_value="LLM from env key",
            ) as mock_summarize,
        ):
            result = await monitor._get_activity_summary("output")

        assert result == "LLM from env key"
        mock_summarize.assert_called_once()
        assert mock_summarize.call_args[1]["api_key"] == "env-key"


class TestResponseRelay:
    """Test response relay from pipe-pane logs."""

    @pytest.fixture
    def agent_with_log(self, tmp_path):
        log_file = tmp_path / ".agent_output.log"
        log_file.write_text("Some agent output\nI fixed the bug.\n")
        return Agent(
            id="abc123",
            project_name="test-project",
            session_name="forge__test-project__abc123",
            worktree_path=str(tmp_path),
            branch_name="agent/abc123/task",
            status=AgentStatus.WORKING,
            created_at=datetime.now(),
            last_activity=datetime.now(),
            last_output="previous output",
            task_description="fix a bug",
            output_log_path=str(log_file),
            last_relay_offset=0,
        )

    @pytest.fixture
    def relay_monitor(self, agent_with_log):
        manager = MagicMock()
        manager.list_agents.return_value = [agent_with_log]
        ws = MagicMock()
        ws.broadcast_agent_update = AsyncMock()
        ws.broadcast_terminal_output = AsyncMock()
        connector_mgr = MagicMock()
        connector_mgr.send_to_project_channels = AsyncMock()
        connector_mgr.send_to_project_channels_rich = AsyncMock()
        return StatusMonitor(
            agent_manager=manager,
            ws_manager=ws,
            connector_manager=connector_mgr,
        )

    @pytest.mark.asyncio
    async def test_relay_reads_from_log(self, relay_monitor, agent_with_log):
        await relay_monitor._relay_response(agent_with_log)
        cm = relay_monitor.connector_manager
        cm.send_to_project_channels.assert_called_once()
        text = cm.send_to_project_channels.call_args[0][1]
        assert "response" in text.lower()
        assert "I fixed the bug." in text

    @pytest.mark.asyncio
    async def test_relay_skips_when_no_log_path(self, relay_monitor, agent_with_log):
        agent_with_log.output_log_path = ""
        await relay_monitor._relay_response(agent_with_log)
        relay_monitor.connector_manager.send_to_project_channels.assert_not_called()

    @pytest.mark.asyncio
    async def test_relay_skips_when_no_new_content(self, relay_monitor, agent_with_log):
        log_path = Path(agent_with_log.output_log_path)
        agent_with_log.last_relay_offset = log_path.stat().st_size
        await relay_monitor._relay_response(agent_with_log)
        relay_monitor.connector_manager.send_to_project_channels.assert_not_called()

    @pytest.mark.asyncio
    async def test_relay_updates_offset(self, relay_monitor, agent_with_log):
        log_path = Path(agent_with_log.output_log_path)
        expected_size = log_path.stat().st_size
        await relay_monitor._relay_response(agent_with_log)
        assert agent_with_log.last_relay_offset == expected_size

    @pytest.mark.asyncio
    async def test_relay_uses_llm_when_configured(self, agent_with_log):
        config = ForgeConfig(
            defaults=DefaultsConfig(
                response_relay=ResponseRelayConfig(enabled=True),
            ),
        )
        manager = MagicMock()
        ws = MagicMock()
        connector_mgr = MagicMock()
        connector_mgr.send_to_project_channels = AsyncMock()
        monitor = StatusMonitor(
            agent_manager=manager,
            ws_manager=ws,
            connector_manager=connector_mgr,
            config=config,
        )
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch(
                "agent_forge.status_monitor.extract_response",
                new_callable=AsyncMock,
                return_value="Extracted response text",
            ) as mock_extract,
        ):
            await monitor._relay_response(agent_with_log)

        mock_extract.assert_called_once()
        text = connector_mgr.send_to_project_channels.call_args[0][1]
        assert "Extracted response text" in text

    @pytest.mark.asyncio
    async def test_relay_falls_back_to_regex(self, agent_with_log):
        config = ForgeConfig(
            defaults=DefaultsConfig(
                response_relay=ResponseRelayConfig(enabled=True),
            ),
        )
        manager = MagicMock()
        ws = MagicMock()
        connector_mgr = MagicMock()
        connector_mgr.send_to_project_channels = AsyncMock()
        monitor = StatusMonitor(
            agent_manager=manager,
            ws_manager=ws,
            connector_manager=connector_mgr,
            config=config,
        )
        with (
            patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}),
            patch(
                "agent_forge.status_monitor.extract_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await monitor._relay_response(agent_with_log)

        connector_mgr.send_to_project_channels.assert_called_once()

    @pytest.mark.asyncio
    async def test_poll_triggers_relay_on_working_to_idle(
        self, relay_monitor, agent_with_log
    ):
        new_output = "claude >"
        with (
            patch("agent_forge.tmux_utils.capture_pane", return_value=new_output),
            patch("agent_forge.tmux_utils.session_exists", return_value=True),
        ):
            await relay_monitor._poll()

        assert agent_with_log.status == AgentStatus.IDLE
        cm = relay_monitor.connector_manager
        cm.send_to_project_channels.assert_called_once()
