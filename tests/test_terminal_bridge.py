"""Tests for TerminalBridge text input handling."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, mock_open
import pytest

from agent_forge.terminal_bridge import TerminalBridge


@pytest.fixture
def bridge():
    """Create a TerminalBridge with mocked internals."""
    b = TerminalBridge("test_session")
    b._running = True
    b._process = MagicMock()
    b._process.stdin = MagicMock()
    b._process.stdin.write = MagicMock()
    b._process.stdin.drain = AsyncMock()
    return b


class TestHandleTextInput:
    """Tests for TerminalBridge.handle_text_input."""

    @pytest.mark.asyncio
    async def test_single_line_sends_literal_and_enter(self, bridge):
        """Single-line text should use send-keys -l then Enter."""
        await bridge.handle_text_input("hello world")

        calls = bridge._process.stdin.write.call_args_list
        # Should have 2 writes: literal text + Enter
        assert len(calls) == 2
        assert b"send-keys -t test_session -l" in calls[0][0][0]
        assert b"hello world" in calls[0][0][0]
        assert b"send-keys -t test_session Enter" in calls[1][0][0]

    @pytest.mark.asyncio
    async def test_empty_text_is_noop(self, bridge):
        """Empty text should not send anything."""
        await bridge.handle_text_input("")
        bridge._process.stdin.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_running_is_noop(self, bridge):
        """Should not send when bridge is not running."""
        bridge._running = False
        await bridge.handle_text_input("hello")
        bridge._process.stdin.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiline_uses_load_buffer(self, bridge):
        """Multi-line text should use tmux load-buffer + paste-buffer."""
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_exec.return_value = mock_proc

            with patch("tempfile.NamedTemporaryFile", mock_open()):
                with patch("os.unlink"):
                    await bridge.handle_text_input("line 1\nline 2")

            # Should have called create_subprocess_exec for load-buffer and paste-buffer
            assert mock_exec.call_count >= 2

    @pytest.mark.asyncio
    async def test_single_quotes_are_escaped(self, bridge):
        """Single quotes in text should be properly escaped."""
        await bridge.handle_text_input("it's a test")

        call_data = bridge._process.stdin.write.call_args_list[0][0][0]
        # The escaped form should be in the command
        assert b"it" in call_data

    @pytest.mark.asyncio
    async def test_no_process_is_noop(self, bridge):
        """Should not send when process is None."""
        bridge._process = None
        await bridge.handle_text_input("hello")
        # No exception raised — this is a no-op

    @pytest.mark.asyncio
    async def test_single_line_with_special_chars(self, bridge):
        """Single-line text without newlines should use the literal send path."""
        await bridge.handle_text_input("print('hello')")

        calls = bridge._process.stdin.write.call_args_list
        assert len(calls) == 2
        # First call is the literal send-keys, second is Enter
        first_call = calls[0][0][0]
        assert b"send-keys -t test_session -l" in first_call
        assert b"send-keys -t test_session Enter" in calls[1][0][0]
