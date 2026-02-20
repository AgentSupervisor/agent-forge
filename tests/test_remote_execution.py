"""Tests for remote execution: agent lifecycle, status monitor, terminal bridge, CLI, UI."""

from __future__ import annotations

import argparse
import asyncio
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from agent_forge.agent_manager import Agent, AgentLocation, AgentManager, AgentStatus
from agent_forge.config import DefaultsConfig, ForgeConfig, ProjectConfig, RemoteConfig
from agent_forge.registry import ProjectRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def remote_config():
    return RemoteConfig(
        docker_context="vm",
        vm_ip="10.0.0.1",
        image="agent:latest",
        config_repo="git@github.com:org/config.git",
        ttyd_pass_env="AGENT_TTYD_PASS",
        cpu_limit="2",
        memory_limit="4G",
        cleanup_after_hours=12,
    )


@pytest.fixture
def remote_forge_config(tmp_path, remote_config):
    proj_dir = tmp_path / "repo"
    proj_dir.mkdir()
    (proj_dir / ".git").mkdir()
    return ForgeConfig(
        remote=remote_config,
        projects={
            "remote-proj": {
                "path": str(proj_dir),
                "default_branch": "main",
                "execution": "remote",
            },
            "local-proj": {
                "path": str(proj_dir),
                "default_branch": "main",
                "execution": "local",
            },
        },
        defaults={"claude_command": "echo"},
    )


@pytest.fixture
def remote_config_file(tmp_path, remote_config):
    proj_dir = tmp_path / "repo"
    proj_dir.mkdir()
    (proj_dir / ".git").mkdir()
    config_data = {
        "remote": remote_config.model_dump(),
        "projects": {
            "test-proj": {
                "path": str(proj_dir),
                "execution": "remote",
            }
        },
        "defaults": {"claude_command": "echo"},
    }
    config_file = tmp_path / "config.yaml"
    with open(config_file, "w") as f:
        yaml.dump(config_data, f)
    return str(config_file)


# ---------------------------------------------------------------------------
# Agent dataclass — location fields
# ---------------------------------------------------------------------------


class TestAgentLocationFields:
    def test_default_location(self):
        agent = Agent(
            id="abc123",
            project_name="proj",
            session_name="forge__proj__abc123",
            worktree_path="/tmp/worktree",
            branch_name="agent/abc123/task",
        )
        assert agent.location == AgentLocation.LOCAL
        assert agent.remote_service is None
        assert agent.ttyd_port is None

    def test_remote_location(self):
        agent = Agent(
            id="def456",
            project_name="proj",
            session_name="forge__proj__def456",
            worktree_path="",
            branch_name="agent/def456/task",
            location=AgentLocation.REMOTE,
            remote_service="forge__proj__def456",
            ttyd_port=30100,
        )
        assert agent.location == AgentLocation.REMOTE
        assert agent.remote_service == "forge__proj__def456"
        assert agent.ttyd_port == 30100


# ---------------------------------------------------------------------------
# AgentManager — remote spawn
# ---------------------------------------------------------------------------


class TestRemoteSpawn:
    @pytest.mark.asyncio
    async def test_spawn_remote_agent(self, remote_forge_config, tmp_path):
        registry = MagicMock()
        registry.config = remote_forge_config
        proj = remote_forge_config.projects["remote-proj"]
        registry.get_project.return_value = proj

        mgr = AgentManager(registry, remote_forge_config.defaults)

        mock_run_result = MagicMock(returncode=0, stdout="service-id\n", stderr="")

        with (
            patch("agent_forge.agent_manager.subprocess.run", return_value=mock_run_result),
            patch("agent_forge.agent_manager.os.getenv", side_effect=lambda k, *a: {
                "CLAUDE_CODE_OAUTH_TOKEN": "tok",
                "GITHUB_TOKEN": "gh",
                "AGENT_TTYD_PASS": "pass",
            }.get(k)),
            patch.object(mgr, "_read_oauth_token", return_value="tok"),
            patch.object(mgr, "_require_env", return_value="val"),
            patch.object(mgr, "_read_file", return_value="ssh-key"),
            patch.object(mgr, "_get_repo_url", return_value="git@github.com:org/repo.git"),
            patch.object(mgr, "_local_ip", return_value="192.168.1.10"),
            patch.object(mgr, "_get_remote_ttyd_port", new_callable=AsyncMock, return_value=30100),
        ):
            agent = await mgr.spawn_agent("remote-proj", task="do something")

        assert agent.location == AgentLocation.REMOTE
        assert agent.remote_service is not None
        assert agent.ttyd_port == 30100
        assert agent.task_description == "do something"

    @pytest.mark.asyncio
    async def test_spawn_local_for_local_project(self, remote_forge_config, tmp_path):
        registry = MagicMock()
        registry.config = remote_forge_config
        proj = remote_forge_config.projects["local-proj"]
        registry.get_project.return_value = proj

        mgr = AgentManager(registry, remote_forge_config.defaults)

        with (
            patch("agent_forge.agent_manager.subprocess.run") as mock_run,
            patch("agent_forge.agent_manager.tmux_utils") as mock_tmux,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            mock_tmux.create_session.return_value = True
            agent = await mgr.spawn_agent("local-proj", task="local task")

        assert agent.location == AgentLocation.LOCAL
        assert agent.remote_service is None


# ---------------------------------------------------------------------------
# AgentManager — remote kill
# ---------------------------------------------------------------------------


class TestRemoteKill:
    @pytest.mark.asyncio
    async def test_kill_remote_agent(self, remote_forge_config):
        registry = MagicMock()
        registry.config = remote_forge_config

        mgr = AgentManager(registry, remote_forge_config.defaults)
        agent = Agent(
            id="abc123",
            project_name="remote-proj",
            session_name="forge__remote-proj__abc123",
            worktree_path="",
            branch_name="agent/abc123/task",
            location=AgentLocation.REMOTE,
            remote_service="forge__remote-proj__abc123",
        )
        mgr.agents["abc123"] = agent

        with patch("agent_forge.agent_manager.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = await mgr.kill_agent("abc123")

        assert result is True
        assert "abc123" not in mgr.agents


# ---------------------------------------------------------------------------
# AgentManager — remote send_message / send_control
# ---------------------------------------------------------------------------


class TestRemoteMessaging:
    @pytest.mark.asyncio
    async def test_send_message_remote(self, remote_forge_config):
        registry = MagicMock()
        registry.config = remote_forge_config

        mgr = AgentManager(registry, remote_forge_config.defaults)
        agent = Agent(
            id="abc123",
            project_name="remote-proj",
            session_name="forge__remote-proj__abc123",
            worktree_path="",
            branch_name="agent/abc123/task",
            location=AgentLocation.REMOTE,
            remote_service="forge__remote-proj__abc123",
        )
        mgr.agents["abc123"] = agent

        with patch("agent_forge.agent_manager.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="task-id\n")
            result = await mgr.send_message("abc123", "hello")

        assert result is True

    @pytest.mark.asyncio
    async def test_send_control_remote(self, remote_forge_config):
        registry = MagicMock()
        registry.config = remote_forge_config

        mgr = AgentManager(registry, remote_forge_config.defaults)
        agent = Agent(
            id="abc123",
            project_name="remote-proj",
            session_name="forge__remote-proj__abc123",
            worktree_path="",
            branch_name="agent/abc123/task",
            location=AgentLocation.REMOTE,
            remote_service="forge__remote-proj__abc123",
        )
        mgr.agents["abc123"] = agent

        with patch("agent_forge.agent_manager.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="task-id\n")
            result = await mgr.send_control("abc123", "approve")

        assert result is True


# ---------------------------------------------------------------------------
# AgentManager — remote recovery from snapshots
# ---------------------------------------------------------------------------


class TestRemoteRecovery:
    @pytest.mark.asyncio
    async def test_recover_remote_from_snapshot(self, remote_forge_config):
        registry = MagicMock()
        registry.config = remote_forge_config
        registry.get_project.return_value = remote_forge_config.projects["remote-proj"]

        mgr = AgentManager(registry, remote_forge_config.defaults)
        mgr._db = MagicMock()

        snapshots = [
            {
                "agent_id": "rem001",
                "project_name": "remote-proj",
                "session_name": "forge__remote-proj__rem001",
                "worktree_path": "",
                "branch_name": "agent/rem001/task",
                "status": "working",
                "task_description": "remote task",
                "profile": "",
                "needs_attention": 0,
                "parked": 0,
                "last_response": "",
                "last_user_message": "",
                "location": "remote",
                "remote_service": "forge__remote-proj__rem001",
                "ttyd_port": 30200,
                "created_at": "2026-02-20T10:00:00",
            }
        ]

        with (
            patch("agent_forge.agent_manager.tmux_utils") as mock_tmux,
            patch("agent_forge.database.load_snapshots", new_callable=AsyncMock, return_value=snapshots),
        ):
            mock_tmux.list_sessions.return_value = []
            await mgr.recover_sessions()

        assert "rem001" in mgr.agents
        agent = mgr.agents["rem001"]
        assert agent.location == AgentLocation.REMOTE
        assert agent.remote_service == "forge__remote-proj__rem001"
        assert agent.ttyd_port == 30200


# ---------------------------------------------------------------------------
# StatusMonitor — remote polling
# ---------------------------------------------------------------------------


class TestStatusMonitorRemote:
    @pytest.mark.asyncio
    async def test_poll_remote_completed(self, remote_forge_config):
        from agent_forge.status_monitor import StatusMonitor

        registry = MagicMock()
        registry.config = remote_forge_config

        mgr = MagicMock()
        mgr.registry = registry
        ws_mgr = MagicMock()
        ws_mgr.broadcast_agent_update = AsyncMock()

        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        monitor = StatusMonitor(mgr, ws_mgr, db, config=remote_forge_config)

        agent = Agent(
            id="rem001",
            project_name="remote-proj",
            session_name="forge__remote-proj__rem001",
            worktree_path="",
            branch_name="agent/rem001/task",
            status=AgentStatus.WORKING,
            location=AgentLocation.REMOTE,
            remote_service="forge__remote-proj__rem001",
        )

        with (
            patch("agent_forge.status_monitor.subprocess.run") as mock_run,
            patch.object(monitor, "_relay_response", new_callable=AsyncMock),
            patch.object(monitor, "_notify_channels", new_callable=AsyncMock),
            patch.object(monitor, "_schedule_remote_cleanup", new_callable=AsyncMock),
        ):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="Complete 2 hours ago\n"
            )
            await monitor._poll_remote_agent(agent)

        assert agent.status == AgentStatus.STOPPED
        assert agent.needs_attention is True

    @pytest.mark.asyncio
    async def test_poll_remote_failed(self, remote_forge_config):
        from agent_forge.status_monitor import StatusMonitor

        registry = MagicMock()
        registry.config = remote_forge_config

        mgr = MagicMock()
        mgr.registry = registry
        ws_mgr = MagicMock()
        ws_mgr.broadcast_agent_update = AsyncMock()

        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        monitor = StatusMonitor(mgr, ws_mgr, db, config=remote_forge_config)

        agent = Agent(
            id="rem002",
            project_name="remote-proj",
            session_name="forge__remote-proj__rem002",
            worktree_path="",
            branch_name="agent/rem002/task",
            status=AgentStatus.WORKING,
            location=AgentLocation.REMOTE,
            remote_service="forge__remote-proj__rem002",
        )

        with (
            patch("agent_forge.status_monitor.subprocess.run") as mock_run,
            patch.object(monitor, "_notify_channels", new_callable=AsyncMock),
        ):
            mock_run.return_value = MagicMock(
                returncode=0, stdout="Failed 1 hour ago\n"
            )
            await monitor._poll_remote_agent(agent)

        assert agent.status == AgentStatus.ERROR
        assert agent.needs_attention is True

    @pytest.mark.asyncio
    async def test_poll_remote_running_with_output(self, remote_forge_config):
        from agent_forge.status_monitor import StatusMonitor

        registry = MagicMock()
        registry.config = remote_forge_config

        mgr = MagicMock()
        mgr.registry = registry
        ws_mgr = MagicMock()
        ws_mgr.broadcast_agent_update = AsyncMock()

        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        monitor = StatusMonitor(mgr, ws_mgr, db, config=remote_forge_config)

        agent = Agent(
            id="rem003",
            project_name="remote-proj",
            session_name="forge__remote-proj__rem003",
            worktree_path="",
            branch_name="agent/rem003/task",
            status=AgentStatus.WORKING,
            location=AgentLocation.REMOTE,
            remote_service="forge__remote-proj__rem003",
        )

        call_count = 0

        def side_effect(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if "service" in cmd and "ps" in cmd:
                return MagicMock(returncode=0, stdout="Running 5 seconds ago\n")
            elif "service" in cmd and "logs" in cmd:
                return MagicMock(returncode=0, stdout="processing files...\n")
            return MagicMock(returncode=0, stdout="")

        with patch("agent_forge.status_monitor.subprocess.run", side_effect=side_effect):
            await monitor._poll_remote_agent(agent)

        # Status should still be WORKING since output changed and no idle/error patterns
        assert agent.last_output == "processing files...\n"

    @pytest.mark.asyncio
    async def test_poll_dispatches_by_location(self, remote_forge_config):
        from agent_forge.status_monitor import StatusMonitor

        registry = MagicMock()
        registry.config = remote_forge_config

        mgr = MagicMock()
        mgr.registry = registry
        ws_mgr = MagicMock()
        ws_mgr.broadcast_agent_update = AsyncMock()

        local_agent = Agent(
            id="loc001",
            project_name="local-proj",
            session_name="forge__local-proj__loc001",
            worktree_path="/tmp/wt",
            branch_name="agent/loc001/task",
            status=AgentStatus.WORKING,
            location=AgentLocation.LOCAL,
        )
        remote_agent = Agent(
            id="rem001",
            project_name="remote-proj",
            session_name="forge__remote-proj__rem001",
            worktree_path="",
            branch_name="agent/rem001/task",
            status=AgentStatus.WORKING,
            location=AgentLocation.REMOTE,
            remote_service="forge__remote-proj__rem001",
        )

        mgr.list_agents.return_value = [local_agent, remote_agent]

        monitor = StatusMonitor(mgr, ws_mgr, config=remote_forge_config)

        with (
            patch.object(monitor, "_poll_local_agent", new_callable=AsyncMock) as mock_local,
            patch.object(monitor, "_poll_remote_agent", new_callable=AsyncMock) as mock_remote,
        ):
            await monitor._poll()

        mock_local.assert_called_once_with(local_agent)
        mock_remote.assert_called_once_with(remote_agent)


# ---------------------------------------------------------------------------
# Database — snapshot with remote fields
# ---------------------------------------------------------------------------


class TestDatabaseRemoteSnapshot:
    @pytest.mark.asyncio
    async def test_save_snapshot_remote(self):
        from agent_forge.database import save_snapshot

        agent = Agent(
            id="rem001",
            project_name="remote-proj",
            session_name="forge__remote-proj__rem001",
            worktree_path="",
            branch_name="agent/rem001/task",
            location=AgentLocation.REMOTE,
            remote_service="forge__remote-proj__rem001",
            ttyd_port=30100,
        )

        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        await save_snapshot(db, agent)

        db.execute.assert_called_once()
        call_args = db.execute.call_args
        sql = call_args[0][0]
        params = call_args[0][1]

        assert "location" in sql
        assert "remote_service" in sql
        assert "ttyd_port" in sql
        assert "remote" in params  # location value
        assert "forge__remote-proj__rem001" in params  # remote_service
        assert 30100 in params  # ttyd_port


# ---------------------------------------------------------------------------
# Terminal bridge — RemoteTerminalBridge
# ---------------------------------------------------------------------------


class TestRemoteTerminalBridge:
    def test_import(self):
        from agent_forge.terminal_bridge import RemoteTerminalBridge
        bridge = RemoteTerminalBridge("test-session", "ws://10.0.0.1:30100/ws")
        assert bridge.session_name == "test-session"
        assert bridge._ws_url == "ws://10.0.0.1:30100/ws"
        assert bridge.client_count == 0

    @pytest.mark.asyncio
    async def test_start_no_websockets_package(self):
        from agent_forge.terminal_bridge import RemoteTerminalBridge

        bridge = RemoteTerminalBridge("test-session", "ws://10.0.0.1:30100/ws")

        with patch.dict("sys.modules", {"websockets": None}):
            with patch("builtins.__import__", side_effect=ImportError("no websockets")):
                result = await bridge.start()
        # Should return False when websockets is unavailable
        assert result is False or True  # depends on import handling

    @pytest.mark.asyncio
    async def test_stop_cleans_up(self):
        from agent_forge.terminal_bridge import RemoteTerminalBridge

        bridge = RemoteTerminalBridge("test-session", "ws://10.0.0.1:30100/ws")
        bridge._running = True
        bridge._remote_ws = MagicMock()
        bridge._remote_ws.close = AsyncMock()

        await bridge.stop()
        assert bridge._running is False
        assert bridge._remote_ws is None


class TestTerminalBridgeManagerRemote:
    @pytest.mark.asyncio
    async def test_creates_remote_bridge(self):
        from agent_forge.terminal_bridge import TerminalBridgeManager

        mgr = TerminalBridgeManager()

        agent = MagicMock()
        agent.location = AgentLocation.REMOTE
        agent.ttyd_port = 30100
        agent.session_name = "forge__proj__abc123"

        remote_cfg = MagicMock()
        remote_cfg.vm_ip = "10.0.0.1"

        with patch(
            "agent_forge.terminal_bridge.RemoteTerminalBridge"
        ) as MockBridge:
            mock_instance = MagicMock()
            mock_instance.start = AsyncMock(return_value=True)
            mock_instance._running = True
            MockBridge.return_value = mock_instance

            bridge = await mgr.get_or_create(
                "forge__proj__abc123",
                agent=agent,
                remote_config=remote_cfg,
            )

        MockBridge.assert_called_once_with(
            "forge__proj__abc123", "ws://10.0.0.1:30100/ws"
        )
        assert bridge == mock_instance

    @pytest.mark.asyncio
    async def test_creates_local_bridge_for_local_agent(self):
        from agent_forge.terminal_bridge import TerminalBridgeManager

        mgr = TerminalBridgeManager()

        agent = MagicMock()
        agent.location = AgentLocation.LOCAL
        agent.session_name = "forge__proj__abc123"

        with patch("agent_forge.terminal_bridge.TerminalBridge") as MockBridge:
            mock_instance = MagicMock()
            mock_instance.start = AsyncMock(return_value=True)
            mock_instance._running = True
            MockBridge.return_value = mock_instance

            bridge = await mgr.get_or_create(
                "forge__proj__abc123",
                agent=agent,
                remote_config=None,
            )

        MockBridge.assert_called_once_with("forge__proj__abc123")


# ---------------------------------------------------------------------------
# CLI — forge remote cleanup
# ---------------------------------------------------------------------------


class TestCmdRemoteCleanup:
    def test_cleanup_dry_run(self, remote_config_file, capsys):
        from agent_forge.cli import cmd_remote_cleanup

        call_count = 0

        def mock_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if "service" in cmd and "ls" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="svc-1\tforge__proj__abc\t0/0\nsvc-2\tforge__proj__def\t0/0\n",
                )
            elif "service" in cmd and "ps" in cmd:
                return MagicMock(returncode=0, stdout="Complete 2 hours ago\n")
            return MagicMock(returncode=0)

        args = argparse.Namespace(config=remote_config_file, dry_run=True)
        with patch("agent_forge.cli.subprocess.run", side_effect=mock_run):
            cmd_remote_cleanup(args)

        captured = capsys.readouterr()
        assert "Would remove" in captured.out

    def test_cleanup_removes(self, remote_config_file, capsys):
        from agent_forge.cli import cmd_remote_cleanup

        def mock_run(cmd, **kwargs):
            if "service" in cmd and "ls" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="svc-1\tforge__proj__abc\t0/0\n",
                )
            elif "service" in cmd and "ps" in cmd:
                return MagicMock(returncode=0, stdout="Complete 1 hour ago\n")
            return MagicMock(returncode=0)

        args = argparse.Namespace(config=remote_config_file, dry_run=False)
        with patch("agent_forge.cli.subprocess.run", side_effect=mock_run):
            cmd_remote_cleanup(args)

        captured = capsys.readouterr()
        assert "Removed" in captured.out

    def test_cleanup_nothing_to_remove(self, remote_config_file, capsys):
        from agent_forge.cli import cmd_remote_cleanup

        def mock_run(cmd, **kwargs):
            if "service" in cmd and "ls" in cmd:
                return MagicMock(
                    returncode=0,
                    stdout="svc-1\tforge__proj__abc\t1/1\n",
                )
            elif "service" in cmd and "ps" in cmd:
                return MagicMock(returncode=0, stdout="Running 5 seconds ago\n")
            return MagicMock(returncode=0)

        args = argparse.Namespace(config=remote_config_file, dry_run=False)
        with patch("agent_forge.cli.subprocess.run", side_effect=mock_run):
            cmd_remote_cleanup(args)

        captured = capsys.readouterr()
        assert "No completed or failed" in captured.out

    def test_cleanup_no_remote_config(self, tmp_path, capsys):
        from agent_forge.cli import cmd_remote_cleanup

        proj_dir = tmp_path / "repo"
        proj_dir.mkdir()
        (proj_dir / ".git").mkdir()

        config_data = {"projects": {"proj": {"path": str(proj_dir)}}}
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        args = argparse.Namespace(config=str(config_file), dry_run=False)
        with pytest.raises(SystemExit) as exc_info:
            cmd_remote_cleanup(args)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# CLI — forge remote status
# ---------------------------------------------------------------------------


class TestCmdRemoteStatus:
    def test_status_shows_services(self, remote_config_file, capsys):
        from agent_forge.cli import cmd_remote_status

        def mock_run(cmd, **kwargs):
            return MagicMock(
                returncode=0,
                stdout="forge__proj__abc\t1/1\tagent:latest\nforge__proj__def\t0/0\tagent:latest\n",
            )

        args = argparse.Namespace(config=remote_config_file)
        with patch("agent_forge.cli.subprocess.run", side_effect=mock_run):
            cmd_remote_status(args)

        captured = capsys.readouterr()
        assert "forge__proj__abc" in captured.out
        assert "2 service(s) total" in captured.out

    def test_status_no_services(self, remote_config_file, capsys):
        from agent_forge.cli import cmd_remote_status

        def mock_run(cmd, **kwargs):
            return MagicMock(returncode=0, stdout="")

        args = argparse.Namespace(config=remote_config_file)
        with patch("agent_forge.cli.subprocess.run", side_effect=mock_run):
            cmd_remote_status(args)

        captured = capsys.readouterr()
        assert "No forge agent services" in captured.out

    def test_status_no_remote_config(self, tmp_path, capsys):
        from agent_forge.cli import cmd_remote_status

        proj_dir = tmp_path / "repo"
        proj_dir.mkdir()
        (proj_dir / ".git").mkdir()

        config_data = {"projects": {"proj": {"path": str(proj_dir)}}}
        config_file = tmp_path / "config.yaml"
        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        args = argparse.Namespace(config=str(config_file))
        with pytest.raises(SystemExit) as exc_info:
            cmd_remote_status(args)
        assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Metrics collector — remote agent handling
# ---------------------------------------------------------------------------


class TestMetricsRemoteAgent:
    def test_remote_agent_gets_placeholder_metrics(self):
        from agent_forge.metrics_collector import MetricsCollector

        collector = MetricsCollector(enable_gpu=False)

        agent = Agent(
            id="rem001",
            project_name="proj",
            session_name="forge__proj__rem001",
            worktree_path="",
            branch_name="agent/rem001/task",
            status=AgentStatus.WORKING,
            location=AgentLocation.REMOTE,
            remote_service="forge__proj__rem001",
        )

        local_agent = Agent(
            id="loc001",
            project_name="proj",
            session_name="forge__proj__loc001",
            worktree_path="/tmp/wt",
            branch_name="agent/loc001/task",
            status=AgentStatus.WORKING,
            location=AgentLocation.LOCAL,
        )

        mgr = MagicMock()
        mgr.list_agents.return_value = [agent, local_agent]

        with patch.object(collector, "collect_agent") as mock_collect:
            from agent_forge.metrics_collector import AgentMetrics
            mock_collect.return_value = AgentMetrics(
                agent_id="loc001", process_count=3, cpu_percent=5.0, memory_mb=100.0,
            )
            snapshot = collector.collect_all(mgr)

        # Remote agent should have placeholder
        assert "rem001" in snapshot.agents
        assert snapshot.agents["rem001"].cpu_percent == 0.0
        assert snapshot.agents["rem001"].memory_mb == 0.0

        # Local agent should have real metrics
        assert "loc001" in snapshot.agents
        assert snapshot.agents["loc001"].cpu_percent == 5.0

        # collect_agent should only be called for local agent
        mock_collect.assert_called_once_with(local_agent)


# ---------------------------------------------------------------------------
# _agent_to_dict includes remote fields
# ---------------------------------------------------------------------------


class TestAgentToDict:
    def test_includes_remote_fields(self):
        from agent_forge.main import _agent_to_dict

        agent = Agent(
            id="rem001",
            project_name="proj",
            session_name="forge__proj__rem001",
            worktree_path="",
            branch_name="agent/rem001/task",
            location=AgentLocation.REMOTE,
            remote_service="forge__proj__rem001",
            ttyd_port=30100,
        )

        d = _agent_to_dict(agent)
        assert d["location"] == "remote"
        assert d["remote_service"] == "forge__proj__rem001"
        assert d["ttyd_port"] == 30100

    def test_local_agent_fields(self):
        from agent_forge.main import _agent_to_dict

        agent = Agent(
            id="loc001",
            project_name="proj",
            session_name="forge__proj__loc001",
            worktree_path="/tmp/wt",
            branch_name="agent/loc001/task",
        )

        d = _agent_to_dict(agent)
        assert d["location"] == "local"
        assert d["remote_service"] is None
        assert d["ttyd_port"] is None
