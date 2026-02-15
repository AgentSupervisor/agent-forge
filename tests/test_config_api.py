"""Tests for config management API endpoints."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from agent_forge.agent_manager import AgentManager
from agent_forge.config import DefaultsConfig, ForgeConfig, ProjectConfig
from agent_forge.main import app
from agent_forge.registry import ProjectRegistry
from agent_forge.websocket_manager import WebSocketManager


def _setup_app_state(config_file: str) -> None:
    """Manually configure app.state, bypassing the full lifespan.

    The real lifespan initialises the database, tmux recovery, status monitor,
    and telegram gateway -- none of which are needed for config API tests.
    Setting state manually avoids those heavy side-effects.
    """
    registry = ProjectRegistry(config_path=config_file)
    config = registry.config

    app.state.config_path = config_file
    app.state.config = config
    app.state.registry = registry
    app.state.db = AsyncMock()  # mock database connection
    app.state.agent_manager = AgentManager(registry, config.defaults)
    app.state.ws_manager = WebSocketManager()
    app.state.status_monitor = None
    app.state.telegram_gw = None
    app.state.started_at = time.time()


@pytest.fixture
def config_with_token(tmp_path, tmp_git_repo):
    """Write a config.yaml with a non-empty bot token for masking tests."""
    data = {
        "server": {"host": "127.0.0.1", "port": 9090, "secret_key": "test-secret"},
        "telegram": {"bot_token": "1234567890:ABCDEFghijklmn", "allowed_users": [111]},
        "defaults": {
            "max_agents_per_project": 3,
            "sandbox": True,
            "claude_command": "echo",
            "poll_interval_seconds": 1.0,
        },
        "projects": {
            "test-project": {
                "path": str(tmp_git_repo),
                "default_branch": "main",
                "max_agents": 2,
                "description": "Test project",
            }
        },
    }
    config_path = tmp_path / "config_token.yaml"
    with open(config_path, "w") as f:
        yaml.dump(data, f)
    return str(config_path)


@pytest.fixture
async def client(config_file):
    """Async test client with app.state set up from the temp config file.

    Bypasses the lifespan entirely to avoid database, tmux, and telegram
    side-effects. State is configured manually via _setup_app_state().
    """
    _setup_app_state(config_file)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def client_with_token(config_with_token):
    """Test client whose config has a real bot token for masking tests."""
    _setup_app_state(config_with_token)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestGetConfig:
    @pytest.mark.asyncio
    async def test_get_config_returns_config(self, client):
        """GET /api/config should return config with expected keys."""
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "server" in data
        assert "telegram" in data
        assert "defaults" in data
        assert "projects" in data
        assert "test-project" in data["projects"]

    @pytest.mark.asyncio
    async def test_get_config_masks_empty_token(self, client):
        """GET /api/config should leave an empty token as-is."""
        resp = await client.get("/api/config")
        data = resp.json()
        # The sample_config_dict has bot_token="" so it should remain empty
        assert data["telegram"]["bot_token"] == ""

    @pytest.mark.asyncio
    async def test_get_config_masks_nonempty_token(self, client_with_token):
        """GET /api/config should mask a non-empty bot token."""
        resp = await client_with_token.get("/api/config")
        data = resp.json()
        token = data["telegram"]["bot_token"]
        assert "***" in token
        # Should not contain the full original token
        assert token != "1234567890:ABCDEFghijklmn"
        # First 4 chars should be preserved
        assert token.startswith("1234")


class TestProjectsCRUD:
    @pytest.mark.asyncio
    async def test_create_project(self, client, tmp_git_repo):
        """POST /api/config/projects should create a new project."""
        with patch("agent_forge.main.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="main\n", stderr=""
            )
            resp = await client.post(
                "/api/config/projects",
                json={
                    "name": "new-project",
                    "path": str(tmp_git_repo),
                    "description": "A new project",
                },
            )

        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "created"
        assert data["name"] == "new-project"

        # Verify it shows up in GET /api/config
        resp2 = await client.get("/api/config")
        assert "new-project" in resp2.json()["projects"]

    @pytest.mark.asyncio
    async def test_create_project_rejects_duplicate(self, client, tmp_git_repo):
        """POST /api/config/projects should return 409 for duplicate names."""
        resp = await client.post(
            "/api/config/projects",
            json={
                "name": "test-project",
                "path": str(tmp_git_repo),
            },
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_update_project(self, client, tmp_git_repo):
        """PUT /api/config/projects/{name} should update project fields."""
        resp = await client.put(
            "/api/config/projects/test-project",
            json={
                "description": "Updated description",
                "default_branch": "develop",
                "max_agents": 10,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "updated"

        # Verify the changes persisted via GET /api/config
        resp2 = await client.get("/api/config")
        project = resp2.json()["projects"]["test-project"]
        assert project["description"] == "Updated description"
        assert project["default_branch"] == "develop"
        assert project["max_agents"] == 10

    @pytest.mark.asyncio
    async def test_update_nonexistent_project(self, client):
        """PUT /api/config/projects/{name} should return 404 for unknown project."""
        resp = await client.put(
            "/api/config/projects/nonexistent",
            json={"description": "nope"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_project(self, client):
        """DELETE /api/config/projects/{name} should remove a project."""
        resp = await client.delete("/api/config/projects/test-project")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "deleted"
        assert data["name"] == "test-project"

        # Verify it's gone
        resp2 = await client.get("/api/config")
        assert "test-project" not in resp2.json()["projects"]

    @pytest.mark.asyncio
    async def test_delete_project_rejects_when_active_agents(self, client):
        """DELETE /api/config/projects/{name} should return 409 when agents are active."""
        from agent_forge.agent_manager import Agent, AgentStatus

        mgr = app.state.agent_manager
        fake_agent = Agent(
            id="aaa111",
            project_name="test-project",
            session_name="forge__test-project__aaa111",
            worktree_path="/tmp/fake",
            branch_name="agent/aaa111/task",
            status=AgentStatus.WORKING,
        )
        mgr.agents["aaa111"] = fake_agent

        try:
            resp = await client.delete("/api/config/projects/test-project")
            assert resp.status_code == 409
            assert "active agent" in resp.json()["detail"]
        finally:
            # Clean up so other tests are unaffected
            mgr.agents.pop("aaa111", None)

    @pytest.mark.asyncio
    async def test_delete_nonexistent_project(self, client):
        """DELETE /api/config/projects/{name} should return 404 for unknown project."""
        resp = await client.delete("/api/config/projects/nonexistent")
        assert resp.status_code == 404


class TestDefaultsUpdate:
    @pytest.mark.asyncio
    async def test_update_defaults(self, client):
        """PUT /api/config/defaults should update default settings."""
        resp = await client.put(
            "/api/config/defaults",
            json={
                "max_agents_per_project": 10,
                "claude_command": "claude --model opus",
                "poll_interval_seconds": 5.0,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"

        # Verify the changes via GET /api/config
        resp2 = await client.get("/api/config")
        defaults = resp2.json()["defaults"]
        assert defaults["max_agents_per_project"] == 10
        assert defaults["claude_command"] == "claude --model opus"
        assert defaults["poll_interval_seconds"] == 5.0

    @pytest.mark.asyncio
    async def test_update_defaults_partial(self, client):
        """PUT /api/config/defaults with partial fields only changes those fields."""
        # First get current values
        resp1 = await client.get("/api/config")
        original_command = resp1.json()["defaults"]["claude_command"]

        # Update only max_agents_per_project
        resp = await client.put(
            "/api/config/defaults",
            json={"max_agents_per_project": 7},
        )
        assert resp.status_code == 200

        # Verify only max_agents changed, command is preserved
        resp2 = await client.get("/api/config")
        defaults = resp2.json()["defaults"]
        assert defaults["max_agents_per_project"] == 7
        assert defaults["claude_command"] == original_command


class TestTelegramUpdate:
    @pytest.mark.asyncio
    async def test_update_telegram(self, client):
        """PUT /api/config/telegram should update telegram settings."""
        resp = await client.put(
            "/api/config/telegram",
            json={
                "bot_token": "new-token-value-here",
                "allowed_users": [123, 456],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"

        # Verify changes via GET /api/config (token should be masked)
        resp2 = await client.get("/api/config")
        telegram = resp2.json()["telegram"]
        assert telegram["allowed_users"] == [123, 456]
        # Token should be masked since it's non-empty
        assert "***" in telegram["bot_token"]

    @pytest.mark.asyncio
    async def test_update_telegram_empty_token(self, client):
        """PUT /api/config/telegram with empty token should clear it."""
        resp = await client.put(
            "/api/config/telegram",
            json={"bot_token": "", "allowed_users": []},
        )
        assert resp.status_code == 200

        resp2 = await client.get("/api/config")
        telegram = resp2.json()["telegram"]
        assert telegram["bot_token"] == ""
        assert telegram["allowed_users"] == []


class TestHooksEvent:
    @pytest.mark.asyncio
    async def test_subagent_start_increments_count(self, client):
        """SubagentStart hook increments sub_agent_count."""
        from agent_forge.main import app
        mgr = app.state.agent_manager
        # Inject a fake agent
        from agent_forge.agent_manager import Agent, AgentStatus
        agent = Agent(id="hook01", project_name="test-project", session_name="forge__test__hook01",
                      worktree_path="/tmp/wt", branch_name="agent/hook01/test")
        mgr.agents["hook01"] = agent
        try:
            resp = await client.post("/api/hooks/event", json={"agent_id": "hook01", "hook_event": "SubagentStart"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["sub_agent_count"] == 1

            # Second sub-agent
            resp = await client.post("/api/hooks/event", json={"agent_id": "hook01", "hook_event": "SubagentStart"})
            assert resp.json()["sub_agent_count"] == 2
        finally:
            mgr.agents.pop("hook01", None)

    @pytest.mark.asyncio
    async def test_subagent_stop_decrements_count(self, client):
        """SubagentStop hook decrements sub_agent_count."""
        from agent_forge.main import app
        from agent_forge.agent_manager import Agent
        mgr = app.state.agent_manager
        agent = Agent(id="hook02", project_name="test-project", session_name="forge__test__hook02",
                      worktree_path="/tmp/wt", branch_name="agent/hook02/test", sub_agent_count=3)
        mgr.agents["hook02"] = agent
        try:
            resp = await client.post("/api/hooks/event", json={"agent_id": "hook02", "hook_event": "SubagentStop"})
            assert resp.status_code == 200
            assert resp.json()["sub_agent_count"] == 2
        finally:
            mgr.agents.pop("hook02", None)

    @pytest.mark.asyncio
    async def test_subagent_stop_does_not_go_negative(self, client):
        """SubagentStop never makes count negative."""
        from agent_forge.main import app
        from agent_forge.agent_manager import Agent
        mgr = app.state.agent_manager
        agent = Agent(id="hook03", project_name="test-project", session_name="forge__test__hook03",
                      worktree_path="/tmp/wt", branch_name="agent/hook03/test", sub_agent_count=0)
        mgr.agents["hook03"] = agent
        try:
            resp = await client.post("/api/hooks/event", json={"agent_id": "hook03", "hook_event": "SubagentStop"})
            assert resp.json()["sub_agent_count"] == 0
        finally:
            mgr.agents.pop("hook03", None)

    @pytest.mark.asyncio
    async def test_hook_event_unknown_agent_ignored(self, client):
        """Hook events for unknown agents are gracefully ignored."""
        resp = await client.post("/api/hooks/event", json={"agent_id": "nonexistent", "hook_event": "SubagentStart"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"

    @pytest.mark.asyncio
    async def test_hook_event_unknown_event_type(self, client):
        """Unknown event types don't crash, just return ok."""
        from agent_forge.main import app
        from agent_forge.agent_manager import Agent
        mgr = app.state.agent_manager
        agent = Agent(id="hook04", project_name="test-project", session_name="forge__test__hook04",
                      worktree_path="/tmp/wt", branch_name="agent/hook04/test")
        mgr.agents["hook04"] = agent
        try:
            resp = await client.post("/api/hooks/event", json={"agent_id": "hook04", "hook_event": "SomeOtherEvent"})
            assert resp.status_code == 200
            assert resp.json()["sub_agent_count"] == 0
        finally:
            mgr.agents.pop("hook04", None)
