"""ConnectorManager — lifecycle, routing, and outbound for all IM connectors."""

from __future__ import annotations

import asyncio
import importlib
import logging
import shutil
from pathlib import Path
from typing import Any

from .base import ActionButton, BaseConnector, ConnectorType, InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)

# Maps connector type string to (module_path, class_name)
_CONNECTOR_REGISTRY: dict[str, tuple[str, str]] = {
    "telegram": ("agent_forge.connectors.telegram", "TelegramConnector"),
    "discord": ("agent_forge.connectors.discord", "DiscordConnector"),
    "slack": ("agent_forge.connectors.slack", "SlackConnector"),
    "whatsapp": ("agent_forge.connectors.whatsapp", "WhatsAppConnector"),
    "signal": ("agent_forge.connectors.signal", "SignalConnector"),
}


class ConnectorManager:
    """Manages lifecycle, inbound routing, and outbound delivery for all connectors."""

    def __init__(
        self,
        agent_manager: Any,
        media_handler: Any,
        config: Any,
        registry: Any = None,
    ) -> None:
        self.agent_manager = agent_manager
        self.media_handler = media_handler
        self._config = config
        self._registry = registry
        self.connectors: dict[str, BaseConnector] = {}
        # (connector_id, channel_id) -> list of (project_name, binding)
        self._channel_map: dict[tuple[str, str], list[tuple[str, Any]]] = {}
        # Sticky context: (connector_id, channel_id) -> agent_id
        self._context: dict[tuple[str, str], str] = {}
        # Reply channels: project_name -> set of (connector_id, channel_id)
        # Tracks channels that sent messages to a project so notifications can reach them
        # even without pre-configured channel bindings.
        self._reply_channels: dict[str, set[tuple[str, str]]] = {}

    @property
    def config(self) -> Any:
        """Always return the latest config from the registry."""
        if self._registry is not None:
            return self._registry.config
        return self._config

    async def start(self) -> None:
        """Instantiate and start all enabled connectors from config."""
        for connector_id, connector_cfg in self.config.connectors.items():
            if not connector_cfg.enabled:
                logger.info("Connector '%s' is disabled, skipping", connector_id)
                continue

            connector = self._create_connector(connector_id, connector_cfg)
            if not connector:
                continue

            try:
                connector.set_message_callback(self._handle_inbound)
                await connector.start()
                self.connectors[connector_id] = connector
                logger.info(
                    "Started connector '%s' (type=%s)", connector_id, connector_cfg.type
                )
            except Exception:
                logger.exception("Failed to start connector '%s'", connector_id)

        self._rebuild_channel_map()

    async def stop(self) -> None:
        """Stop all running connectors."""
        for connector_id, connector in list(self.connectors.items()):
            try:
                await connector.stop()
                logger.info("Stopped connector '%s'", connector_id)
            except Exception:
                logger.exception("Error stopping connector '%s'", connector_id)
        self.connectors.clear()

    def _create_connector(
        self, connector_id: str, connector_cfg: Any
    ) -> BaseConnector | None:
        """Dynamically import and instantiate a connector by type."""
        entry = _CONNECTOR_REGISTRY.get(connector_cfg.type)
        if not entry:
            logger.warning(
                "Unknown connector type '%s' for '%s'", connector_cfg.type, connector_id
            )
            return None

        module_path, class_name = entry
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
        except (ImportError, AttributeError) as exc:
            logger.warning(
                "Cannot load connector '%s' (type=%s): %s. "
                "Is the required library installed?",
                connector_id,
                connector_cfg.type,
                exc,
            )
            return None

        config_dict = {
            "credentials": connector_cfg.credentials,
            "settings": connector_cfg.settings,
        }
        return cls(connector_id, config_dict)

    def _rebuild_channel_map(self) -> None:
        """Build (connector_id, channel_id) -> [(project_name, binding)] lookup."""
        self._channel_map.clear()
        for project_name, project_cfg in self.config.projects.items():
            for binding in getattr(project_cfg, "channels", []):
                if not binding.inbound:
                    continue
                key = (binding.connector_id, binding.channel_id)
                self._channel_map.setdefault(key, []).append((project_name, binding))

    def _set_context(self, connector_id: str, channel_id: str, agent_id: str) -> None:
        """Remember the last-interacted agent for a channel."""
        self._context[(connector_id, channel_id)] = agent_id

    def _track_reply_channel(
        self, connector_id: str, channel_id: str, project_name: str
    ) -> None:
        """Register a channel as a reply target for a project.

        This ensures outbound notifications reach channels that sent messages
        via @project prefix even if no channel binding is configured.
        """
        self._reply_channels.setdefault(project_name, set()).add(
            (connector_id, channel_id)
        )

    def _get_context(self, connector_id: str, channel_id: str) -> str:
        """Retrieve sticky agent context, clearing if the agent no longer exists."""
        agent_id = self._context.get((connector_id, channel_id), "")
        if agent_id and not self.agent_manager.get_agent(agent_id):
            del self._context[(connector_id, channel_id)]
            return ""
        return agent_id

    def _resolve_single_agent(self, connector_id: str, channel_id: str) -> str:
        """If the channel is bound to exactly one project with one agent, return its id."""
        key = (connector_id, channel_id)
        bindings = self._channel_map.get(key, [])
        if len(bindings) != 1:
            return ""
        project_name = bindings[0][0]
        agents = self.agent_manager.list_agents(project_name=project_name)
        if len(agents) == 1:
            return agents[0].id
        return ""

    def _persist_known_chats(self, connector_id: str) -> None:
        """Save connector's known chats to config so they survive restarts."""
        if not self._registry:
            return
        connector = self.connectors.get(connector_id)
        if not connector or not hasattr(connector, "get_known_chats"):
            return
        known = connector.get_known_chats()
        if not known:
            return
        connector_cfg = self.config.connectors.get(connector_id)
        if connector_cfg and connector_cfg.settings.get("known_chats") != known:
            connector_cfg.settings["known_chats"] = known
            try:
                self._registry.save()
                logger.debug("Persisted %d known chat(s) for '%s'", len(known), connector_id)
            except Exception:
                logger.debug("Failed to persist known chats for '%s'", connector_id, exc_info=True)

    async def _handle_inbound(self, msg: InboundMessage) -> None:
        """Route an inbound message to the correct agent."""
        # Persist any newly tracked chats
        self._persist_known_chats(msg.connector_id)

        if msg.is_command:
            await self._handle_command(msg)
            return

        # Try channel-based routing first
        project_name = msg.project_name
        agent_id = msg.agent_id

        if not project_name:
            key = (msg.connector_id, msg.channel_id)
            bindings = self._channel_map.get(key, [])
            if len(bindings) == 1:
                project_name = bindings[0][0]
            elif len(bindings) > 1:
                # Multiple projects bound — try @project prefix first
                project_name, agent_id, text = self._parse_target(msg.text)
                if project_name:
                    msg.text = text
                else:
                    # Fall back to sticky context (e.g. replying to an agent message)
                    ctx_agent_id = self._get_context(msg.connector_id, msg.channel_id)
                    if ctx_agent_id:
                        agent = self.agent_manager.get_agent(ctx_agent_id)
                        if agent:
                            project_name = agent.project_name
                            agent_id = ctx_agent_id
                    # Also check if the connector extracted an agent_id (e.g. from a reply)
                    if not project_name and msg.agent_id:
                        agent = self.agent_manager.get_agent(msg.agent_id)
                        if agent:
                            project_name = agent.project_name
                            agent_id = msg.agent_id
                    if not project_name:
                        projects = ", ".join(b[0] for b in bindings)
                        await self._reply(
                            msg,
                            f"Multiple projects bound to this channel: {projects}\n"
                            "Use @project message to specify.",
                        )
                        return
            else:
                # No channel binding — try @project prefix
                project_name, agent_id, text = self._parse_target(msg.text)
                if project_name:
                    msg.text = text
                else:
                    # Try sticky context before giving up
                    ctx_agent_id = self._get_context(msg.connector_id, msg.channel_id)
                    if ctx_agent_id:
                        agent = self.agent_manager.get_agent(ctx_agent_id)
                        if agent:
                            project_name = agent.project_name
                            agent_id = ctx_agent_id
                    if not project_name:
                        await self._reply(
                            msg,
                            "Usage: @project message\nOr: @project:agent_id message",
                        )
                        return

        # Validate project
        projects = self.agent_manager.registry.list_projects()
        if project_name not in projects:
            available = ", ".join(sorted(projects.keys()))
            await self._reply(msg, f"Unknown project: '{project_name}'\nAvailable: {available}")
            return

        # Resolve agent
        newly_spawned = False
        if agent_id:
            agent = self.agent_manager.get_agent(agent_id)
            if not agent:
                await self._reply(msg, f"Agent `{agent_id}` not found.")
                return
        else:
            result = await self._smart_route(project_name, msg)
            if result is None:
                return
            agent, newly_spawned = result

        if newly_spawned:
            # Message text is sent via the spawn start sequence.
            # Handle media staging if needed.
            if msg.media_paths and self.media_handler:
                try:
                    staged = []
                    last_media_type = None
                    for media_path in msg.media_paths:
                        paths, media_type = await self.media_handler.process_and_stage(
                            source_path=media_path,
                            agent_worktree=agent.worktree_path,
                        )
                        staged.extend(paths)
                        last_media_type = media_type
                    if staged:
                        media_context = ""
                        if last_media_type is not None:
                            media_context = self.media_handler.build_media_reference(
                                staged, last_media_type
                            )
                        # Send media references after the start sequence finishes
                        async def _send_media_refs(
                            aid: str = agent.id,
                            ctx: str = media_context,
                            paths: list[str] = staged,
                        ) -> None:
                            await asyncio.sleep(5.0)
                            if ctx:
                                await self.agent_manager.send_message(aid, ctx)
                            else:
                                refs = "\n".join(f"  - {p}" for p in paths)
                                await self.agent_manager.send_message(
                                    aid, f"Media files staged:\n{refs}"
                                )

                        asyncio.ensure_future(_send_media_refs())
                    file_list = "\n".join(f"  - {p}" for p in staged)
                    await self._reply(
                        msg,
                        f"Spawned agent `{agent.id}` for {project_name}\n"
                        f"Staged:\n{file_list}",
                    )
                except Exception:
                    logger.exception("Failed to process media for auto-spawned agent")
                    await self._reply(
                        msg,
                        f"Spawned agent `{agent.id}` for {project_name}"
                        " (media staging failed)",
                    )
            else:
                await self._reply(
                    msg, f"Spawned agent `{agent.id}` for {project_name}"
                )
            self._set_context(msg.connector_id, msg.channel_id, agent.id)
            self._track_reply_channel(msg.connector_id, msg.channel_id, project_name)
            return

        # Send message to an existing agent (with or without media)
        if msg.media_paths and self.media_handler:
            temp_paths = list(msg.media_paths)
            try:
                staged: list[str] = []
                last_media_type = None
                for media_path in msg.media_paths:
                    paths, media_type = await self.media_handler.process_and_stage(
                        source_path=media_path,
                        agent_worktree=agent.worktree_path,
                    )
                    staged.extend(paths)
                    last_media_type = media_type

                media_context = ""
                if staged and last_media_type is not None:
                    media_context = self.media_handler.build_media_reference(
                        staged, last_media_type
                    )

                await self.agent_manager.send_message_with_media(
                    agent.id, msg.text, staged, media_context=media_context
                )
                file_list = "\n".join(f"  - {p}" for p in staged)
                await self._reply(msg, f"Staged to `{agent.id}` ({project_name}):\n{file_list}")
                self._set_context(msg.connector_id, msg.channel_id, agent.id)
                self._track_reply_channel(msg.connector_id, msg.channel_id, project_name)
            except Exception:
                logger.exception("Failed to process media message")
                await self._reply(msg, "Failed to process media attachment.")
            finally:
                # Clean up connector temp files
                for temp_path in temp_paths:
                    try:
                        p = Path(temp_path)
                        if p.exists():
                            p.unlink()
                            # Remove parent dir if it's a forge temp dir and now empty
                            parent = p.parent
                            if parent.name.startswith("forge_") and not any(parent.iterdir()):
                                parent.rmdir()
                    except OSError:
                        logger.debug("Failed to clean up temp file: %s", temp_path)
        else:
            success = await self.agent_manager.send_message(agent.id, msg.text)
            if success:
                await self._reply(msg, f"Sent to `{agent.id}` ({project_name})")
                self._set_context(msg.connector_id, msg.channel_id, agent.id)
                self._track_reply_channel(msg.connector_id, msg.channel_id, project_name)
            else:
                await self._reply(msg, f"Failed to send message to `{agent.id}`.")

    async def _smart_route(
        self, project_name: str, msg: InboundMessage
    ) -> tuple[Any, bool] | None:
        """Smart load balancer: find an available agent or spawn a new one.

        Returns (agent, newly_spawned) or None if routing failed (reply already sent).

        Priority:
        1. IDLE agents — free to take a new task (context is cleared first)
        2. If all agents are busy, spawn a new one (if under limit)
        3. If at limit and all busy, report to the user
        """
        from ..agent_manager import AgentStatus

        agents = self.agent_manager.list_agents(project_name=project_name)
        active_agents = [a for a in agents if a.status != AgentStatus.STOPPED]

        if not active_agents:
            return await self._auto_spawn(project_name, msg)

        # Prefer IDLE agents — they're at a prompt and free for a new task
        idle_agents = [a for a in active_agents if a.status == AgentStatus.IDLE]
        if idle_agents:
            agent = max(idle_agents, key=lambda a: a.last_activity)
            await self.agent_manager.clear_context(agent.id)
            agent.task_description = msg.text[:200]
            return agent, False

        # All agents are busy — try to spawn a new one
        config = self.agent_manager.registry.config
        max_agents = config.get_max_agents(project_name)

        if len(active_agents) < max_agents:
            return await self._auto_spawn(project_name, msg)

        # At limit and all busy
        busy_list = "\n".join(
            f"  [{a.status.value}] {a.id}"
            + (f" — {a.task_description}" if a.task_description else "")
            for a in active_agents
        )
        await self._reply(
            msg,
            f"All agents for {project_name} are busy"
            f" ({len(active_agents)}/{max_agents}):\n{busy_list}",
        )
        return None

    async def _auto_spawn(
        self, project_name: str, msg: InboundMessage
    ) -> tuple[Any, bool] | None:
        """Spawn a new agent for the project. Returns (agent, True) or None on failure."""
        try:
            agent = await self.agent_manager.spawn_agent(project_name, task=msg.text)
            return agent, True
        except RuntimeError as exc:
            await self._reply(msg, f"Failed to spawn agent: {exc}")
            return None

    def _resolve_control_agent(self, msg: InboundMessage) -> str | None:
        """Resolve target agent for a control command.

        Resolution order:
        1. Explicit arg: /approve abc123
        2. Sticky context for the channel
        3. Single-agent shortcut (channel bound to one project with one agent)
        4. None (caller should send usage hint)
        """
        if msg.command_args:
            return msg.command_args[0]

        ctx = self._get_context(msg.connector_id, msg.channel_id)
        if ctx:
            return ctx

        single = self._resolve_single_agent(msg.connector_id, msg.channel_id)
        if single:
            return single

        return None

    async def _handle_command(self, msg: InboundMessage) -> None:
        """Handle platform-agnostic commands (/status, /spawn, /kill, /projects, control)."""
        cmd = msg.command_name.lstrip("/")

        if cmd in ("help", "commands", "start"):
            help_text = (
                "Agent Forge — Command Reference\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "\n"
                "AGENT MANAGEMENT\n"
                "  /status — List all active agents and their status\n"
                "  /spawn <project> [task] — Spawn a new agent\n"
                "  /kill <agent_id> — Terminate an agent\n"
                "  /projects — List available projects\n"
                "\n"
                "AGENT CONTROL\n"
                "  /approve [agent_id] — Approve a pending action\n"
                "  /approve_all [agent_id] — Approve all pending actions\n"
                "  /reject [agent_id] — Reject a pending action\n"
                "  /interrupt [agent_id] — Interrupt an agent\n"
                "\n"
                "  Control commands use your last-interacted agent\n"
                "  if no agent_id is given.\n"
                "\n"
                "SENDING MESSAGES\n"
                "  @project message — Send to the most recent agent\n"
                "  @project:agent_id message — Send to a specific agent\n"
                "\n"
                "  If the channel is bound to a single project, just\n"
                "  type your message directly — no prefix needed.\n"
                "\n"
                "  You can also send photos, files, and voice messages\n"
                "  with or without a caption. They'll be staged into\n"
                "  the agent's worktree.\n"
                "\n"
                "TIPS\n"
                "  - After messaging an agent, it becomes your active\n"
                "    context — control commands auto-target it\n"
                "  - Use /status to find agent IDs\n"
                "  - Use /spawn project fix the login bug to spawn\n"
                "    an agent with a task description"
            )
            await self._reply(msg, help_text)
            return

        if cmd == "status":
            grouped = self.agent_manager.get_agents_by_project()
            if not grouped:
                await self._reply(msg, "No active agents.")
                return
            lines: list[str] = []
            for project, agents in grouped.items():
                lines.append(f"** {project} **")
                for agent in agents:
                    task_info = f" - {agent.task_description}" if agent.task_description else ""
                    lines.append(f"  [{agent.status.value}] {agent.id}{task_info}")
            await self._reply(msg, "\n".join(lines))

        elif cmd == "spawn":
            args = msg.command_args
            if not args:
                await self._reply(msg, "Usage: /spawn project [task description]")
                return
            project_name = args[0]
            task = " ".join(args[1:]) if len(args) > 1 else ""
            projects = self.agent_manager.registry.list_projects()
            if project_name not in projects:
                available = ", ".join(sorted(projects.keys()))
                await self._reply(msg, f"Unknown project: '{project_name}'\nAvailable: {available}")
                return
            try:
                agent = await self.agent_manager.spawn_agent(project_name, task=task)
                reply = f"Spawned agent `{agent.id}` for {project_name}"
                if task:
                    reply += f"\nTask: {task}"
                await self._reply(msg, reply)
                self._set_context(msg.connector_id, msg.channel_id, agent.id)
                self._track_reply_channel(msg.connector_id, msg.channel_id, project_name)
            except RuntimeError as exc:
                await self._reply(msg, f"Failed to spawn agent: {exc}")

        elif cmd == "kill":
            args = msg.command_args
            if not args:
                await self._reply(msg, "Usage: /kill agent_id")
                return
            agent_id = args[0]
            success = await self.agent_manager.kill_agent(agent_id)
            if success:
                await self._reply(msg, f"Agent `{agent_id}` killed.")
            else:
                await self._reply(msg, f"Agent `{agent_id}` not found.")

        elif cmd == "projects":
            projects = self.agent_manager.registry.list_projects()
            if not projects:
                await self._reply(msg, "No projects registered.")
                return
            lines = []
            for name, project in sorted(projects.items()):
                desc = f" - {project.description}" if project.description else ""
                lines.append(f"* {name}{desc}")
            await self._reply(msg, "\n".join(lines))

        elif cmd in ("approve", "reject", "interrupt", "approve_all"):
            agent_id = self._resolve_control_agent(msg)
            if not agent_id:
                await self._reply(
                    msg,
                    f"Usage: /{cmd} [agent_id]\n"
                    "Send a message to an agent first to set context.",
                )
                return
            agent = self.agent_manager.get_agent(agent_id)
            if not agent:
                await self._reply(msg, f"Agent `{agent_id}` not found.")
                return
            success = await self.agent_manager.send_control(agent_id, cmd)
            if success:
                await self._reply(msg, f"Sent `{cmd}` to agent `{agent_id}`")
                self._set_context(msg.connector_id, msg.channel_id, agent_id)
            else:
                await self._reply(msg, f"Failed to send `{cmd}` to agent `{agent_id}`.")

        else:
            await self._reply(msg, f"Unknown command: /{cmd}")

    async def _reply(self, original: InboundMessage, text: str) -> None:
        """Send a reply back through the same connector/channel the message came from."""
        connector = self.connectors.get(original.connector_id)
        if not connector:
            logger.warning(
                "Cannot reply: connector '%s' not found", original.connector_id
            )
            return
        out = OutboundMessage(channel_id=original.channel_id, text=text)
        try:
            await connector.send_message(out)
        except Exception:
            logger.exception("Failed to send reply via connector '%s'", original.connector_id)

    async def send_to_project_channels(self, project_name: str, text: str) -> None:
        """Send a message to all outbound channels bound to a project."""
        sent: set[tuple[str, str]] = set()
        for proj_name, project_cfg in self.config.projects.items():
            if proj_name != project_name:
                continue
            for binding in getattr(project_cfg, "channels", []):
                if not binding.outbound:
                    continue
                connector = self.connectors.get(binding.connector_id)
                if not connector:
                    logger.warning("Connector %s not found for outbound to %s", binding.connector_id, project_name)
                    continue
                out = OutboundMessage(channel_id=binding.channel_id, text=text)
                try:
                    await connector.send_message(out)
                    sent.add((binding.connector_id, binding.channel_id))
                except Exception:
                    logger.exception(
                        "Failed to send outbound to %s/%s",
                        binding.connector_id,
                        binding.channel_id,
                    )

        # Also send to reply channels that interacted via @project prefix
        for connector_id, channel_id in self._reply_channels.get(project_name, set()):
            if (connector_id, channel_id) in sent:
                continue
            connector = self.connectors.get(connector_id)
            if not connector:
                continue
            out = OutboundMessage(channel_id=channel_id, text=text)
            try:
                await connector.send_message(out)
            except Exception:
                logger.debug(
                    "Failed to send to reply channel %s/%s",
                    connector_id,
                    channel_id,
                )

    async def send_to_project_channels_rich(
        self,
        project_name: str,
        text: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Send a message with extra metadata (e.g. action buttons) to all outbound channels."""
        sent: set[tuple[str, str]] = set()
        for proj_name, project_cfg in self.config.projects.items():
            if proj_name != project_name:
                continue
            for binding in getattr(project_cfg, "channels", []):
                if not binding.outbound:
                    continue
                connector = self.connectors.get(binding.connector_id)
                if not connector:
                    continue
                out = OutboundMessage(
                    channel_id=binding.channel_id,
                    text=text,
                    extra=extra or {},
                )
                try:
                    await connector.send_message(out)
                    sent.add((binding.connector_id, binding.channel_id))
                except Exception:
                    logger.debug(
                        "Failed to send rich outbound to %s/%s",
                        binding.connector_id,
                        binding.channel_id,
                    )

        # Also send to reply channels that interacted via @project prefix
        for connector_id, channel_id in self._reply_channels.get(project_name, set()):
            if (connector_id, channel_id) in sent:
                continue
            connector = self.connectors.get(connector_id)
            if not connector:
                continue
            out = OutboundMessage(
                channel_id=channel_id,
                text=text,
                extra=extra or {},
            )
            try:
                await connector.send_message(out)
            except Exception:
                logger.debug(
                    "Failed to send rich to reply channel %s/%s",
                    connector_id,
                    channel_id,
                )

    async def restart_connector(self, connector_id: str) -> bool:
        """Stop and restart a single connector from current config."""
        old = self.connectors.pop(connector_id, None)
        if old:
            try:
                await old.stop()
            except Exception:
                logger.exception("Error stopping connector '%s'", connector_id)

        connector_cfg = self.config.connectors.get(connector_id)
        if not connector_cfg or not connector_cfg.enabled:
            self._rebuild_channel_map()
            return False

        connector = self._create_connector(connector_id, connector_cfg)
        if not connector:
            self._rebuild_channel_map()
            return False

        try:
            connector.set_message_callback(self._handle_inbound)
            await connector.start()
            self.connectors[connector_id] = connector
            self._rebuild_channel_map()
            logger.info("Restarted connector '%s'", connector_id)
            return True
        except Exception:
            logger.exception("Failed to restart connector '%s'", connector_id)
            self._rebuild_channel_map()
            return False

    def get_connector(self, connector_id: str) -> BaseConnector | None:
        return self.connectors.get(connector_id)

    def get_status(self) -> dict[str, dict[str, Any]]:
        """Return status for all configured connectors."""
        status: dict[str, dict[str, Any]] = {}
        for connector_id, connector_cfg in self.config.connectors.items():
            status[connector_id] = {
                "type": connector_cfg.type,
                "enabled": connector_cfg.enabled,
                "running": connector_id in self.connectors,
            }
        return status

    @staticmethod
    def _parse_target(text: str) -> tuple[str, str, str]:
        """Parse @project[:agent_id] message from text.

        Returns (project_name, agent_id, remaining_text).
        If no match, all strings are empty.
        """
        import re

        match = re.match(r"^@([\w-]+)(?::([\w-]+))?[:\s]\s*(.*)", text, re.DOTALL)
        if not match:
            return "", "", ""
        project_name = match.group(1)
        agent_id = match.group(2) or ""
        message = match.group(3).strip()
        return project_name, agent_id, message
