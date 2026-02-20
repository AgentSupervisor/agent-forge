"""TerminalBridge — real-time tmux control mode streaming to WebSocket clients."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket

__all__ = ["TerminalBridge", "TerminalBridgeManager"]

logger = logging.getLogger(__name__)


class TerminalBridge:
    """Manages a single ``tmux -CC attach-session`` subprocess per tmux session.

    Reads ``%output`` lines from the control mode stdout and forwards the
    decoded bytes to all connected WebSocket clients.  Keyboard input from
    clients is written back to the control mode stdin as ``send-keys``
    commands.
    """

    def __init__(self, session_name: str) -> None:
        self.session_name: str = session_name
        self._clients: list[WebSocket] = []
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task | None = None
        self._running: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """Start the tmux control mode subprocess.

        Returns:
            True on success, False if the session does not exist or the
            process fails to start.
        """
        try:
            self._process = await asyncio.create_subprocess_exec(
                "tmux",
                "-C",
                "attach-session",
                "-t",
                self.session_name,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception:
            logger.exception(
                "Failed to start tmux control mode for session %s", self.session_name
            )
            return False

        # Verify the process didn't exit immediately (e.g. session not found)
        await asyncio.sleep(0.1)
        if self._process.returncode is not None:
            logger.warning(
                "tmux control mode exited immediately for session %s (rc=%s)",
                self.session_name,
                self._process.returncode,
            )
            self._process = None
            return False

        self._running = True
        self._reader_task = asyncio.create_task(self._read_output())
        logger.info("TerminalBridge started for session %s", self.session_name)
        return True

    async def stop(self) -> None:
        """Gracefully detach from tmux and clean up."""
        self._running = False

        if self._process and self._process.stdin:
            try:
                self._process.stdin.write(b"detach\n")
                await self._process.stdin.drain()
            except Exception:
                pass

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        if self._process:
            try:
                self._process.terminate()
            except Exception:
                pass
            self._process = None

        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._clients.clear()

        logger.info("TerminalBridge stopped for session %s", self.session_name)

    # ------------------------------------------------------------------
    # Output reader
    # ------------------------------------------------------------------

    async def _read_output(self) -> None:
        """Read stdout from the control mode process and forward %output lines."""
        if not self._process or not self._process.stdout:
            return

        try:
            while self._running:
                line_bytes = await self._process.stdout.readline()
                if not line_bytes:
                    # EOF — tmux session likely died
                    logger.warning(
                        "tmux control mode EOF for session %s", self.session_name
                    )
                    self._running = False
                    break

                line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")

                if not line.startswith("%output "):
                    # Ignore %begin, %end, %error, %session-changed, etc.
                    continue

                # Format: %output %PANE_ID ESCAPED_DATA
                # Strip the leading "%output " prefix then split off the pane id
                rest = line[len("%output "):]
                # pane id is the next token (e.g. "%0"), data follows
                space_idx = rest.find(" ")
                if space_idx == -1:
                    # No data after pane id
                    continue

                escaped_data = rest[space_idx + 1:]
                decoded = self._decode_output(escaped_data)

                for ws in list(self._clients):
                    try:
                        await ws.send_bytes(decoded)
                    except Exception:
                        logger.debug(
                            "Failed to send bytes to WebSocket client; removing", exc_info=True
                        )
                        try:
                            self._clients.remove(ws)
                        except ValueError:
                            pass

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Unexpected error in _read_output for session %s", self.session_name
            )
            self._running = False
        finally:
            # Close all client WebSockets so browsers detect the disconnect
            # and reconnect, getting a fresh bridge.
            for ws in list(self._clients):
                try:
                    await ws.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Client management
    # ------------------------------------------------------------------

    async def add_client(self, ws: WebSocket) -> None:
        """Add a WebSocket client and send the current terminal snapshot.

        The snapshot is obtained via a regular ``tmux capture-pane`` subprocess
        call (not through the control mode stdin) so we do not need to parse the
        ``%begin``/``%end`` response.
        """
        self._clients.append(ws)

        # Send initial snapshot
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                "capture-pane",
                "-e",
                "-p",
                "-t",
                self.session_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if stdout:
                # capture-pane uses \n line endings; xterm.js needs \r\n
                stdout = stdout.replace(b"\n", b"\r\n")
                # Strip trailing blank lines (empty rows from pane padding)
                while stdout.endswith(b"\r\n\r\n"):
                    stdout = stdout[:-2]
                await ws.send_bytes(stdout)
        except Exception:
            logger.exception(
                "Failed to capture initial pane snapshot for session %s",
                self.session_name,
            )

    def remove_client(self, ws: WebSocket) -> bool:
        """Remove a WebSocket client.

        Returns:
            True if no clients remain after removal (caller may clean up).
        """
        try:
            self._clients.remove(ws)
        except ValueError:
            pass
        return len(self._clients) == 0

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    async def handle_input(self, data: bytes) -> None:
        """Forward keyboard input from the client to the tmux session.

        Printable ASCII text is sent via ``send-keys -l`` (literal mode).
        Any input that contains non-printable bytes (control characters, escape
        sequences, etc.) is sent via ``send-keys -H`` (hex mode) so that raw
        control bytes never corrupt the line-oriented tmux control mode protocol.
        """
        if not self._running or self._process is None:
            return

        # Check if ALL bytes are printable ASCII (0x20–0x7E)
        all_printable = all(0x20 <= b <= 0x7E for b in data)

        if all_printable:
            # Safe to send as literal text
            text = data.decode("ascii")
            escaped = text.replace("'", "'\\''")
            await self._send_command(
                f"send-keys -t {self.session_name} -l -- '{escaped}'"
            )
        else:
            # Contains control/escape characters — use hex mode to avoid
            # injecting raw ESC bytes into the control mode command stream.
            hex_bytes = " ".join(f"{b:02x}" for b in data)
            await self._send_command(
                f"send-keys -t {self.session_name} -H {hex_bytes}"
            )

    async def handle_resize(self, cols: int, rows: int) -> None:
        """Resize the tmux window to match the client terminal dimensions."""
        if not self._running:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux",
                "resize-window",
                "-t",
                self.session_name,
                "-x",
                str(cols),
                "-y",
                str(rows),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
        except Exception:
            logger.debug(
                "Failed to resize window for session %s", self.session_name, exc_info=True
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _send_command(self, cmd: str) -> None:
        """Write a control mode command to the tmux subprocess via stdin."""
        if self._process is None or self._process.stdin is None:
            return
        try:
            self._process.stdin.write((cmd + "\n").encode("utf-8"))
            await self._process.stdin.drain()
        except Exception:
            logger.debug("Failed to write command to tmux stdin: %s", cmd, exc_info=True)

    @staticmethod
    def _decode_output(data: str) -> bytes:
        """Decode tmux control mode ``%output`` escaped data to raw bytes.

        tmux escapes non-printable bytes as ``\\NNN`` (octal) and backslash
        as ``\\\\``.
        """
        result = bytearray()
        i = 0
        while i < len(data):
            if data[i] == "\\" and i + 1 < len(data):
                if data[i + 1] == "\\":
                    result.append(0x5c)
                    i += 2
                elif (
                    i + 3 < len(data)
                    and all(c in "01234567" for c in data[i + 1 : i + 4])
                ):
                    result.append(int(data[i + 1 : i + 4], 8))
                    i += 4
                else:
                    result.extend(data[i].encode("utf-8"))
                    i += 1
            else:
                result.extend(data[i].encode("utf-8"))
                i += 1
        return bytes(result)

    @property
    def client_count(self) -> int:
        """Return the number of connected WebSocket clients."""
        return len(self._clients)


class TerminalBridgeManager:
    """Manages ``TerminalBridge`` instances across all agents.

    Keyed by tmux session name.  Thread-safe via an ``asyncio.Lock``.
    """

    def __init__(self) -> None:
        self._bridges: dict[str, TerminalBridge] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    async def get_or_create(self, session_name: str) -> TerminalBridge:
        """Return an existing running bridge or create and start a new one.

        Args:
            session_name: The tmux session name to attach to.

        Returns:
            A started ``TerminalBridge`` instance.

        Raises:
            RuntimeError: If the bridge cannot be started (e.g. session missing).
        """
        async with self._lock:
            bridge = self._bridges.get(session_name)
            if bridge is not None and bridge._running:
                return bridge

            bridge = TerminalBridge(session_name)
            started = await bridge.start()
            if not started:
                raise RuntimeError(
                    f"Could not attach to tmux session '{session_name}'"
                )
            self._bridges[session_name] = bridge
            return bridge

    async def remove(self, session_name: str) -> None:
        """Stop and remove the bridge for the given session."""
        async with self._lock:
            bridge = self._bridges.pop(session_name, None)
            if bridge is not None:
                await bridge.stop()

    async def shutdown(self) -> None:
        """Stop all managed bridges.  Called on application shutdown."""
        async with self._lock:
            for bridge in list(self._bridges.values()):
                await bridge.stop()
            self._bridges.clear()
        logger.info("TerminalBridgeManager shut down all bridges")
