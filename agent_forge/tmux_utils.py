"""Low-level tmux subprocess helpers."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TMUX_TIMEOUT = 10


@dataclass
class TmuxSession:
    name: str
    created: str
    attached: bool
    width: int
    height: int


def _run(args: list[str], timeout: int = TMUX_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a subprocess command with standard options."""
    try:
        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error("Tmux command timed out: %s", " ".join(args))
        raise
    except FileNotFoundError:
        logger.error("tmux not found. Is it installed?")
        raise


def list_sessions() -> list[TmuxSession]:
    """List all tmux sessions with metadata."""
    fmt = "#{session_name}|#{session_created}|#{session_attached}|#{session_width}|#{session_height}"
    result = _run(["tmux", "list-sessions", "-F", fmt])
    if result.returncode != 0:
        return []

    sessions = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) != 5:
            continue
        sessions.append(
            TmuxSession(
                name=parts[0],
                created=parts[1],
                attached=parts[2] == "1",
                width=int(parts[3]) if parts[3].isdigit() else 80,
                height=int(parts[4]) if parts[4].isdigit() else 24,
            )
        )
    return sessions


def session_exists(name: str) -> bool:
    """Check if a tmux session exists."""
    result = _run(["tmux", "has-session", "-t", name])
    return result.returncode == 0


def create_session(name: str, working_dir: str, command: str) -> bool:
    """Create a new detached tmux session running command in working_dir."""
    result = _run(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            name,
            "-c",
            working_dir,
            command,
        ]
    )
    if result.returncode != 0:
        logger.error(
            "Failed to create tmux session '%s': %s", name, result.stderr.strip()
        )
        return False
    return True


def kill_session(name: str) -> bool:
    """Kill a tmux session."""
    result = _run(["tmux", "kill-session", "-t", name])
    if result.returncode != 0:
        logger.error(
            "Failed to kill tmux session '%s': %s", name, result.stderr.strip()
        )
        return False
    return True


def send_keys(session_name: str, text: str, enter: bool = True) -> bool:
    """Send keystrokes to a tmux session.

    Sends the text first, then presses Enter twice if *enter* is True.
    Claude Code needs the second Enter to actually submit the prompt
    (the first Enter just finalises the text line).
    """
    # Send the text content
    result = _run(["tmux", "send-keys", "-t", session_name, text])
    if result.returncode != 0:
        logger.error(
            "Failed to send keys to '%s': %s", session_name, result.stderr.strip()
        )
        return False
    if enter:
        # Two Enters: first closes the line, second submits the prompt
        _run(["tmux", "send-keys", "-t", session_name, "Enter"])
        _run(["tmux", "send-keys", "-t", session_name, "Enter"])
    return True


def capture_pane(session_name: str, lines: int = 50) -> str:
    """Capture the last N lines of terminal output.

    Uses -S (start line relative to visible pane) instead of -l which
    is not available in all tmux versions.
    """
    result = _run(
        [
            "tmux",
            "capture-pane",
            "-t",
            session_name,
            "-p",
            "-e",
            "-S",
            str(-lines),
        ]
    )
    if result.returncode != 0:
        logger.error(
            "Failed to capture pane for '%s': %s", session_name, result.stderr.strip()
        )
        return ""
    return result.stdout


def send_raw(session_name: str, *keys: str) -> bool:
    """Send raw key presses to a tmux session without extra Enter.

    Each argument is a tmux key name, e.g. "Enter", "Escape", "y", "C-c",
    "Up", "Down".  Use this for interactive prompts (Y/n, menu selection).
    """
    for key in keys:
        result = _run(["tmux", "send-keys", "-t", session_name, key])
        if result.returncode != 0:
            logger.error(
                "Failed to send raw key '%s' to '%s': %s",
                key,
                session_name,
                result.stderr.strip(),
            )
            return False
    return True


def get_cursor_y(session_name: str) -> int:
    """Get the cursor Y position to detect if session is waiting for input."""
    result = _run(
        [
            "tmux",
            "display-message",
            "-t",
            session_name,
            "-p",
            "#{cursor_y}",
        ]
    )
    if result.returncode != 0:
        return -1
    try:
        return int(result.stdout.strip())
    except ValueError:
        return -1


def enable_pipe_pane(session_name: str, log_path: str) -> bool:
    """Start piping all terminal output to a log file via tmux pipe-pane."""
    result = _run(["tmux", "pipe-pane", "-t", session_name, "-o", f"cat >> {log_path}"])
    if result.returncode != 0:
        logger.error("Failed to enable pipe-pane for '%s': %s", session_name, result.stderr.strip())
        return False
    return True


def disable_pipe_pane(session_name: str) -> bool:
    """Stop piping terminal output (pass empty command to pipe-pane)."""
    result = _run(["tmux", "pipe-pane", "-t", session_name])
    if result.returncode != 0:
        logger.error("Failed to disable pipe-pane for '%s': %s", session_name, result.stderr.strip())
        return False
    return True
