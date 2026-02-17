"""Tests for the LLM-based response extractor."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from agent_forge.response_extractor import (
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


class TestExtractResponseRegex:
    def test_returns_last_50_meaningful_lines(self):
        lines = [f"meaningful line {i}" for i in range(100)]
        raw = "\n".join(lines)
        result = extract_response_regex(raw)
        result_lines = result.splitlines()
        assert len(result_lines) <= 50
        assert "meaningful line 99" in result_lines[-1]

    def test_truncates_at_200_chars(self):
        raw = "x" * 300
        result = extract_response_regex(raw)
        for line in result.splitlines():
            assert len(line) <= 200

    def test_filters_noise(self):
        raw = "Real output\n> \n⠋ spin\nMore output"
        result = extract_response_regex(raw)
        assert "Real output" in result
        assert "More output" in result
        assert "⠋" not in result

    def test_empty_input(self):
        assert extract_response_regex("") == ""

    def test_all_noise_returns_empty(self):
        raw = "> \n❯ \n$ "
        assert extract_response_regex(raw) == ""


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

        assert result == "I fixed the login bug by updating auth.py."
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
