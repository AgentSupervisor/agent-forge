"""Tests for connector CRUD and channel binding API endpoints."""

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from agent_forge.agent_manager import AgentManager
from agent_forge.config import (
    ChannelBinding,
    ConnectorConfig,
    ForgeConfig,
    ProjectConfig,
)
from agent_forge.main import app
from agent_forge.registry import ProjectRegistry
from agent_forge.websocket_manager import WebSocketManager


def _setup_app_state(config_file: str) -> None:
    """Set up app.state with connector_manager support."""
    registry = ProjectRegistry(config_path=config_file)
    config = registry.config

    app.state.config_path = config_file
    app.state.config = config
    app.state.registry = registry
    app.state.db = AsyncMock()
    app.state.agent_manager = AgentManager(registry, config.defaults)
    app.state.ws_manager = WebSocketManager()
    app.state.status_monitor = None
    app.state.connector_manager = None
    app.state.started_at = time.time()


@pytest.fixture
def config_with_connectors(tmp_path, tmp_git_repo):
    data = {
        "server": {"host": "127.0.0.1", "port": 9090, "secret_key": "test"},
        "telegram": {"bot_token": "", "allowed_users": []},
        "connectors": {
            "my-tg": {
                "type": "telegram",
                "enabled": True,
                "credentials": {"bot_token": "1234567890:ABCDEF"},
                "settings": {"allowed_users": [111]},
            },
        },
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
                "channels": [
                    {
                        "connector_id": "my-tg",
                        "channel_id": "-100999",
                        "channel_name": "Dev Chat",
                        "inbound": True,
                        "outbound": True,
                    }
                ],
            },
        },
    }
    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(data, f)
    return str(config_path)


@pytest.fixture
async def client(config_with_connectors):
    _setup_app_state(config_with_connectors)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestListConnectors:
    @pytest.mark.asyncio
    async def test_lists_connectors(self, client):
        resp = await client.get("/api/config/connectors")
        assert resp.status_code == 200
        data = resp.json()
        assert "my-tg" in data
        assert data["my-tg"]["type"] == "telegram"
        assert data["my-tg"]["enabled"] is True

    @pytest.mark.asyncio
    async def test_masks_credentials(self, client):
        resp = await client.get("/api/config/connectors")
        data = resp.json()
        token = data["my-tg"]["credentials"]["bot_token"]
        assert "***" in token
        assert token != "1234567890:ABCDEF"


class TestAddConnector:
    @pytest.mark.asyncio
    async def test_add_connector(self, client):
        resp = await client.post(
            "/api/config/connectors",
            json={
                "id": "my-discord",
                "type": "discord",
                "enabled": True,
                "credentials": {"bot_token": "MTIz-fake"},
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "created"
        assert data["id"] == "my-discord"

        # Verify it shows up
        resp2 = await client.get("/api/config/connectors")
        assert "my-discord" in resp2.json()

    @pytest.mark.asyncio
    async def test_add_duplicate_connector(self, client):
        resp = await client.post(
            "/api/config/connectors",
            json={"id": "my-tg", "type": "telegram"},
        )
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_add_invalid_type(self, client):
        resp = await client.post(
            "/api/config/connectors",
            json={"id": "bad", "type": "icq"},
        )
        assert resp.status_code == 400
        assert "Invalid type" in resp.json()["detail"]


class TestUpdateConnector:
    @pytest.mark.asyncio
    async def test_update_connector(self, client):
        resp = await client.put(
            "/api/config/connectors/my-tg",
            json={"enabled": False},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"

        resp2 = await client.get("/api/config/connectors")
        assert resp2.json()["my-tg"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_update_nonexistent(self, client):
        resp = await client.put(
            "/api/config/connectors/nonexistent",
            json={"enabled": False},
        )
        assert resp.status_code == 404


class TestDeleteConnector:
    @pytest.mark.asyncio
    async def test_delete_connector(self, client):
        resp = await client.delete("/api/config/connectors/my-tg")
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

        resp2 = await client.get("/api/config/connectors")
        assert "my-tg" not in resp2.json()

    @pytest.mark.asyncio
    async def test_delete_removes_channel_bindings(self, client):
        """Deleting a connector should remove channel bindings that reference it."""
        resp = await client.delete("/api/config/connectors/my-tg")
        assert resp.status_code == 200

        # Check the project no longer has channel bindings
        resp2 = await client.get("/api/config")
        project = resp2.json()["projects"]["test-project"]
        assert len(project["channels"]) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, client):
        resp = await client.delete("/api/config/connectors/nonexistent")
        assert resp.status_code == 404


class TestChannelBindings:
    @pytest.mark.asyncio
    async def test_add_channel_binding(self, client):
        resp = await client.post(
            "/api/config/projects/test-project/channels",
            json={
                "connector_id": "my-tg",
                "channel_id": "-100888",
                "channel_name": "Second Channel",
                "inbound": True,
                "outbound": False,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["status"] == "created"
        assert data["channel_count"] == 2  # original + new

    @pytest.mark.asyncio
    async def test_add_binding_invalid_connector(self, client):
        resp = await client.post(
            "/api/config/projects/test-project/channels",
            json={
                "connector_id": "nonexistent-connector",
                "channel_id": "123",
            },
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_add_binding_invalid_project(self, client):
        resp = await client.post(
            "/api/config/projects/nonexistent/channels",
            json={"connector_id": "my-tg", "channel_id": "123"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_remove_channel_binding(self, client):
        resp = await client.delete("/api/config/projects/test-project/channels/0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "deleted"
        assert data["channel_count"] == 0

    @pytest.mark.asyncio
    async def test_remove_out_of_range(self, client):
        resp = await client.delete("/api/config/projects/test-project/channels/99")
        assert resp.status_code == 404


class TestGetConfigWithConnectors:
    @pytest.mark.asyncio
    async def test_config_includes_connectors(self, client):
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "connectors" in data
        assert "my-tg" in data["connectors"]
        # Token should be masked
        assert "***" in data["connectors"]["my-tg"]["credentials"]["bot_token"]

    @pytest.mark.asyncio
    async def test_config_includes_channels(self, client):
        resp = await client.get("/api/config")
        data = resp.json()
        channels = data["projects"]["test-project"]["channels"]
        assert len(channels) == 1
        assert channels[0]["connector_id"] == "my-tg"
        assert channels[0]["channel_id"] == "-100999"
