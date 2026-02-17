"""LLM-based response extraction from agent terminal output."""

from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(
    r"\x1b"
    r"(?:"
    r"\[[0-9;?]*[a-zA-Z]"         # CSI sequences (including DEC private modes like ?2026h)
    r"|\][^\x07]*\x07"            # OSC terminated by BEL (e.g. window title)
    r"|\][^\x1b]*\x1b\\"          # OSC terminated by ST (ESC \)
    r"|[()#][0-9a-zA-Z]"          # Character set / line attrs
    r"|[a-zA-Z><=]"               # Simple ESC sequences
    r")"
)

_NOISE_RE = re.compile(
    r"^\s*[>❯$#]\s*$"
    r"|^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷]"
    r"|^\s*[|/\-\\]\s\S.{0,30}$"
    r"|^[\s─━─=~_*]{6,}$"
    r"|^[\s\-]{6,}$"
    r"|^\s*⏵"
    r"|^\s*[❯>]\s+\S"
    r"|^\s*[✢-✿]"
)

_SYSTEM_PROMPT = (
    "You are extracting an AI coding agent's response from raw terminal output. "
    "The terminal contains tool calls, file contents, command output, spinner artifacts, "
    "and UI chrome mixed with the agent's actual response to the user.\n\n"
    "Extract ONLY the agent's final response text — the message it wrote to communicate "
    "its results to the user. Exclude:\n"
    "- Tool call invocations and their output\n"
    "- File contents being read or written\n"
    "- Command output (test results, build logs, etc.)\n"
    "- Spinner lines, progress indicators, UI decorations\n"
    "- Status lines like 'Read file X' or 'Edit file Y'\n\n"
    "Return the response text as-is, preserving formatting. "
    "If you cannot identify a clear response, return the last meaningful "
    "text the agent produced."
)


def preprocess_output(raw: str) -> str:
    """Strip ANSI codes, filter noise, and take the last ~10K chars of meaningful content."""
    cleaned = _ANSI_RE.sub("", raw)
    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    meaningful = [ln for ln in lines if not _NOISE_RE.match(ln)]
    result_lines: list[str] = []
    total = 0
    for line in reversed(meaningful):
        if total + len(line) + 1 > 10000:
            break
        result_lines.insert(0, line)
        total += len(line) + 1
    return "\n".join(result_lines)


def extract_response_regex(raw: str) -> str:
    """Improved regex-based response extraction fallback.

    More generous than the old 15-line summary: takes up to 50 meaningful lines
    with 200-char line truncation.
    """
    cleaned = _ANSI_RE.sub("", raw)
    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    meaningful = [ln for ln in lines if not _NOISE_RE.match(ln)]
    if not meaningful:
        return ""
    tail = [ln[:200] for ln in meaningful[-50:]]
    return "\n".join(tail)


async def extract_response(
    raw_output: str,
    *,
    api_key: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 4000,
    timeout: float = 15.0,
) -> str | None:
    """Call the Anthropic Messages API to extract the agent's response from terminal output.

    Returns the extracted response text, or None on any failure.
    """
    preprocessed = preprocess_output(raw_output)
    if not preprocessed.strip():
        return None

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "system": _SYSTEM_PROMPT,
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "Extract the agent's response from this terminal output:\n\n"
                                f"```\n{preprocessed}\n```"
                            ),
                        }
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            blocks = data.get("content", [])
            text_parts = [b["text"] for b in blocks if b.get("type") == "text"]
            if text_parts:
                return "\n".join(text_parts).strip()
            return None
    except httpx.TimeoutException:
        logger.debug("Response extractor timed out after %.1fs", timeout)
        return None
    except Exception:
        logger.debug("Response extractor failed", exc_info=True)
        return None
