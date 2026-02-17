"""Tests for AgentManager — spawn, kill, message, listing."""

from unittest.mock import MagicMock, patch
import subprocess

import pytest
import pytest_asyncio

from agent_forge.agent_manager import Agent, AgentManager, AgentStatus, _sanitize_for_branch
from agent_forge.config import AgentProfile, DefaultsConfig, ForgeConfig, StartSequenceStep
from agent_forge.registry import ProjectRegistry


class TestSanitizeBranch:
    def test_simple_text(self):
        assert _sanitize_for_branch("fix auth bug") == "fix-auth-bug"

    def test_special_characters(self):
        assert _sanitize_for_branch("fix: the bug!") == "fix-the-bug"

    def test_truncation(self):
        result = _sanitize_for_branch("a" * 100)
        assert len(result) <= 50

    def test_empty_string(self):
        assert _sanitize_for_branch("") == "task"


class TestAgentManager:
    @pytest.fixture
    def manager(self, registry):
        defaults = DefaultsConfig(
            max_agents_per_project=3,
            claude_command="echo",
            poll_interval_seconds=1.0,
        )
        return AgentManager(registry=registry, defaults=defaults)

    @pytest.mark.asyncio
    async def test_spawn_agent(self, manager, tmp_git_repo):
        """Test spawning an agent with mocked subprocess and tmux."""
        # We need to mock git worktree add and tmux calls
        with (
            patch("subprocess.run") as mock_run,
            patch("agent_forge.tmux_utils.create_session", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            agent = await manager.spawn_agent("test-project", task="fix login bug")

            assert agent.project_name == "test-project"
            assert agent.task_description == "fix login bug"
            assert agent.status == AgentStatus.STARTING
            assert agent.id in manager.agents
            assert "forge__test-project__" in agent.session_name
            assert "fix-login-bug" in agent.branch_name

    @pytest.mark.asyncio
    async def test_spawn_exceeds_limit(self, manager):
        """Test that spawning beyond limit raises error."""
        # Set limit to 2 and fill up
        with (
            patch("subprocess.run") as mock_run,
            patch("agent_forge.tmux_utils.create_session", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            await manager.spawn_agent("test-project", task="task1")
            await manager.spawn_agent("test-project", task="task2")

            with pytest.raises(RuntimeError, match="Agent limit reached"):
                await manager.spawn_agent("test-project", task="task3")

    @pytest.mark.asyncio
    async def test_kill_agent(self, manager):
        """Test killing an agent cleans up."""
        with (
            patch("subprocess.run") as mock_run,
            patch("agent_forge.tmux_utils.create_session", return_value=True),
            patch("agent_forge.tmux_utils.kill_session", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            agent = await manager.spawn_agent("test-project", task="some task")
            agent_id = agent.id

            result = await manager.kill_agent(agent_id)
            assert result is True
            assert agent_id not in manager.agents

    @pytest.mark.asyncio
    async def test_kill_nonexistent_agent(self, manager):
        result = await manager.kill_agent("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_message(self, manager):
        """Test sending a message to an agent."""
        with (
            patch("subprocess.run") as mock_run,
            patch("agent_forge.tmux_utils.create_session", return_value=True),
            patch("agent_forge.tmux_utils.send_keys", return_value=True) as mock_send,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            agent = await manager.spawn_agent("test-project")

            result = await manager.send_message(agent.id, "hello world")
            assert result is True
            mock_send.assert_called_with(agent.session_name, "hello world")

    @pytest.mark.asyncio
    async def test_send_message_nonexistent(self, manager):
        result = await manager.send_message("nonexistent", "hello")
        assert result is False

    @pytest.mark.asyncio
    async def test_list_agents(self, manager):
        with (
            patch("subprocess.run") as mock_run,
            patch("agent_forge.tmux_utils.create_session", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            await manager.spawn_agent("test-project", task="task1")
            await manager.spawn_agent("test-project", task="task2")

        all_agents = manager.list_agents()
        assert len(all_agents) == 2

        project_agents = manager.list_agents(project_name="test-project")
        assert len(project_agents) == 2

        other_agents = manager.list_agents(project_name="other")
        assert len(other_agents) == 0

    @pytest.mark.asyncio
    async def test_get_agents_by_project(self, manager):
        with (
            patch("subprocess.run") as mock_run,
            patch("agent_forge.tmux_utils.create_session", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            await manager.spawn_agent("test-project", task="task1")

        grouped = manager.get_agents_by_project()
        assert "test-project" in grouped
        assert len(grouped["test-project"]) == 1

    @pytest.mark.asyncio
    async def test_recover_sessions(self, manager):
        """Test recovery of existing tmux sessions."""
        mock_session = MagicMock()
        mock_session.name = "forge__test-project__abc123"

        with patch("agent_forge.tmux_utils.list_sessions", return_value=[mock_session]):
            await manager.recover_sessions()

        assert "abc123" in manager.agents
        agent = manager.agents["abc123"]
        assert agent.project_name == "test-project"
        assert agent.status == AgentStatus.IDLE

    @pytest.mark.asyncio
    async def test_recover_sessions_restores_snapshot(self, manager):
        """Test that recovery loads persisted fields from database snapshots."""
        from unittest.mock import AsyncMock

        mock_session = MagicMock()
        mock_session.name = "forge__test-project__abc123"

        snapshot_rows = [
            {
                "agent_id": "abc123",
                "project_name": "test-project",
                "session_name": "forge__test-project__abc123",
                "worktree_path": "/tmp/worktree",
                "branch_name": "agent/abc123/fix-login-bug",
                "status": "idle",
                "task_description": "fix login bug",
                "created_at": "2026-01-15T10:00:00",
                "last_activity": "2026-01-15T11:00:00",
                "last_output": "",
                "needs_attention": 1,
                "parked": 0,
            }
        ]

        manager._db = MagicMock()
        with (
            patch("agent_forge.tmux_utils.list_sessions", return_value=[mock_session]),
            patch("agent_forge.database.load_snapshots", new_callable=AsyncMock, return_value=snapshot_rows),
        ):
            await manager.recover_sessions()

        assert "abc123" in manager.agents
        agent = manager.agents["abc123"]
        assert agent.task_description == "fix login bug"
        assert agent.branch_name == "agent/abc123/fix-login-bug"
        assert agent.needs_attention is True
        assert agent.created_at.year == 2026

    @pytest.mark.asyncio
    async def test_spawn_cleanup_on_tmux_failure(self, manager):
        """Test that failed tmux session creation cleans up the worktree."""
        with (
            patch("subprocess.run") as mock_run,
            patch("agent_forge.tmux_utils.create_session", return_value=False),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            with pytest.raises(RuntimeError, match="Failed to create tmux session"):
                await manager.spawn_agent("test-project", task="will fail")

        assert len(manager.agents) == 0

    @pytest.mark.asyncio
    async def test_install_hooks_creates_settings(self, manager, tmp_git_repo):
        """_install_hooks creates .claude/settings.local.json with correct hook config."""
        import json
        worktree_dir = tmp_git_repo / "test-worktree"
        worktree_dir.mkdir()

        manager._install_hooks(worktree_dir, "abc123")

        settings_path = worktree_dir / ".claude" / "settings.local.json"
        assert settings_path.exists()

        config = json.loads(settings_path.read_text())
        assert "hooks" in config
        assert "SubagentStart" in config["hooks"]
        assert "SubagentStop" in config["hooks"]

        # Verify the hook commands reference the agent ID and hook_reporter.py
        start_cmd = config["hooks"]["SubagentStart"][0]["hooks"][0]["command"]
        assert "abc123" in start_cmd
        assert "SubagentStart" in start_cmd
        assert "hook_reporter.py" in start_cmd

        stop_cmd = config["hooks"]["SubagentStop"][0]["hooks"][0]["command"]
        assert "abc123" in stop_cmd
        assert "SubagentStop" in stop_cmd


class TestCLAUDEmdGeneration:
    """Tests for CLAUDE.md generation with merged instruction layers."""

    @pytest.fixture
    def manager(self, registry):
        defaults = DefaultsConfig(
            max_agents_per_project=3,
            claude_command="echo",
            poll_interval_seconds=1.0,
            agent_instructions="Global: Always parallelize work.",
        )
        return AgentManager(registry=registry, defaults=defaults)

    def test_global_instructions_only(self, manager, tmp_path):
        """CLAUDE.md should contain global instructions when no profile or project instructions."""
        worktree = tmp_path / "wt"
        worktree.mkdir()

        manager._generate_claude_md(worktree, "test-project", profile=None)

        claude_md = worktree / "CLAUDE.md"
        assert claude_md.exists()
        content = claude_md.read_text()
        assert "Global: Always parallelize work." in content

    def test_merged_layers(self, manager, tmp_path):
        """CLAUDE.md merges global + project + profile instructions."""
        worktree = tmp_path / "wt"
        worktree.mkdir()

        # Set project instructions
        project = manager.registry.config.projects["test-project"]
        project.agent_instructions = "Project: Use pytest for testing."

        profile = AgentProfile(
            description="Test profile",
            instructions="Profile: Enter plan mode first.",
        )

        manager._generate_claude_md(worktree, "test-project", profile=profile)

        content = (worktree / "CLAUDE.md").read_text()
        assert "Global: Always parallelize work." in content
        assert "Project: Use pytest for testing." in content
        assert "Profile: Enter plan mode first." in content
        # Global should come before project, project before profile
        gi = content.index("Global:")
        pi = content.index("Project:")
        pr = content.index("Profile:")
        assert gi < pi < pr

    def test_preserves_existing_claude_md(self, manager, tmp_path):
        """Existing CLAUDE.md content is preserved at the end."""
        worktree = tmp_path / "wt"
        worktree.mkdir()
        existing = worktree / "CLAUDE.md"
        existing.write_text("# Existing Project Docs\nDo not delete this.\n")

        manager._generate_claude_md(worktree, "test-project", profile=None)

        content = (worktree / "CLAUDE.md").read_text()
        assert "Global: Always parallelize work." in content
        assert "# Existing Project Docs" in content
        assert "Do not delete this." in content
        # Generated content should come before existing
        assert content.index("Global:") < content.index("# Existing Project Docs")

    def test_skips_when_empty(self, tmp_path, registry):
        """No CLAUDE.md created when all instruction layers are empty."""
        defaults = DefaultsConfig(
            max_agents_per_project=3,
            claude_command="echo",
            agent_instructions="",
        )
        mgr = AgentManager(registry=registry, defaults=defaults)

        worktree = tmp_path / "wt"
        worktree.mkdir()

        mgr._generate_claude_md(worktree, "test-project", profile=None)

        assert not (worktree / "CLAUDE.md").exists()

    def test_context_files_inlined(self, manager, tmp_path, tmp_git_repo):
        """Context files from the project are inlined into CLAUDE.md."""
        # Create a context file in the project
        docs_dir = tmp_git_repo / "docs"
        docs_dir.mkdir()
        arch_file = docs_dir / "ARCHITECTURE.md"
        arch_file.write_text("# Architecture\nThis is the architecture doc.")

        project = manager.registry.config.projects["test-project"]
        project.context_files = ["docs/ARCHITECTURE.md"]

        worktree = tmp_path / "wt"
        worktree.mkdir()

        manager._generate_claude_md(worktree, "test-project", profile=None)

        content = (worktree / "CLAUDE.md").read_text()
        assert "# Architecture" in content
        assert "This is the architecture doc." in content


class TestStartSequence:
    """Tests for profile-based start sequences."""

    @pytest.fixture
    def manager(self, registry):
        defaults = DefaultsConfig(
            max_agents_per_project=5,
            claude_command="echo",
            poll_interval_seconds=1.0,
        )
        return AgentManager(registry=registry, defaults=defaults)

    @pytest.mark.asyncio
    async def test_spawn_with_profile_stores_name(self, manager):
        """Spawning with a profile stores the profile name on the agent."""
        # Add a profile to config
        manager.registry.config.profiles["parallel"] = AgentProfile(
            description="Aggressive parallelization",
            system_prompt="Always decompose into parallel subagents.",
            instructions="Use agent teams for multi-file tasks.",
        )

        with (
            patch("subprocess.run") as mock_run,
            patch("agent_forge.tmux_utils.create_session", return_value=True) as mock_create,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            agent = await manager.spawn_agent("test-project", task="fix bug", profile="parallel")

        assert agent.profile == "parallel"
        assert agent.id in manager.agents

    @pytest.mark.asyncio
    async def test_system_prompt_in_command(self, manager):
        """Profile system_prompt should be passed via --append-system-prompt."""
        manager.registry.config.profiles["careful"] = AgentProfile(
            description="Plan first",
            system_prompt="Plan thoroughly before coding.",
        )

        with (
            patch("subprocess.run") as mock_run,
            patch("agent_forge.tmux_utils.create_session", return_value=True) as mock_create,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            await manager.spawn_agent("test-project", task="refactor", profile="careful")

        # The tmux command should include --append-system-prompt
        call_args = mock_create.call_args
        tmux_command = call_args[0][2]  # third positional arg
        assert "--append-system-prompt" in tmux_command
        assert "Plan thoroughly before coding." in tmux_command

    @pytest.mark.asyncio
    async def test_invalid_profile_raises(self, manager):
        """Spawning with a non-existent profile should raise ValueError."""
        with pytest.raises(ValueError, match="Profile not found"):
            with (
                patch("subprocess.run") as mock_run,
                patch("agent_forge.tmux_utils.create_session", return_value=True),
            ):
                mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                await manager.spawn_agent("test-project", task="task", profile="nonexistent")

    @pytest.mark.asyncio
    async def test_default_start_sequence(self, manager):
        """Without a profile, the default start sequence is wait 3s + send task."""
        steps = manager._get_start_sequence(profile=None, task="do something")
        assert len(steps) == 2
        assert steps[0].action == "wait"
        assert steps[0].value == "3"
        assert steps[1].action == "send"
        assert steps[1].value == "{task}"

    @pytest.mark.asyncio
    async def test_custom_start_sequence(self, manager):
        """Profile start_sequence overrides the default."""
        profile = AgentProfile(
            start_sequence=[
                StartSequenceStep(action="wait", value="5"),
                StartSequenceStep(action="wait_for_idle", value="60"),
                StartSequenceStep(action="send", value="Hello {task}"),
            ],
        )
        steps = manager._get_start_sequence(profile=profile, task="x")
        assert len(steps) == 3
        assert steps[0].action == "wait"
        assert steps[0].value == "5"
        assert steps[1].action == "wait_for_idle"
        assert steps[2].action == "send"
        assert steps[2].value == "Hello {task}"

    @pytest.mark.asyncio
    async def test_no_start_sequence_when_no_task(self, manager):
        """No task and no profile means empty start sequence."""
        steps = manager._get_start_sequence(profile=None, task="")
        assert steps == []


class TestComparisonMode:
    """Tests for comparison mode (A/B testing with multiple profiles)."""

    @pytest.fixture
    def manager(self, registry):
        defaults = DefaultsConfig(
            max_agents_per_project=10,
            claude_command="echo",
            poll_interval_seconds=1.0,
        )
        mgr = AgentManager(registry=registry, defaults=defaults)
        # Set up project max_agents to allow multiple
        registry.config.projects["test-project"].max_agents = 10
        return mgr

    @pytest.mark.asyncio
    async def test_spawns_correct_count(self, manager):
        """spawn_comparison spawns the specified number of agents."""
        manager.registry.config.profiles["a"] = AgentProfile(description="Profile A")
        manager.registry.config.profiles["b"] = AgentProfile(description="Profile B")

        with (
            patch("subprocess.run") as mock_run,
            patch("agent_forge.tmux_utils.create_session", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            agents = await manager.spawn_comparison("test-project", "fix bug", ["a", "b"])

        assert len(agents) == 2
        assert agents[0].profile == "a"
        assert agents[1].profile == "b"

    @pytest.mark.asyncio
    async def test_cycles_profiles(self, manager):
        """When count > len(profiles), profiles are cycled."""
        manager.registry.config.profiles["a"] = AgentProfile(description="A")
        manager.registry.config.profiles["b"] = AgentProfile(description="B")

        with (
            patch("subprocess.run") as mock_run,
            patch("agent_forge.tmux_utils.create_session", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            agents = await manager.spawn_comparison("test-project", "task", ["a", "b"], count=4)

        assert len(agents) == 4
        assert [a.profile for a in agents] == ["a", "b", "a", "b"]

    @pytest.mark.asyncio
    async def test_uses_compare_branch_prefix(self, manager):
        """Comparison agents use 'compare' as branch prefix."""
        manager.registry.config.profiles["a"] = AgentProfile(description="A")

        with (
            patch("subprocess.run") as mock_run,
            patch("agent_forge.tmux_utils.create_session", return_value=True),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            agents = await manager.spawn_comparison("test-project", "task", ["a"])

        assert agents[0].branch_name.startswith("compare/")

    @pytest.mark.asyncio
    async def test_empty_profiles_raises(self, manager):
        """spawn_comparison with empty profiles list raises ValueError."""
        with pytest.raises(ValueError, match="At least one profile"):
            await manager.spawn_comparison("test-project", "task", [])


class TestAttentionFields:
    """Tests for needs_attention and parked Agent fields."""

    def test_defaults(self):
        agent = Agent(
            id="abc123",
            project_name="test",
            session_name="forge__test__abc123",
            worktree_path="/tmp/wt",
            branch_name="agent/abc123/task",
        )
        assert agent.needs_attention is False
        assert agent.parked is False

    def test_explicit_values(self):
        agent = Agent(
            id="abc123",
            project_name="test",
            session_name="forge__test__abc123",
            worktree_path="/tmp/wt",
            branch_name="agent/abc123/task",
            needs_attention=True,
            parked=True,
        )
        assert agent.needs_attention is True
        assert agent.parked is True

    def test_mutation(self):
        agent = Agent(
            id="abc123",
            project_name="test",
            session_name="forge__test__abc123",
            worktree_path="/tmp/wt",
            branch_name="agent/abc123/task",
        )
        agent.needs_attention = True
        agent.parked = True
        assert agent.needs_attention is True
        assert agent.parked is True
        agent.needs_attention = False
        assert agent.needs_attention is False
