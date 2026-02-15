"""LLM-based activity summarization using the Anthropic Messages API."""

from __future__ import annotations

import logging
import re

import httpx

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_NOISE_RE = re.compile(
    r"^\s*[>❯$#]\s*$"                  # bare prompt chars
    r"|^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷]"  # Unicode spinners
    r"|^\s*[|/\-\\]\s\S.{0,30}$"       # ASCII spinners (short lines only)
    r"|^[\s─━─=~_*]{6,}$"              # separator lines (no hyphen — handled below)
    r"|^[\s\-]{6,}$"                    # dash-only separator lines
    r"|^\s*⏵"                           # Claude Code UI chrome
    r"|^\s*[❯>]\s+\S"                  # Claude Code tool invocations
    r"|^\s*✻"                           # Claude Code thinking indicator
)

_SYSTEM_PROMPT = (
    "You are a concise status reporter for a software engineering agent. "
    "Given terminal output from a coding agent session, extract a short summary "
    "of what happened. Focus on: what the agent did, what was the result, "
    "are there errors or blockers, what does the agent need next. "
    "Write 2-5 concise lines in plain text. Do not fabricate information. "
    "If the output is unclear or empty, say so briefly."
)


def _preprocess_output(raw: str) -> str:
    """Strip ANSI codes, filter noise, and take the last 80 meaningful lines."""
    cleaned = _ANSI_RE.sub("", raw)
    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    meaningful = [ln for ln in lines if not _NOISE_RE.match(ln)]
    tail = meaningful[-80:]
    return "\n".join(tail)


async def summarize_output(
    output: str,
    *,
    api_key: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 300,
    timeout: float = 10.0,
) -> str | None:
    """Call the Anthropic Messages API to summarize agent terminal output.

    Returns the summary string, or None on any failure.
    """
    preprocessed = _preprocess_output(output)
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
                                "Summarize this agent's terminal output:\n\n"
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
        logger.debug("Summarizer timed out after %.1fs", timeout)
        return None
    except Exception:
        logger.debug("Summarizer failed", exc_info=True)
        return None
