"""Tests for the LLM-based response extractor."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agent_forge.response_extractor import (
    ExtractionResult,
    extract_response,
    extract_response_regex,
    preprocess_output,
)


class TestPreprocessOutput:
    def test_strips_ansi_codes(self):
        raw = "\x1b[32mHello\x1b[0m world"
        result = preprocess_output(raw)
        assert "\x1b" not in result
        assert "Hello" in result

    def test_strips_dec_private_modes(self):
        raw = "\x1b[?2026hHello\x1b[?2026l world"
        result = preprocess_output(raw)
        assert "?2026" not in result
        assert "Hello" in result
        assert "world" in result

    def test_strips_osc_sequences(self):
        raw = "\x1b]0;Window Title\x07Real content here"
        result = preprocess_output(raw)
        assert "Window Title" not in result
        assert "Real content here" in result

    def test_filters_noise_lines(self):
        raw = "Real output\n> \n⠋ Loading...\n────────────\nDone."
        result = preprocess_output(raw)
        assert "Real output" in result
        assert "Done." in result
        assert "⠋" not in result
        assert "────" not in result

    def test_limits_to_10k_chars(self):
        lines = [f"line {i}: " + "x" * 90 for i in range(200)]
        raw = "\n".join(lines)
        result = preprocess_output(raw)
        assert len(result) <= 10500  # Allow slight overhead

    def test_empty_input(self):
        assert preprocess_output("") == ""

    def test_all_noise(self):
        raw = "> \n❯ \n$ \n⠋ spin"
        result = preprocess_output(raw)
        assert result.strip() == ""

    def test_filters_star_spinner_lines(self):
        raw = "✢ processing...\n✳ building...\nReal output\n✶ done\n✽ cleaning"
        result = preprocess_output(raw)
        assert "Real output" in result
        assert "✢" not in result
        assert "✳" not in result
        assert "✶" not in result
        assert "✽" not in result

    def test_preserves_text_after_block_marker(self):
        """⏺ lines with text content should have text preserved, not filtered entirely."""
        raw = "⏺ Why do programmers prefer dark mode?\n\n  Because light attracts bugs."
        result = preprocess_output(raw)
        assert "Why do programmers prefer dark mode?" in result
        assert "Because light attracts bugs." in result

    def test_bare_block_marker_is_filtered(self):
        """Bare ⏺ with no text should be filtered."""
        raw = "⏺\nReal output here"
        result = preprocess_output(raw)
        assert "Real output here" in result
        assert "⏺" not in result

    def test_filters_ai_thinking(self):
        """ai(thinking) should be filtered as noise."""
        raw = "Real response text\nai(thinking)\nMore real text"
        result = preprocess_output(raw)
        assert "Real response text" in result
        assert "More real text" in result
        assert "ai(thinking)" not in result

    def test_filters_bare_thinking(self):
        """(thinking) should still be filtered."""
        raw = "Real text\n(thinking)\nMore text"
        result = preprocess_output(raw)
        assert "(thinking)" not in result

    def test_filters_tool_call_headers(self):
        """Lines like Bash(...) and Read(...) should be filtered as noise."""
        raw = "Some output\nBash(git log --oneline -5)\nRead(/path/to/file.py)\nMore output"
        result = preprocess_output(raw)
        assert "Some output" in result
        assert "More output" in result
        assert "Bash(git log" not in result
        assert "Read(/path/to/file.py)" not in result

    def test_filters_tool_output_markers(self):
        """Lines starting with ⎿ should be filtered."""
        raw = "Real text\n  ⎿  a33d24a fix: something\n  ⎿  1e127f9 fix: else\nMore text"
        result = preprocess_output(raw)
        assert "Real text" in result
        assert "More text" in result
        assert "⎿" not in result

    def test_filters_expand_hints(self):
        """Lines like '… +10 lines (ctrl+o to expand)' should be filtered."""
        raw = "Line A\n… +10 lines (ctrl+o to expand)\nLine B"
        result = preprocess_output(raw)
        assert "Line A" in result
        assert "Line B" in result
        assert "ctrl+o" not in result

    def test_filters_git_diff_markers(self):
        """Git diff header lines should be filtered as noise."""
        raw = (
            "Result text\n"
            "diff --git a/agent_forge/main.py b/agent_forge/main.py\n"
            "index abc123..def456 100644\n"
            "--- a/agent_forge/main.py\n"
            "+++ b/agent_forge/main.py\n"
            "End text"
        )
        result = preprocess_output(raw)
        assert "Result text" in result
        assert "End text" in result
        assert "diff --git" not in result
        assert "index abc123" not in result
        assert "--- a/" not in result
        assert "+++ b/" not in result

    def test_strips_complete_tool_blocks(self):
        """A tool header followed by ⎿ output lines should all be stripped as a unit."""
        raw = (
            "Some agent text\n"
            "Bash(git log --oneline -5)\n"
            "  ⎿  a33d24a fix: something\n"
            "  ⎿  1e127f9 fix: something else\n"
            "More agent text"
        )
        result = preprocess_output(raw)
        assert "Some agent text" in result
        assert "More agent text" in result
        assert "Bash(git log" not in result
        assert "a33d24a" not in result
        assert "1e127f9" not in result


class TestExtractResponseRegex:
    def test_returns_last_50_meaningful_lines(self):
        lines = [f"meaningful line {i}" for i in range(100)]
        raw = "\n".join(lines)
        result = extract_response_regex(raw)
        assert isinstance(result, ExtractionResult)
        result_lines = result.text.splitlines()
        assert len(result_lines) <= 50
        assert "meaningful line 99" in result_lines[-1]

    def test_truncates_at_200_chars(self):
        raw = "x" * 300
        result = extract_response_regex(raw)
        for line in result.text.splitlines():
            assert len(line) <= 200

    def test_filters_noise(self):
        raw = "Real output\n> \n⠋ spin\nMore output"
        result = extract_response_regex(raw)
        assert "Real output" in result.text
        assert "More output" in result.text
        assert "⠋" not in result.text

    def test_empty_input(self):
        result = extract_response_regex("")
        assert isinstance(result, ExtractionResult)
        assert result.text == ""

    def test_all_noise_returns_empty(self):
        raw = "> \n❯ \n$ "
        result = extract_response_regex(raw)
        assert result.text == ""

    def test_preserves_block_marker_content(self):
        """⏺ prefixed lines should have their text preserved in regex extraction."""
        raw = "⏺ Why do programmers prefer dark mode?\n\n  Because light attracts bugs."
        result = extract_response_regex(raw)
        assert "Why do programmers prefer dark mode?" in result.text
        assert "Because light attracts bugs." in result.text

    def test_filters_ai_thinking_artifact(self):
        """ai(thinking) should be filtered from regex extraction output."""
        raw = "The answer is 42.\nai(thinking)\nThat's the result."
        result = extract_response_regex(raw)
        assert "The answer is 42." in result.text
        assert "ai(thinking)" not in result.text

    def test_extracts_last_response_block(self):
        """A ⏺ text block after tool calls should be extracted, not the tool call."""
        raw = (
            "⏺ Bash(git push -u origin branch)\n"
            "  ⎿  remote: Create a pull request...\n"
            "     remote: https://github.com/...\n"
            "⏺ Done. Here's the summary:\n"
            "  Bug: ReferenceError in CompetitionScoreRow\n"
            "  Fix: Changed import styles to import useStyles\n"
            "  PR: https://github.com/example/pull/10 — merged into release/1.2.0"
        )
        result = extract_response_regex(raw)
        assert "Done. Here's the summary:" in result.text
        assert "Bug: ReferenceError" in result.text
        assert "PR: https://github.com/example/pull/10" in result.text
        assert "git push" not in result.text
        assert "remote: Create" not in result.text

    def test_extracts_short_answer_after_tool_calls(self):
        """A short answer following tool output should be extracted cleanly."""
        raw = (
            "⏺ Bash(gh pr view 10 --json state)\n"
            '  ⎿  {"mergeCommit": {"oid": "d4e59fd"}, "state": "MERGED"}\n'
            "⏺ I created the PR targeting release/1.2.0, as you requested."
            " The PR was merged into that branch."
        )
        result = extract_response_regex(raw)
        assert "I created the PR targeting release/1.2.0" in result.text
        assert "The PR was merged into that branch." in result.text
        assert "gh pr view" not in result.text
        assert "MERGED" not in result.text

    def test_fallback_strips_tool_blocks(self):
        """Without ⏺ markers, the fallback should still strip tool blocks."""
        raw = (
            "Starting work\n"
            "Bash(pytest tests/ -v)\n"
            "  ⎿  collected 42 items\n"
            "  ⎿  42 passed in 1.23s\n"
            "All tests pass."
        )
        result = extract_response_regex(raw)
        assert "All tests pass." in result.text
        assert "pytest tests/" not in result.text
        assert "42 passed" not in result.text


class TestExtractResponse:
    @pytest.mark.asyncio
    async def test_successful_extraction(self):
        mock_response = httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "I fixed the login bug by updating auth.py."}],
            },
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )
        with patch("agent_forge.response_extractor.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await extract_response(
                "Running tests...\nAll fixed\nDone.",
                api_key="test-key",
            )

        assert isinstance(result, ExtractionResult)
        assert result.text == "I fixed the login bug by updating auth.py."
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert call_kwargs[1]["headers"]["x-api-key"] == "test-key"

    @pytest.mark.asyncio
    async def test_api_error_returns_none(self):
        mock_response = httpx.Response(
            500,
            json={"error": {"message": "Internal server error"}},
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )
        with patch("agent_forge.response_extractor.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await extract_response(
                "Some output", api_key="test-key",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        with patch("agent_forge.response_extractor.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("timed out")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await extract_response(
                "Some output", api_key="test-key", timeout=1.0,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_output_returns_none(self):
        result = await extract_response("", api_key="test-key")
        assert result is None

    @pytest.mark.asyncio
    async def test_noise_only_returns_none(self):
        result = await extract_response("> \n❯ \n$ ", api_key="test-key")
        assert result is None

    @pytest.mark.asyncio
    async def test_custom_model_and_max_tokens(self):
        mock_response = httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "Response"}]},
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )
        with patch("agent_forge.response_extractor.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await extract_response(
                "Some output",
                api_key="test-key",
                model="claude-sonnet-4-5-20250929",
                max_tokens=100,
            )

        call_kwargs = mock_client.post.call_args
        body = call_kwargs[1]["json"]
        assert body["model"] == "claude-sonnet-4-5-20250929"
        assert body["max_tokens"] == 100
