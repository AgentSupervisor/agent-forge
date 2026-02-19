"""AgentManager — spawn, kill, route, and list Claude Code agents."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from . import tmux_utils
from .config import AgentProfile, DefaultsConfig, ForgeConfig, StartSequenceStep
from .registry import ProjectRegistry

logger = logging.getLogger(__name__)


class AgentStatus(str, Enum):
    STARTING = "starting"
    WORKING = "working"
    WAITING_INPUT = "waiting_input"
    IDLE = "idle"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class Agent:
    id: str
    project_name: str
    session_name: str
    worktree_path: str
    branch_name: str
    status: AgentStatus = AgentStatus.STARTING
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    last_output: str = ""
    task_description: str = ""
    sub_agent_count: int = 0
    profile: str = ""
    needs_attention: bool = False
    parked: bool = False
    output_log_path: str = ""
    last_relay_offset: int = 0
    last_response: str = ""
    last_user_message: str = ""


def _sanitize_for_branch(text: str) -> str:
    """Sanitize text for use in a git branch name."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "-", text.lower())
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
    return sanitized[:50] if sanitized else "task"


class AgentManager:
    def __init__(self, registry: ProjectRegistry, defaults: DefaultsConfig):
        self.registry = registry
        self.defaults = defaults
        self.agents: dict[str, Agent] = {}

        # Optional database reference — set by main.py after init
        self._db: object | None = None

    def _install_hooks(self, worktree_dir: Path, agent_id: str) -> None:
        """Install Claude Code hooks in the worktree to report sub-agent events."""
        hook_script = Path(__file__).resolve().parent / "hook_reporter.py"
        server_port = self.registry.config.server.port
        server_url = f"http://localhost:{server_port}"

        hooks_config = {
            "hooks": {
                "SubagentStart": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"python3 {hook_script} {agent_id} SubagentStart {server_url}",
                            }
                        ],
                    }
                ],
                "SubagentStop": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"python3 {hook_script} {agent_id} SubagentStop {server_url}",
                            }
                        ],
                    }
                ],
            }
        }

        claude_dir = worktree_dir / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_path = claude_dir / "settings.local.json"
        settings_path.write_text(json.dumps(hooks_config, indent=2))
        logger.info("Installed Claude Code hooks in %s", settings_path)

    def _copy_agent_skills(self, worktree_dir: Path) -> None:
        """Copy .claude/agents/ skill definitions into the worktree.

        Agent-forge's skill catalog is copied so spawned agents
        have access to specialized agent definitions.
        """
        forge_root = Path(__file__).resolve().parent.parent
        source = forge_root / ".claude" / "agents"
        if not source.is_dir():
            return

        dest = worktree_dir / ".claude" / "agents"
        shutil.copytree(str(source), str(dest), dirs_exist_ok=True)
        logger.info("Copied agent skills to %s", dest)

    def _generate_claude_md(
        self,
        worktree_dir: Path,
        project_name: str,
        profile: AgentProfile | None,
    ) -> None:
        """Generate a CLAUDE.md in the worktree by merging instruction layers.

        Layers (in order):
        1. Global agent_instructions from defaults
        2. Project-specific agent_instructions
        3. Profile instructions
        4. Context files inlined from the project

        When any layer produces content, the generated output is written as the
        authoritative CLAUDE.md, overwriting whatever was already in the worktree.
        If all layers are empty, the file is left untouched.
        """
        config = self.registry.config
        project = config.projects.get(project_name)
        sections: list[str] = []

        # Layer 1: Global defaults (use self.defaults which may be updated at runtime)
        if self.defaults.agent_instructions.strip():
            sections.append(self.defaults.agent_instructions.strip())

        # Layer 2: Project-specific instructions
        if project and project.agent_instructions.strip():
            sections.append(project.agent_instructions.strip())

        # Layer 3: Profile instructions
        if profile and profile.instructions.strip():
            sections.append(profile.instructions.strip())

        # Layer 4: Context files
        if project and project.context_files:
            project_path = Path(project.path)
            for ctx_file in project.context_files:
                ctx_path = project_path / ctx_file
                if ctx_path.exists():
                    content = ctx_path.read_text().strip()
                    if content:
                        sections.append(
                            f"## {ctx_file}\n\n{content}"
                        )
                else:
                    logger.warning(
                        "Context file not found: %s (project %s)",
                        ctx_path,
                        project_name,
                    )

        # Nothing to write if all layers are empty
        if not sections:
            return

        generated = "\n\n".join(sections)

        claude_md_path = worktree_dir / "CLAUDE.md"
        claude_md_path.write_text(f"{generated}\n")
        logger.info("Generated CLAUDE.md in %s (%d layers)", worktree_dir, len(sections))

    def _get_start_sequence(
        self, profile: AgentProfile | None, task: str,
    ) -> list[StartSequenceStep]:
        """Return the start sequence for a profile, or the default sequence."""
        if profile and profile.start_sequence:
            return profile.start_sequence

        # Default sequence: wait 3s then send task
        if task:
            return [
                StartSequenceStep(action="wait", value="3"),
                StartSequenceStep(action="send", value="{task}"),
            ]
        return []

    async def _execute_start_sequence(
        self,
        agent_id: str,
        steps: list[StartSequenceStep],
        task: str,
    ) -> None:
        """Execute a start sequence, substituting {task} in send values."""
        for step in steps:
            agent = self.agents.get(agent_id)
            if not agent or agent.status == AgentStatus.STOPPED:
                return

            if step.action == "wait":
                try:
                    delay = float(step.value)
                except ValueError:
                    delay = 3.0
                await asyncio.sleep(delay)

            elif step.action == "send":
                text = step.value.replace("{task}", task)
                await self.send_message(agent_id, text)

            elif step.action == "wait_for_idle":
                await self._wait_for_idle(agent_id, step.value)

    async def _wait_for_idle(self, agent_id: str, timeout_str: str = "") -> None:
        """Poll tmux output until we detect an idle prompt pattern."""
        try:
            timeout = float(timeout_str) if timeout_str else 120.0
        except ValueError:
            timeout = 120.0

        idle_patterns = [
            r"^>\s*$",           # bare prompt
            r"╭─",              # claude code box top
            r"What would you",  # claude asking for input
        ]

        elapsed = 0.0
        poll_interval = 2.0
        while elapsed < timeout:
            agent = self.agents.get(agent_id)
            if not agent or agent.status == AgentStatus.STOPPED:
                return

            output = tmux_utils.capture_pane(agent.session_name, lines=20)
            if output:
                for pattern in idle_patterns:
                    if re.search(pattern, output, re.MULTILINE):
                        return

            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

        logger.warning("wait_for_idle timed out for agent %s after %.0fs", agent_id, timeout)

    async def _run_start_sequence(
        self,
        agent_id: str,
        profile: AgentProfile | None,
        task: str,
    ) -> None:
        """Build and execute the start sequence for an agent."""
        steps = self._get_start_sequence(profile, task)
        if steps:
            await self._execute_start_sequence(agent_id, steps, task)

    def _build_tmux_command(
        self,
        worktree_dir: Path,
        profile_obj: AgentProfile | None = None,
    ) -> str:
        """Build the tmux shell command that starts Claude Code in a worktree."""
        env_exports = " ".join(
            f"export {k}={v} &&" for k, v in self.defaults.claude_env.items()
        )
        claude_cmd = self.defaults.claude_command
        if profile_obj and profile_obj.system_prompt.strip():
            escaped_prompt = profile_obj.system_prompt.strip().replace("'", "'\\''")
            claude_cmd = f"{claude_cmd} --append-system-prompt '{escaped_prompt}'"
        if env_exports:
            return f"cd {worktree_dir} && {env_exports} {claude_cmd}"
        return f"cd {worktree_dir} && {claude_cmd}"

    async def spawn_agent(
        self,
        project_name: str,
        task: str = "",
        branch_prefix: str = "agent",
        profile: str = "",
    ) -> Agent:
        """Spawn a new Claude Code agent for a project."""
        project = self.registry.get_project(project_name)
        config = self.registry.config
        max_agents = config.get_max_agents(project_name)

        current_count = len(
            [a for a in self.agents.values() if a.project_name == project_name]
        )
        if current_count >= max_agents:
            raise RuntimeError(
                f"Agent limit reached for '{project_name}': {current_count}/{max_agents}"
            )

        # Resolve profile
        profile_obj: AgentProfile | None = None
        if profile:
            profile_obj = config.get_profile(profile)
            if not profile_obj:
                raise ValueError(f"Profile not found: '{profile}'")

        short_id = uuid.uuid4().hex[:6]
        task_slug = _sanitize_for_branch(task) if task else "task"
        branch_name = f"{branch_prefix}/{short_id}/{task_slug}"
        session_name = f"forge__{project_name}__{short_id}"
        project_path = Path(project.path)
        worktree_dir = project_path / ".worktrees" / short_id

        # Create the worktree directory
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)

        # Create git worktree with a new branch
        result = subprocess.run(
            [
                "git",
                "-C",
                str(project_path),
                "worktree",
                "add",
                "-b",
                branch_name,
                str(worktree_dir),
                project.default_branch,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create worktree: {result.stderr.strip()}")

        # Create .media/ directory in the worktree
        (worktree_dir / ".media").mkdir(parents=True, exist_ok=True)

        # Copy .env files from project directory (they're gitignored so not in worktrees)
        for env_file in project_path.glob(".env*"):
            if env_file.is_file():
                shutil.copy2(str(env_file), str(worktree_dir / env_file.name))

        # Install Claude Code hooks for sub-agent tracking
        self._install_hooks(worktree_dir, short_id)

        # Copy agent skill definitions from forge repo
        self._copy_agent_skills(worktree_dir)

        # Generate CLAUDE.md with merged instruction layers
        self._generate_claude_md(worktree_dir, project_name, profile_obj)

        # Build the command with optional env vars and system prompt
        tmux_command = self._build_tmux_command(worktree_dir, profile_obj)

        if not tmux_utils.create_session(session_name, str(worktree_dir), tmux_command):
            # Cleanup on failure
            subprocess.run(
                ["git", "-C", str(project_path), "worktree", "remove", str(worktree_dir), "--force"],
                capture_output=True,
                timeout=10,
            )
            raise RuntimeError(f"Failed to create tmux session: {session_name}")

        # Enable pipe-pane for full output capture
        output_log = worktree_dir / ".agent_output.log"
        tmux_utils.enable_pipe_pane(session_name, str(output_log))

        agent = Agent(
            id=short_id,
            project_name=project_name,
            session_name=session_name,
            worktree_path=str(worktree_dir),
            branch_name=branch_name,
            task_description=task,
            profile=profile,
            output_log_path=str(output_log),
        )
        self.agents[short_id] = agent

        # Run start sequence asynchronously (replaces hardcoded 3s delay)
        asyncio.ensure_future(self._run_start_sequence(short_id, profile_obj, task))

        logger.info(
            "Spawned agent %s for project '%s' on branch '%s' (profile=%s)",
            short_id,
            project_name,
            branch_name,
            profile or "none",
        )
        return agent

    async def spawn_comparison(
        self,
        project_name: str,
        task: str,
        profiles: list[str],
        count: int = 0,
    ) -> list[Agent]:
        """Spawn multiple agents on the same task with cycling profiles for A/B testing."""
        if not profiles:
            raise ValueError("At least one profile is required for comparison mode")

        # Default count = number of profiles
        if count <= 0:
            count = len(profiles)

        agents: list[Agent] = []
        for i in range(count):
            profile_name = profiles[i % len(profiles)]
            agent = await self.spawn_agent(
                project_name,
                task=task,
                branch_prefix="compare",
                profile=profile_name,
            )
            agents.append(agent)

        logger.info(
            "Spawned %d comparison agents for project '%s' with profiles %s",
            len(agents),
            project_name,
            profiles,
        )
        return agents

    async def kill_agent(self, agent_id: str) -> bool:
        """Kill an agent and clean up worktree, branch, and session."""
        agent = self.agents.get(agent_id)
        if not agent:
            logger.warning("Agent not found: %s", agent_id)
            return False

        project = self.registry.get_project(agent.project_name)
        project_path = Path(project.path)

        # Disable pipe-pane and clean up output log
        tmux_utils.disable_pipe_pane(agent.session_name)
        output_log = Path(agent.output_log_path)
        if output_log.exists():
            try:
                output_log.unlink()
            except OSError:
                pass

        # Kill tmux session
        tmux_utils.kill_session(agent.session_name)

        # Remove git worktree
        worktree = Path(agent.worktree_path)
        result = subprocess.run(
            [
                "git",
                "-C",
                str(project_path),
                "worktree",
                "remove",
                agent.worktree_path,
                "--force",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            logger.warning(
                "git worktree remove failed for %s: %s",
                agent_id,
                result.stderr.strip(),
            )
            # Fallback: remove directory and prune stale worktree entries
            if worktree.exists():
                shutil.rmtree(str(worktree), ignore_errors=True)
            subprocess.run(
                ["git", "-C", str(project_path), "worktree", "prune"],
                capture_output=True,
                text=True,
                timeout=10,
            )

        # Delete the local branch
        subprocess.run(
            [
                "git",
                "-C",
                str(project_path),
                "branch",
                "-D",
                agent.branch_name,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        del self.agents[agent_id]
        agent.status = AgentStatus.STOPPED

        logger.info("Killed agent %s (project '%s')", agent_id, agent.project_name)
        return True

    async def restart_agent(self, agent_id: str) -> "Agent":
        """Kill an agent and respawn it with the same project, task, and profile."""
        agent = self.agents.get(agent_id)
        if not agent:
            raise ValueError(f"Agent not found: {agent_id}")

        # Save config before kill destroys it
        project_name = agent.project_name
        task = agent.task_description
        profile = agent.profile

        await self.kill_agent(agent_id)
        return await self.spawn_agent(project_name, task=task, profile=profile)

    async def clear_context(self, agent_id: str) -> bool:
        """Clear an agent's conversation context by sending /clear to Claude Code.

        Should only be used on IDLE agents. Waits briefly for the command to process.
        """
        agent = self.agents.get(agent_id)
        if not agent:
            return False
        success = tmux_utils.send_keys(agent.session_name, "/clear")
        if success:
            await asyncio.sleep(1.0)
            agent.last_activity = datetime.now()
        return success

    async def send_message(self, agent_id: str, message: str) -> bool:
        """Send a text message to an agent's tmux session."""
        agent = self.agents.get(agent_id)
        if not agent:
            logger.warning("Agent not found: %s", agent_id)
            return False

        success = tmux_utils.send_keys(agent.session_name, message)
        if success:
            agent.last_activity = datetime.now()
            logger.info(
                "Sent message to agent %s: %s",
                agent_id,
                message[:100] + ("..." if len(message) > 100 else ""),
            )
            # Record byte offset for response relay
            if agent.output_log_path:
                try:
                    agent.last_relay_offset = Path(agent.output_log_path).stat().st_size
                except OSError:
                    pass
        return success

    async def send_message_with_media(
        self,
        agent_id: str,
        message: str,
        media_paths: list[str],
        media_context: str = "",
    ) -> bool:
        """Send a message that references media files staged in the worktree."""
        agent = self.agents.get(agent_id)
        if not agent:
            return False

        if media_context:
            full_message = f"{message}\n\n{media_context}"
        else:
            media_refs = ", ".join(media_paths)
            full_message = f"{message}\n\nReferenced files: {media_refs}"
        return await self.send_message(agent_id, full_message)

    async def send_control(self, agent_id: str, action: str) -> bool:
        """Send a control action to an agent's tmux session.

        Supported actions:
            approve      – press Enter (select highlighted option / confirm)
            approve_all  – press Down then Enter (select "Yes, always" option)
            reject       – press Escape (cancel / reject prompt)
            interrupt    – send Ctrl+C
            up / down    – arrow key navigation
        """
        agent = self.agents.get(agent_id)
        if not agent:
            logger.warning("Agent not found: %s", agent_id)
            return False

        key_map: dict[str, list[str]] = {
            "approve": ["Enter"],
            "approve_all": ["Down", "Enter"],
            "reject": ["Escape"],
            "interrupt": ["C-c"],
            "up": ["Up"],
            "down": ["Down"],
        }

        keys = key_map.get(action)
        if not keys:
            logger.warning("Unknown control action: %s", action)
            return False

        success = tmux_utils.send_raw(agent.session_name, *keys)
        if success:
            agent.last_activity = datetime.now()
            logger.info("Sent control '%s' to agent %s", action, agent_id)
        return success

    def get_agent(self, agent_id: str) -> Agent | None:
        return self.agents.get(agent_id)

    def list_agents(self, project_name: str | None = None) -> list[Agent]:
        """List all agents, optionally filtered by project."""
        agents = list(self.agents.values())
        if project_name:
            agents = [a for a in agents if a.project_name == project_name]
        return agents

    def get_agents_by_project(self) -> dict[str, list[Agent]]:
        """Return agents grouped by project name."""
        grouped: dict[str, list[Agent]] = {}
        for agent in self.agents.values():
            grouped.setdefault(agent.project_name, []).append(agent)
        return grouped

    async def recover_sessions(self) -> None:
        """
        On startup, scan for existing forge-* tmux sessions
        and reconstruct self.agents from them, restoring persisted
        metadata (task_description, branch_name, etc.) from the database.

        A second pass recovers orphaned agents whose tmux sessions were destroyed
        by a full system restart (power failure / reboot) but whose worktrees and
        database snapshots are still intact.
        """
        # Load saved snapshots keyed by agent_id
        snapshots: dict[str, dict] = {}
        if hasattr(self, "_db") and self._db is not None:
            from . import database
            rows = await database.load_snapshots(self._db)
            for row in rows:
                snapshots[row["agent_id"]] = row

        sessions = tmux_utils.list_sessions()
        recovered = 0
        for session in sessions:
            if not session.name.startswith("forge__"):
                continue

            parts = session.name.split("__", 2)
            if len(parts) != 3:
                continue

            _, project_name, short_id = parts

            if short_id in self.agents:
                continue

            try:
                project = self.registry.get_project(project_name)
            except KeyError:
                logger.warning(
                    "Recovered session '%s' references unknown project '%s'",
                    session.name,
                    project_name,
                )
                continue

            worktree_path = str(Path(project.path) / ".worktrees" / short_id)

            # Capture live tmux state so the first poll cycle
            # doesn't see a spurious status change and re-notify.
            # Use output as both args: we have no real "previous" to compare
            # against, so this avoids the output!=previous_output branch
            # returning WORKING, which would cause a spurious working->idle
            # notification on the first poll.
            output = tmux_utils.capture_pane(session.name, lines=100)
            from .status_monitor import StatusMonitor

            detected_status = StatusMonitor.detect_status(output, output)

            # Restore persisted fields from the database snapshot if available
            snap = snapshots.get(short_id, {})

            agent = Agent(
                id=short_id,
                project_name=project_name,
                session_name=session.name,
                worktree_path=worktree_path,
                branch_name=snap.get("branch_name", f"agent/{short_id}/recovered"),
                status=detected_status,
                last_output=output,
                task_description=snap.get("task_description", ""),
                needs_attention=bool(snap.get("needs_attention", False)),
                parked=bool(snap.get("parked", False)),
                last_response=snap.get("last_response", ""),
                last_user_message=snap.get("last_user_message", ""),
            )
            # Restore created_at from snapshot if available
            if snap.get("created_at"):
                try:
                    agent.created_at = datetime.fromisoformat(snap["created_at"])
                except (ValueError, TypeError):
                    pass

            self.agents[short_id] = agent
            recovered += 1

        # --- Power failure recovery ---
        # Check for snapshots with no matching tmux session but worktree still on disk
        recovered_ids = set(self.agents.keys())  # Already recovered above
        for agent_id, snap in snapshots.items():
            if agent_id in recovered_ids:
                continue  # Already recovered via tmux session

            # Don't revive agents that were intentionally stopped
            if snap.get("status") == AgentStatus.STOPPED.value:
                continue

            project_name = snap.get("project_name", "")
            try:
                project = self.registry.get_project(project_name)
            except KeyError:
                logger.warning(
                    "Power recovery: snapshot '%s' references unknown project '%s'",
                    agent_id,
                    project_name,
                )
                continue

            worktree_path = snap.get("worktree_path", "")
            if not worktree_path or not Path(worktree_path).is_dir():
                logger.info(
                    "Power recovery: skipping agent %s — worktree missing at %s",
                    agent_id,
                    worktree_path,
                )
                # Clean up stale snapshot
                if self._db:
                    from . import database
                    await database.delete_snapshot(self._db, agent_id)
                continue

            # Resolve profile for rebuilding the tmux command
            profile_name = snap.get("profile", "")
            config = self.registry.config
            profile_obj = config.get_profile(profile_name) if profile_name else None

            session_name = snap.get("session_name", f"forge__{project_name}__{agent_id}")
            worktree_dir = Path(worktree_path)

            # Rebuild and create the tmux session
            tmux_command = self._build_tmux_command(worktree_dir, profile_obj)
            if not tmux_utils.create_session(session_name, str(worktree_dir), tmux_command):
                logger.error(
                    "Power recovery: failed to recreate tmux session for agent %s",
                    agent_id,
                )
                continue

            # Re-enable pipe-pane for output capture
            output_log = worktree_dir / ".agent_output.log"
            tmux_utils.enable_pipe_pane(session_name, str(output_log))

            agent = Agent(
                id=agent_id,
                project_name=project_name,
                session_name=session_name,
                worktree_path=worktree_path,
                branch_name=snap.get("branch_name", f"agent/{agent_id}/recovered"),
                status=AgentStatus.STARTING,
                task_description=snap.get("task_description", ""),
                profile=profile_name,
                needs_attention=True,  # Flag for user attention after recovery
                parked=False,
                last_response=snap.get("last_response", ""),
                last_user_message=snap.get("last_user_message", ""),
                output_log_path=str(output_log),
            )
            # Restore created_at from snapshot if available
            if snap.get("created_at"):
                try:
                    agent.created_at = datetime.fromisoformat(snap["created_at"])
                except (ValueError, TypeError):
                    pass

            self.agents[agent_id] = agent

            # Log recovery event
            if self._db:
                from . import database
                await database.log_event(
                    self._db, agent_id, project_name, "power_recovery",
                    {
                        "task_description": agent.task_description,
                        "profile": profile_name,
                        "previous_status": snap.get("status", ""),
                    },
                )

            # Schedule recovery message asynchronously
            asyncio.ensure_future(
                self._send_recovery_message(agent_id, agent.task_description, agent.last_user_message)
            )
            recovered += 1

        if recovered:
            logger.info("Recovered %d agent sessions (including power failure recovery)", recovered)

    async def _send_recovery_message(
        self, agent_id: str, task: str, last_user_message: str,
    ) -> None:
        """Wait for Claude Code to become idle after restart, then send a recovery context message."""
        # Wait for Claude Code to initialize
        await asyncio.sleep(5)

        agent = self.agents.get(agent_id)
        if not agent or agent.status == AgentStatus.STOPPED:
            return

        # Wait for the idle prompt
        await self._wait_for_idle(agent_id, "120")

        parts = [
            "You are being recovered after a system restart (power failure or reboot).",
            "Your previous session was interrupted. Here is your context:",
        ]
        if task:
            parts.append(f"\n**Original task**: {task}")
        if last_user_message:
            parts.append(f"\n**Last message from user**: {last_user_message}")
        parts.append(
            "\nPlease review your current state (`git status`, recent changes) and continue working on your task."
            " If you had completed the task, just confirm completion."
        )

        recovery_msg = "\n".join(parts)
        await self.send_message(agent_id, recovery_msg)
