"""Tests for the LLM-based activity summarizer."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agent_forge.summarizer import _preprocess_output, summarize_output


class TestPreprocessOutput:
    """Test _preprocess_output filtering and truncation."""

    def test_strips_ansi_codes(self):
        raw = "\x1b[32mHello\x1b[0m world"
        result = _preprocess_output(raw)
        assert "\x1b" not in result
        assert "Hello" in result

    def test_filters_noise_lines(self):
        raw = "Real output\n> \n⠋ Loading...\n────────────\nDone."
        result = _preprocess_output(raw)
        assert "Real output" in result
        assert "Done." in result
        assert "⠋" not in result
        assert "────" not in result

    def test_keeps_last_80_lines(self):
        lines = [f"line {i}" for i in range(100)]
        raw = "\n".join(lines)
        result = _preprocess_output(raw)
        result_lines = result.splitlines()
        assert len(result_lines) <= 80
        assert "line 99" in result_lines[-1]

    def test_empty_input(self):
        assert _preprocess_output("") == ""

    def test_all_noise(self):
        raw = "> \n❯ \n$ \n⠋ spin"
        result = _preprocess_output(raw)
        assert result.strip() == ""


class TestSummarizeOutput:
    """Test summarize_output with mocked httpx."""

    @pytest.mark.asyncio
    async def test_successful_summary(self):
        mock_response = httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "Agent fixed 3 test failures."}],
            },
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )
        with patch("agent_forge.summarizer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await summarize_output(
                "Running tests...\n3 tests fixed\nAll passing",
                api_key="test-key",
            )

        assert result == "Agent fixed 3 test failures."
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
        with patch("agent_forge.summarizer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await summarize_output(
                "Some output", api_key="test-key",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        with patch("agent_forge.summarizer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = httpx.TimeoutException("timed out")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await summarize_output(
                "Some output", api_key="test-key", timeout=1.0,
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_empty_output_returns_none(self):
        result = await summarize_output("", api_key="test-key")
        assert result is None

    @pytest.mark.asyncio
    async def test_noise_only_output_returns_none(self):
        result = await summarize_output("> \n❯ \n$ ", api_key="test-key")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_content_blocks_returns_none(self):
        mock_response = httpx.Response(
            200,
            json={"content": []},
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )
        with patch("agent_forge.summarizer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await summarize_output(
                "Some output", api_key="test-key",
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_custom_model_and_max_tokens(self):
        mock_response = httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "Summary"}]},
            request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
        )
        with patch("agent_forge.summarizer.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_response
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            await summarize_output(
                "Some output",
                api_key="test-key",
                model="claude-sonnet-4-5-20250929",
                max_tokens=100,
            )

        call_kwargs = mock_client.post.call_args
        body = call_kwargs[1]["json"]
        assert body["model"] == "claude-sonnet-4-5-20250929"
        assert body["max_tokens"] == 100
