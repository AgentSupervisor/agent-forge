"""TelegramGateway — stateless Telegram bot that relays messages to Claude Code agents."""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

from telegram import Update, Bot
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

STATUS_EMOJI = {
    "starting": "\u23f3",
    "working": "\ud83d\udee0",
    "waiting_input": "\u2753",
    "idle": "\ud83d\udca4",
    "stopped": "\u26d4",
    "error": "\u274c",
}


class TelegramGateway:
    """Stateless Telegram bot that relays messages to Claude Code agents."""

    def __init__(
        self,
        agent_manager,
        media_handler,
        bot_token: str,
        allowed_users: list[int],
    ):
        self.agent_manager = agent_manager
        self.media_handler = media_handler
        self.bot_token = bot_token
        self.allowed_users = allowed_users
        self._app: Application | None = None

    async def start(self):
        """Build and start the Telegram bot."""
        self._app = Application.builder().token(self.bot_token).build()

        # Register handlers
        self._app.add_handler(CommandHandler("status", self._handle_status))
        self._app.add_handler(CommandHandler("spawn", self._handle_spawn))
        self._app.add_handler(CommandHandler("kill", self._handle_kill))
        self._app.add_handler(CommandHandler("projects", self._handle_projects))
        self._app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._handle_message,
            )
        )
        self._app.add_handler(
            MessageHandler(
                filters.PHOTO
                | filters.VIDEO
                | filters.AUDIO
                | filters.Document.ALL
                | filters.VOICE,
                self._handle_media_message,
            )
        )

        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling()

    async def stop(self):
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

    # ------------------------------------------------------------------
    # Authorization
    # ------------------------------------------------------------------

    def _check_authorized(self, user_id: int) -> bool:
        """Empty allowed_users = allow all."""
        if not self.allowed_users:
            return True
        return user_id in self.allowed_users

    # ------------------------------------------------------------------
    # Message parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_target(text: str) -> tuple[str, str | None, str] | None:
        """Parse ``@project message`` or ``@project:agent_id message``.

        Returns ``(project_name, agent_id | None, message)`` or ``None``
        if the text does not match the expected prefix format.
        """
        match = re.match(r"^@([\w-]+)(?::([\w-]+))?[:\s]\s*(.*)", text, re.DOTALL)
        if not match:
            return None
        project_name = match.group(1)
        agent_id = match.group(2)  # may be None
        message = match.group(3).strip()
        return project_name, agent_id, message

    # ------------------------------------------------------------------
    # Smart routing
    # ------------------------------------------------------------------

    async def _smart_route(
        self, project_name: str, message: str, update: Update
    ) -> tuple[object | None, bool]:
        """Smart load balancer: find an available agent or spawn a new one.

        Returns (agent, newly_spawned) or (None, False) if routing failed.
        """
        from .agent_manager import AgentStatus

        agents = self.agent_manager.list_agents(project_name=project_name)
        active_agents = [a for a in agents if a.status != AgentStatus.STOPPED]

        # No agents → auto-spawn
        if not active_agents:
            try:
                agent = await self.agent_manager.spawn_agent(
                    project_name, task=message
                )
                return agent, True
            except RuntimeError as exc:
                await update.message.reply_text(f"Failed to spawn agent: {exc}")
                return None, False

        # Prefer IDLE agents
        idle_agents = [a for a in active_agents if a.status == AgentStatus.IDLE]
        if idle_agents:
            agent = max(idle_agents, key=lambda a: a.last_activity)
            await self.agent_manager.clear_context(agent.id)
            agent.task_description = message[:200]
            return agent, False

        # All busy — spawn if under limit
        config = self.agent_manager.registry.config
        max_agents = config.get_max_agents(project_name)
        if len(active_agents) < max_agents:
            try:
                agent = await self.agent_manager.spawn_agent(
                    project_name, task=message
                )
                return agent, True
            except RuntimeError as exc:
                await update.message.reply_text(f"Failed to spawn agent: {exc}")
                return None, False

        # At limit and all busy
        busy_list = "\n".join(
            f"  [{a.status.value}] `{a.id}`"
            + (f" — {a.task_description}" if a.task_description else "")
            for a in active_agents
        )
        await update.message.reply_text(
            f"All agents for *{project_name}* are busy"
            f" ({len(active_agents)}/{max_agents}):\n{busy_list}",
            parse_mode="Markdown",
        )
        return None, False

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _handle_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._check_authorized(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return

        grouped = self.agent_manager.get_agents_by_project()
        if not grouped:
            await update.message.reply_text("No active agents.")
            return

        lines: list[str] = []
        for project, agents in grouped.items():
            lines.append(f"\ud83d\udcc1 *{project}*")
            for agent in agents:
                emoji = STATUS_EMOJI.get(agent.status.value, "\u2753")
                task_info = f" — {agent.task_description}" if agent.task_description else ""
                lines.append(f"  {emoji} `{agent.id}` {agent.status.value}{task_info}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    async def _handle_spawn(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._check_authorized(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return

        args = context.args or []
        if not args:
            await update.message.reply_text(
                "Usage: /spawn {project} [task description]"
            )
            return

        project_name = args[0]
        task = " ".join(args[1:]) if len(args) > 1 else ""

        # Validate project exists
        projects = self.agent_manager.registry.list_projects()
        if project_name not in projects:
            available = ", ".join(sorted(projects.keys()))
            await update.message.reply_text(
                f"Unknown project: '{project_name}'\nAvailable: {available}"
            )
            return

        try:
            agent = await self.agent_manager.spawn_agent(project_name, task=task)
            await update.message.reply_text(
                f"Spawned agent `{agent.id}` for *{project_name}*"
                + (f"\nTask: {task}" if task else ""),
                parse_mode="Markdown",
            )
        except RuntimeError as exc:
            await update.message.reply_text(f"Failed to spawn agent: {exc}")

    async def _handle_kill(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._check_authorized(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return

        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /kill {agent_id}")
            return

        agent_id = args[0]
        success = await self.agent_manager.kill_agent(agent_id)
        if success:
            await update.message.reply_text(f"Agent `{agent_id}` killed.")
        else:
            await update.message.reply_text(f"Agent `{agent_id}` not found.")

    async def _handle_projects(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._check_authorized(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return

        projects = self.agent_manager.registry.list_projects()
        if not projects:
            await update.message.reply_text("No projects registered.")
            return

        lines: list[str] = []
        for name, project in sorted(projects.items()):
            desc = f" — {project.description}" if project.description else ""
            lines.append(f"\u2022 *{name}*{desc}")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

    # ------------------------------------------------------------------
    # Text message handler
    # ------------------------------------------------------------------

    async def _handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._check_authorized(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return

        text = update.message.text or ""
        parsed = self._parse_target(text)

        if parsed is None:
            await update.message.reply_text(
                "Usage: @{project} {message}\n"
                "Or: @{project}:{agent_id} {message}"
            )
            return

        project_name, agent_id, message = parsed

        # Validate project
        projects = self.agent_manager.registry.list_projects()
        if project_name not in projects:
            available = ", ".join(sorted(projects.keys()))
            await update.message.reply_text(
                f"Unknown project: '{project_name}'\nAvailable: {available}"
            )
            return

        # Resolve agent_id
        if agent_id:
            agent = self.agent_manager.get_agent(agent_id)
            if not agent:
                await update.message.reply_text(f"Agent `{agent_id}` not found.")
                return
            success = await self.agent_manager.send_message(agent.id, message)
            if success:
                await update.message.reply_text(
                    f"Sent to `{agent.id}` ({project_name})",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(f"Failed to send message to `{agent.id}`.")
            return

        # Smart routing: find or spawn an agent
        agent, newly_spawned = await self._smart_route(
            project_name, message, update
        )
        if agent is None:
            return

        if newly_spawned:
            await update.message.reply_text(
                f"Spawned agent `{agent.id}` for *{project_name}*",
                parse_mode="Markdown",
            )
        else:
            success = await self.agent_manager.send_message(agent.id, message)
            if success:
                await update.message.reply_text(
                    f"Sent to `{agent.id}` ({project_name})",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(f"Failed to send message to `{agent.id}`.")

    # ------------------------------------------------------------------
    # Media message handler
    # ------------------------------------------------------------------

    async def _handle_media_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._check_authorized(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return

        # Extract caption for routing
        caption = update.message.caption or ""
        parsed = self._parse_target(caption)

        if parsed is None:
            await update.message.reply_text(
                "Add a caption with @{project} {message} to route media to an agent."
            )
            return

        project_name, agent_id, message = parsed

        # Validate project
        projects = self.agent_manager.registry.list_projects()
        if project_name not in projects:
            available = ", ".join(sorted(projects.keys()))
            await update.message.reply_text(
                f"Unknown project: '{project_name}'\nAvailable: {available}"
            )
            return

        # Resolve agent
        if agent_id:
            agent = self.agent_manager.get_agent(agent_id)
            if not agent:
                await update.message.reply_text(f"Agent `{agent_id}` not found.")
                return
        else:
            agent, newly_spawned = await self._smart_route(
                project_name, message, update
            )
            if agent is None:
                return

        # Download attachment to temp directory
        try:
            file_obj = None
            if update.message.photo:
                file_obj = await update.message.photo[-1].get_file()
            elif update.message.video:
                file_obj = await update.message.video.get_file()
            elif update.message.audio:
                file_obj = await update.message.audio.get_file()
            elif update.message.voice:
                file_obj = await update.message.voice.get_file()
            elif update.message.document:
                file_obj = await update.message.document.get_file()

            if not file_obj:
                await update.message.reply_text("Could not process attachment.")
                return

            # Download to temp dir
            tmp_dir = tempfile.mkdtemp(prefix="forge_media_")
            file_name = Path(file_obj.file_path).name if file_obj.file_path else "attachment"
            tmp_path = Path(tmp_dir) / file_name
            await file_obj.download_to_drive(str(tmp_path))

            # Process and stage via media handler
            staged_paths = await self.media_handler.process_and_stage(
                source_path=str(tmp_path),
                worktree_path=agent.worktree_path,
            )

            # Send message to agent referencing the files
            await self.agent_manager.send_message_with_media(
                agent.id, message, staged_paths
            )

            file_list = "\n".join(f"  - {p}" for p in staged_paths)
            await update.message.reply_text(
                f"Staged to `{agent.id}` ({project_name}):\n{file_list}",
                parse_mode="Markdown",
            )
        except Exception:
            logger.exception("Failed to process media message")
            await update.message.reply_text("Failed to process media attachment.")
