"""Tests for TelegramGateway — authorization, parsing, command handlers."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_forge.telegram_gateway import TelegramGateway


@pytest.fixture
def mock_agent_manager():
    manager = MagicMock()
    manager.registry = MagicMock()
    manager.spawn_agent = AsyncMock()
    manager.kill_agent = AsyncMock()
    manager.send_message = AsyncMock(return_value=True)
    manager.send_message_with_media = AsyncMock(return_value=True)
    manager.get_agent = MagicMock(return_value=None)
    manager.list_agents = MagicMock(return_value=[])
    manager.get_agents_by_project = MagicMock(return_value={})
    return manager


@pytest.fixture
def mock_media_handler():
    handler = MagicMock()
    handler.process_and_stage = AsyncMock(return_value=[".media/photo.jpg"])
    return handler


@pytest.fixture
def gateway(mock_agent_manager, mock_media_handler):
    return TelegramGateway(
        agent_manager=mock_agent_manager,
        media_handler=mock_media_handler,
        bot_token="test-token",
        allowed_users=[111, 222],
    )


@pytest.fixture
def gateway_open(mock_agent_manager, mock_media_handler):
    """Gateway with empty allowed_users (allow all)."""
    return TelegramGateway(
        agent_manager=mock_agent_manager,
        media_handler=mock_media_handler,
        bot_token="test-token",
        allowed_users=[],
    )


def _make_update(user_id: int = 111, text: str = "", caption: str | None = None):
    """Create a mock Telegram Update with message and user."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.message = MagicMock()
    update.message.text = text
    update.message.caption = caption
    update.message.reply_text = AsyncMock()
    update.message.photo = None
    update.message.video = None
    update.message.audio = None
    update.message.voice = None
    update.message.document = None
    return update


def _make_context(args: list[str] | None = None):
    """Create a mock CallbackContext."""
    context = MagicMock()
    context.args = args
    return context


# ------------------------------------------------------------------
# Authorization
# ------------------------------------------------------------------


class TestCheckAuthorized:
    def test_check_authorized_empty_list(self, gateway_open):
        assert gateway_open._check_authorized(999) is True

    def test_check_authorized_allowed_user(self, gateway):
        assert gateway._check_authorized(111) is True

    def test_check_authorized_denied_user(self, gateway):
        assert gateway._check_authorized(999) is False


# ------------------------------------------------------------------
# Message parsing
# ------------------------------------------------------------------


class TestParseTarget:
    def test_parse_message_project_prefix(self):
        result = TelegramGateway._parse_target("@my-project fix the login bug")
        assert result is not None
        project, agent_id, message = result
        assert project == "my-project"
        assert agent_id is None
        assert message == "fix the login bug"

    def test_parse_message_project_agent_prefix(self):
        result = TelegramGateway._parse_target("@my-project:abc123 deploy it")
        assert result is not None
        project, agent_id, message = result
        assert project == "my-project"
        assert agent_id == "abc123"
        assert message == "deploy it"

    def test_parse_message_no_prefix(self):
        result = TelegramGateway._parse_target("just a plain message")
        assert result is None

    def test_parse_message_at_without_space(self):
        result = TelegramGateway._parse_target("@project")
        assert result is None

    def test_parse_message_multiline(self):
        result = TelegramGateway._parse_target("@proj do this\nand that")
        assert result is not None
        _, _, message = result
        assert "and that" in message


# ------------------------------------------------------------------
# Command handlers
# ------------------------------------------------------------------


class TestHandleStatus:
    @pytest.mark.asyncio
    async def test_unauthorized(self, gateway):
        update = _make_update(user_id=999)
        await gateway._handle_status(update, _make_context())
        update.message.reply_text.assert_awaited_once_with("Not authorized.")

    @pytest.mark.asyncio
    async def test_no_agents(self, gateway):
        update = _make_update(user_id=111)
        await gateway._handle_status(update, _make_context())
        update.message.reply_text.assert_awaited_once_with("No active agents.")

    @pytest.mark.asyncio
    async def test_with_agents(self, gateway, mock_agent_manager):
        agent = MagicMock()
        agent.id = "abc123"
        agent.status = MagicMock()
        agent.status.value = "working"
        agent.task_description = "fix bug"
        mock_agent_manager.get_agents_by_project.return_value = {
            "test-proj": [agent]
        }

        update = _make_update(user_id=111)
        await gateway._handle_status(update, _make_context())

        reply = update.message.reply_text.call_args[0][0]
        assert "test-proj" in reply
        assert "abc123" in reply
        assert "working" in reply


class TestHandleSpawn:
    @pytest.mark.asyncio
    async def test_unauthorized(self, gateway):
        update = _make_update(user_id=999)
        await gateway._handle_spawn(update, _make_context(args=["proj"]))
        update.message.reply_text.assert_awaited_once_with("Not authorized.")

    @pytest.mark.asyncio
    async def test_no_args(self, gateway):
        update = _make_update(user_id=111)
        await gateway._handle_spawn(update, _make_context(args=[]))
        reply = update.message.reply_text.call_args[0][0]
        assert "Usage" in reply

    @pytest.mark.asyncio
    async def test_unknown_project(self, gateway, mock_agent_manager):
        mock_agent_manager.registry.list_projects.return_value = {"real-proj": MagicMock()}
        update = _make_update(user_id=111)
        await gateway._handle_spawn(update, _make_context(args=["fake-proj"]))
        reply = update.message.reply_text.call_args[0][0]
        assert "Unknown project" in reply
        assert "real-proj" in reply

    @pytest.mark.asyncio
    async def test_spawn_success(self, gateway, mock_agent_manager):
        mock_agent_manager.registry.list_projects.return_value = {"proj": MagicMock()}
        agent = MagicMock()
        agent.id = "x1y2z3"
        mock_agent_manager.spawn_agent.return_value = agent

        update = _make_update(user_id=111)
        await gateway._handle_spawn(update, _make_context(args=["proj", "fix", "bug"]))

        mock_agent_manager.spawn_agent.assert_awaited_once_with("proj", task="fix bug")
        reply = update.message.reply_text.call_args[0][0]
        assert "x1y2z3" in reply

    @pytest.mark.asyncio
    async def test_spawn_failure(self, gateway, mock_agent_manager):
        mock_agent_manager.registry.list_projects.return_value = {"proj": MagicMock()}
        mock_agent_manager.spawn_agent.side_effect = RuntimeError("limit reached")

        update = _make_update(user_id=111)
        await gateway._handle_spawn(update, _make_context(args=["proj"]))
        reply = update.message.reply_text.call_args[0][0]
        assert "Failed" in reply


class TestHandleKill:
    @pytest.mark.asyncio
    async def test_unauthorized(self, gateway):
        update = _make_update(user_id=999)
        await gateway._handle_kill(update, _make_context(args=["abc"]))
        update.message.reply_text.assert_awaited_once_with("Not authorized.")

    @pytest.mark.asyncio
    async def test_no_args(self, gateway):
        update = _make_update(user_id=111)
        await gateway._handle_kill(update, _make_context(args=[]))
        reply = update.message.reply_text.call_args[0][0]
        assert "Usage" in reply

    @pytest.mark.asyncio
    async def test_kill_success(self, gateway, mock_agent_manager):
        mock_agent_manager.kill_agent.return_value = True
        update = _make_update(user_id=111)
        await gateway._handle_kill(update, _make_context(args=["abc123"]))
        reply = update.message.reply_text.call_args[0][0]
        assert "killed" in reply

    @pytest.mark.asyncio
    async def test_kill_not_found(self, gateway, mock_agent_manager):
        mock_agent_manager.kill_agent.return_value = False
        update = _make_update(user_id=111)
        await gateway._handle_kill(update, _make_context(args=["abc123"]))
        reply = update.message.reply_text.call_args[0][0]
        assert "not found" in reply


class TestHandleProjects:
    @pytest.mark.asyncio
    async def test_unauthorized(self, gateway):
        update = _make_update(user_id=999)
        await gateway._handle_projects(update, _make_context())
        update.message.reply_text.assert_awaited_once_with("Not authorized.")

    @pytest.mark.asyncio
    async def test_no_projects(self, gateway, mock_agent_manager):
        mock_agent_manager.registry.list_projects.return_value = {}
        update = _make_update(user_id=111)
        await gateway._handle_projects(update, _make_context())
        reply = update.message.reply_text.call_args[0][0]
        assert "No projects" in reply

    @pytest.mark.asyncio
    async def test_list_projects(self, gateway, mock_agent_manager):
        proj = MagicMock()
        proj.description = "My API"
        mock_agent_manager.registry.list_projects.return_value = {"api": proj}
        update = _make_update(user_id=111)
        await gateway._handle_projects(update, _make_context())
        reply = update.message.reply_text.call_args[0][0]
        assert "api" in reply
        assert "My API" in reply


# ------------------------------------------------------------------
# Text message handler
# ------------------------------------------------------------------


class TestHandleMessage:
    @pytest.mark.asyncio
    async def test_unauthorized(self, gateway):
        update = _make_update(user_id=999, text="@proj hello")
        await gateway._handle_message(update, _make_context())
        update.message.reply_text.assert_awaited_once_with("Not authorized.")

    @pytest.mark.asyncio
    async def test_no_prefix(self, gateway):
        update = _make_update(user_id=111, text="just some text")
        await gateway._handle_message(update, _make_context())
        reply = update.message.reply_text.call_args[0][0]
        assert "Usage" in reply

    @pytest.mark.asyncio
    async def test_unknown_project(self, gateway, mock_agent_manager):
        mock_agent_manager.registry.list_projects.return_value = {"real": MagicMock()}
        update = _make_update(user_id=111, text="@fake hello")
        await gateway._handle_message(update, _make_context())
        reply = update.message.reply_text.call_args[0][0]
        assert "Unknown project" in reply

    @pytest.mark.asyncio
    async def test_no_agents_for_project(self, gateway, mock_agent_manager):
        mock_agent_manager.registry.list_projects.return_value = {"proj": MagicMock()}
        mock_agent_manager.list_agents.return_value = []
        update = _make_update(user_id=111, text="@proj do something")
        await gateway._handle_message(update, _make_context())
        reply = update.message.reply_text.call_args[0][0]
        assert "No active agents" in reply

    @pytest.mark.asyncio
    async def test_route_to_most_recent_agent(self, gateway, mock_agent_manager):
        mock_agent_manager.registry.list_projects.return_value = {"proj": MagicMock()}

        old_agent = MagicMock()
        old_agent.id = "old"
        old_agent.last_activity = datetime(2024, 1, 1)

        new_agent = MagicMock()
        new_agent.id = "new"
        new_agent.last_activity = datetime(2024, 6, 1)

        mock_agent_manager.list_agents.return_value = [old_agent, new_agent]

        update = _make_update(user_id=111, text="@proj fix it")
        await gateway._handle_message(update, _make_context())

        mock_agent_manager.send_message.assert_awaited_once_with("new", "fix it")

    @pytest.mark.asyncio
    async def test_route_to_specific_agent(self, gateway, mock_agent_manager):
        mock_agent_manager.registry.list_projects.return_value = {"proj": MagicMock()}
        agent = MagicMock()
        agent.id = "abc123"
        mock_agent_manager.get_agent.return_value = agent

        update = _make_update(user_id=111, text="@proj:abc123 do this")
        await gateway._handle_message(update, _make_context())

        mock_agent_manager.send_message.assert_awaited_once_with("abc123", "do this")

    @pytest.mark.asyncio
    async def test_specific_agent_not_found(self, gateway, mock_agent_manager):
        mock_agent_manager.registry.list_projects.return_value = {"proj": MagicMock()}
        mock_agent_manager.get_agent.return_value = None

        update = _make_update(user_id=111, text="@proj:nope hello")
        await gateway._handle_message(update, _make_context())
        reply = update.message.reply_text.call_args[0][0]
        assert "not found" in reply


# ------------------------------------------------------------------
# Media message handler
# ------------------------------------------------------------------


class TestHandleMediaMessage:
    @pytest.mark.asyncio
    async def test_unauthorized(self, gateway):
        update = _make_update(user_id=999, caption="@proj look at this")
        await gateway._handle_media_message(update, _make_context())
        update.message.reply_text.assert_awaited_once_with("Not authorized.")

    @pytest.mark.asyncio
    async def test_no_caption(self, gateway):
        update = _make_update(user_id=111, caption="")
        await gateway._handle_media_message(update, _make_context())
        reply = update.message.reply_text.call_args[0][0]
        assert "caption" in reply.lower()

    @pytest.mark.asyncio
    async def test_media_photo_success(
        self, gateway, mock_agent_manager, mock_media_handler
    ):
        mock_agent_manager.registry.list_projects.return_value = {"proj": MagicMock()}
        agent = MagicMock()
        agent.id = "abc123"
        agent.worktree_path = "/tmp/worktree"
        agent.last_activity = datetime(2024, 6, 1)
        mock_agent_manager.list_agents.return_value = [agent]

        file_obj = AsyncMock()
        file_obj.file_path = "photos/file_1.jpg"
        file_obj.download_to_drive = AsyncMock()

        photo = MagicMock()
        photo.get_file = AsyncMock(return_value=file_obj)

        update = _make_update(user_id=111, caption="@proj check this screenshot")
        update.message.photo = [photo]

        await gateway._handle_media_message(update, _make_context())

        mock_media_handler.process_and_stage.assert_awaited_once()
        mock_agent_manager.send_message_with_media.assert_awaited_once()
        reply = update.message.reply_text.call_args[0][0]
        assert "Staged" in reply
