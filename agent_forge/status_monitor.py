"""StatusMonitor — polls tmux sessions and broadcasts agent state changes."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

import aiosqlite

from . import tmux_utils
from .agent_manager import AgentManager, AgentStatus
from .config import ForgeConfig
from .connectors.base import ActionButton
from .database import log_event, save_snapshot
from .response_extractor import extract_response, extract_response_regex
from .summarizer import summarize_output
from .websocket_manager import WebSocketManager

logger = logging.getLogger(__name__)

# Patterns that indicate the agent is waiting for user input
_INPUT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bAllow\b", re.IGNORECASE),
    re.compile(r"\bY/n\b"),
    re.compile(r"\by/N\b"),
    re.compile(r"\byes/no\b", re.IGNORECASE),
    re.compile(r"\bDo you want\b", re.IGNORECASE),
    re.compile(r"\[y/n\]", re.IGNORECASE),
    re.compile(r"\(y/n\)", re.IGNORECASE),
]

# Patterns that indicate an error state
_ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bError:", re.IGNORECASE),
    re.compile(r"\bfatal:", re.IGNORECASE),
    re.compile(r"\bFAILED\b"),
]

# Patterns that indicate the agent is idle at a prompt
_IDLE_PROMPT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[>❯]\s*$"),
    re.compile(r"\$\s*$"),
]


class StatusMonitor:
    """Periodically polls tmux sessions and pushes status updates via WebSocket."""

    def __init__(
        self,
        agent_manager: AgentManager,
        ws_manager: WebSocketManager,
        db: aiosqlite.Connection | None = None,
        poll_interval: float = 3.0,
        connector_manager: object | None = None,
        config: ForgeConfig | None = None,
    ) -> None:
        self.agent_manager = agent_manager
        self.ws_manager = ws_manager
        self.db = db
        self.poll_interval = poll_interval
        self.connector_manager = connector_manager
        self.config = config
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the background polling loop."""
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("StatusMonitor started (poll every %.1fs)", self.poll_interval)

    async def stop(self) -> None:
        """Stop the background polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("StatusMonitor stopped")

    async def _run(self) -> None:
        while self._running:
            try:
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in status monitor poll")
            await asyncio.sleep(self.poll_interval)

    async def _poll(self) -> None:
        for agent in self.agent_manager.list_agents():
            if agent.status == AgentStatus.STOPPED:
                continue

            output = tmux_utils.capture_pane(agent.session_name, lines=100)

            if not tmux_utils.session_exists(agent.session_name):
                old_status = agent.status
                agent.status = AgentStatus.STOPPED
                agent.needs_attention = True
                agent.parked = False
                if old_status != AgentStatus.STOPPED and self.db:
                    await log_event(
                        self.db, agent.id, agent.project_name,
                        "status_change", {"status": AgentStatus.STOPPED.value},
                    )
                    if old_status == AgentStatus.WORKING:
                        await self._relay_response(agent)
                    msg = f"Agent `{agent.id}` ({agent.project_name}) stopped"
                    summary = await self._get_activity_summary(
                        agent.last_output or "",
                    )
                    if summary:
                        msg += f"\n```\n{summary}\n```"
                    await self._notify_channels(agent.project_name, msg)
            else:
                new_status = self.detect_status(output, agent.last_output)
                if new_status != agent.status:
                    old_status = agent.status
                    agent.status = new_status

                    # Set attention flags based on status transitions
                    if new_status in (AgentStatus.IDLE, AgentStatus.WAITING_INPUT, AgentStatus.ERROR):
                        agent.needs_attention = True
                        agent.parked = False
                    elif new_status == AgentStatus.WORKING:
                        agent.needs_attention = False

                    if self.db:
                        await log_event(
                            self.db, agent.id, agent.project_name,
                            "status_change", {"status": new_status.value},
                        )
                    if new_status == AgentStatus.WAITING_INPUT:
                        await self._notify_waiting_input(
                            agent.id, agent.project_name, output,
                        )
                    elif new_status != AgentStatus.WORKING:
                        if new_status == AgentStatus.IDLE and old_status == AgentStatus.WORKING:
                            await self._relay_response(agent)
                        else:
                            msg = f"Agent `{agent.id}` ({agent.project_name}): {old_status.value} -> {new_status.value}"
                            summary = await self._get_activity_summary(output)
                            if summary:
                                msg += f"\n```\n{summary}\n```"
                            await self._notify_channels(agent.project_name, msg)

            agent.last_output = output

            if self.db:
                await save_snapshot(self.db, agent)

            await self.ws_manager.broadcast_agent_update(agent)
            await self.ws_manager.broadcast_terminal_output(agent.id, output)

    async def _notify_channels(self, project_name: str, text: str) -> None:
        """Send status notification to bound IM channels (best-effort)."""
        if not self.connector_manager:
            logger.debug("No connector_manager; skipping notification for %s", project_name)
            return
        try:
            await self.connector_manager.send_to_project_channels(project_name, text)
        except Exception:
            logger.exception("Failed to notify channels for %s", project_name)

    async def _notify_waiting_input(
        self, agent_id: str, project_name: str, output: str
    ) -> None:
        """Send a rich WAITING_INPUT notification with prompt text and action buttons."""
        if not self.connector_manager:
            return

        prompt_text = self.extract_prompt_text(output)
        header = f"Agent `{agent_id}` ({project_name}) is waiting for input"

        if prompt_text:
            text = f"{header}:\n```\n{prompt_text}\n```"
        else:
            text = header

        text += "\n\nReply: /approve | /reject | /interrupt"

        buttons = [
            ActionButton(label="Approve", action="approve", agent_id=agent_id),
            ActionButton(label="Reject", action="reject", agent_id=agent_id),
            ActionButton(label="Interrupt", action="interrupt", agent_id=agent_id),
        ]

        extra = {
            "notification_type": "waiting_input",
            "action_buttons": buttons,
        }

        try:
            await self.connector_manager.send_to_project_channels_rich(
                project_name, text, extra=extra,
            )
        except Exception:
            logger.debug("Failed to send rich notification for %s", project_name)

    async def _get_activity_summary(self, output: str) -> str:
        """Get an activity summary, using LLM if configured, else regex fallback."""
        if self.config:
            summary_cfg = self.config.defaults.summary
            api_key = self.config.get_summary_api_key()
            if summary_cfg.enabled and api_key:
                result = await summarize_output(
                    output,
                    api_key=api_key,
                    model=summary_cfg.model,
                    max_tokens=summary_cfg.max_tokens,
                    timeout=summary_cfg.timeout_seconds,
                )
                if result:
                    return result
        return self.extract_activity_summary(output)

    async def _relay_response(self, agent: Any) -> None:
        """Read new output from pipe-pane log and relay extracted response to IM."""
        if not agent.output_log_path:
            return

        log_path = Path(agent.output_log_path)
        if not log_path.exists():
            return

        try:
            file_size = log_path.stat().st_size
        except OSError:
            return

        if file_size <= agent.last_relay_offset:
            return

        # Read new content from the log
        try:
            with open(log_path, "r", errors="replace") as f:
                f.seek(agent.last_relay_offset)
                new_content = f.read()
        except OSError:
            return

        if not new_content.strip():
            return

        # Update offset
        agent.last_relay_offset = file_size

        # Try LLM extraction first, then regex fallback
        response_text = None
        if self.config:
            relay_cfg = self.config.defaults.response_relay
            api_key = self.config.get_summary_api_key()
            if relay_cfg.enabled and api_key:
                response_text = await extract_response(
                    new_content,
                    api_key=api_key,
                    model=relay_cfg.model,
                    max_tokens=relay_cfg.max_tokens,
                    timeout=relay_cfg.timeout_seconds,
                )

        if not response_text:
            response_text = extract_response_regex(new_content)

        if not response_text:
            return

        msg = f"Agent `{agent.id}` ({agent.project_name}) response:\n\n{response_text}"
        await self._notify_channels(agent.project_name, msg)

    @staticmethod
    def extract_prompt_text(output: str) -> str:
        """Extract the prompt/question text from terminal output.

        Searches backward through the last lines for input patterns,
        then captures surrounding context lines. Strips ANSI escape codes.
        """
        if not output:
            return ""

        # Strip ANSI escape codes
        ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        cleaned = ansi_re.sub("", output)

        lines = cleaned.rstrip().splitlines()
        if not lines:
            return ""

        # Search backward through the last 30 lines for an input pattern
        search_lines = lines[-30:]
        match_idx = -1
        for i in range(len(search_lines) - 1, -1, -1):
            for pattern in _INPUT_PATTERNS:
                if pattern.search(search_lines[i]):
                    match_idx = i
                    break
            if match_idx >= 0:
                break

        if match_idx < 0:
            return ""

        # Capture up to 3 lines of context before + the matching line
        start = max(0, match_idx - 3)
        context_lines = search_lines[start : match_idx + 1]
        # Strip empty leading lines
        while context_lines and not context_lines[0].strip():
            context_lines.pop(0)

        return "\n".join(context_lines)

    @staticmethod
    def extract_activity_summary(output: str) -> str:
        """Extract a short activity summary from terminal output.

        Returns the last few meaningful lines, stripped of ANSI codes and noise.
        """
        if not output or not output.strip():
            return ""

        ansi_re = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
        cleaned = ansi_re.sub("", output)

        lines = [ln for ln in cleaned.splitlines() if ln.strip()]
        if not lines:
            return ""

        # Take last ~40 non-empty lines
        tail = lines[-40:]

        # Filter out prompt lines, spinner artifacts, separators, and UI chrome
        noise_re = re.compile(
            r"^\s*[>❯$#]\s*$"                  # bare prompt chars
            r"|^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷]"  # Unicode spinners
            r"|^\s*[|/\-\\]\s\S.{0,30}$"       # ASCII spinners (short lines only)
            r"|^[\s─━─=~_*]{6,}$"              # separator lines
            r"|^[\s\-]{6,}$"                    # dash-only separator lines
            r"|^\s*⏵"                           # Claude Code UI chrome (bypass toggle)
            r"|^\s*[❯>]\s+\S"                  # Claude Code tool invocations (❯ command)
            r"|^\s*✻"                           # Claude Code thinking/churning indicator
        )
        meaningful = [ln for ln in tail if not noise_re.match(ln)]
        if not meaningful:
            return ""

        # Last 15 meaningful lines, truncated
        summary_lines = [ln[:120] for ln in meaningful[-15:]]
        return "\n".join(summary_lines)

    @staticmethod
    def detect_status(output: str, previous_output: str) -> AgentStatus:
        """Detect agent status from terminal output.

        Checks patterns in priority order:
        1. Permission / input prompts -> WAITING_INPUT
        2. Error indicators -> ERROR
        3. Idle prompt characters -> IDLE
        4. Output changed from previous -> WORKING
        5. Output unchanged -> IDLE
        """
        if not output:
            return AgentStatus.IDLE

        # Only inspect the last portion of output for prompt/error detection
        tail = output[-2000:]

        # 1. Input prompts (highest priority)
        for pattern in _INPUT_PATTERNS:
            if pattern.search(tail):
                return AgentStatus.WAITING_INPUT

        # 2. Error indicators
        for pattern in _ERROR_PATTERNS:
            if pattern.search(tail):
                return AgentStatus.ERROR

        # 3. Idle prompt — check the last non-empty line
        lines = tail.rstrip().splitlines()
        if lines:
            last_line = lines[-1]
            for pattern in _IDLE_PROMPT_PATTERNS:
                if pattern.search(last_line):
                    return AgentStatus.IDLE

        # 4. If output changed, the agent is working
        if output != previous_output:
            return AgentStatus.WORKING

        # 5. Output unchanged — idle
        return AgentStatus.IDLE
