"""LLM-based response extraction from agent terminal output."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import httpx


@dataclass
class ExtractionResult:
    text: str
    file_paths: list[str] = field(default_factory=list)

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
    r"|.*\bChannelling\b"                      # Claude Code "Channelling…" status
    r"|^\s*⏺\s*$"                              # Claude Code bare status dot (no content after)
    r"|^\s*[·.…↑↓←→]{1,}\s*$"                 # terminal artifacts: arrows, dots, middots
    r"|^\s*·\s+\S+…\s*$"                      # Claude Code churning status (e.g. "· Scurrying…")
    r"|^\s*\S{1,4}\s*$"                        # very short (1-4 char) fragment lines
    r"|^\s*\w+…\s*$"                           # single-word status text ending in …
    r"|^\s*\w*\(thinking\)\s*$"                # Claude thinking indicator (e.g. "(thinking)", "ai(thinking)")
    r"|^\s*Thinking\.*\s*$"                    # Claude "Thinking..." status
    r"|^\s*claude-\S+\s*$"                     # bare model name lines (e.g. claude-sonnet-4-6)
    r"|^\s*\d+[,.]?\d*\s*tokens?\s*$"         # token count lines
)

_BLOCK_MARKER_RE = re.compile(r"^\s*⏺\s?")

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
    "Also extract any file paths (screenshots, generated images, documents) that the agent "
    "produced and that are relevant to share with the user.\n\n"
    "Return a JSON object with this exact shape:\n"
    '{"text": "the extracted response text", "files": ["/path/to/file.png"]}\n\n'
    "The 'text' field should contain the response text as-is, preserving formatting. "
    "The 'files' field should list absolute file paths the agent produced for the user. "
    "If no relevant files were produced, use an empty list. "
    "If you cannot identify a clear response, use the last meaningful text the agent produced."
)


def _dedup_consecutive(lines: list[str]) -> list[str]:
    """Remove consecutive duplicate lines (terminal redraws)."""
    if not lines:
        return lines
    result = [lines[0]]
    for line in lines[1:]:
        if line.strip() != result[-1].strip():
            result.append(line)
    return result


def preprocess_output(raw: str) -> str:
    """Strip ANSI codes, filter noise, and take the last ~10K chars of meaningful content."""
    cleaned = _ANSI_RE.sub("", raw)
    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    # Strip Claude Code block marker (⏺) prefix — preserve the text that follows
    lines = [_BLOCK_MARKER_RE.sub("", ln) for ln in lines]
    lines = [ln for ln in lines if ln.strip()]
    meaningful = [ln for ln in lines if not _NOISE_RE.match(ln)]
    meaningful = _dedup_consecutive(meaningful)
    result_lines: list[str] = []
    total = 0
    for line in reversed(meaningful):
        if total + len(line) + 1 > 10000:
            break
        result_lines.insert(0, line)
        total += len(line) + 1
    return "\n".join(result_lines)


def extract_response_regex(raw: str) -> ExtractionResult:
    """Improved regex-based response extraction fallback.

    More generous than the old 15-line summary: takes up to 50 meaningful lines
    with 200-char line truncation.
    """
    cleaned = _ANSI_RE.sub("", raw)
    lines = [ln for ln in cleaned.splitlines() if ln.strip()]
    # Strip Claude Code block marker (⏺) prefix — preserve the text that follows
    lines = [_BLOCK_MARKER_RE.sub("", ln) for ln in lines]
    lines = [ln for ln in lines if ln.strip()]
    meaningful = [ln for ln in lines if not _NOISE_RE.match(ln)]
    meaningful = _dedup_consecutive(meaningful)
    if not meaningful:
        return ExtractionResult(text="")
    tail = [ln[:200] for ln in meaningful[-50:]]
    return ExtractionResult(text="\n".join(tail))


async def extract_response(
    raw_output: str,
    *,
    api_key: str,
    model: str = "claude-haiku-4-5-20251001",
    max_tokens: int = 4000,
    timeout: float = 15.0,
    user_question: str = "",
) -> ExtractionResult | None:
    """Call the Anthropic Messages API to extract the agent's response from terminal output.

    Returns an ExtractionResult with the extracted text and any file paths, or None on failure.
    """
    preprocessed = preprocess_output(raw_output)
    if not preprocessed.strip():
        return None

    user_content = "Extract the agent's response from this terminal output:\n\n"
    if user_question:
        user_content += f"The user asked: '{user_question}'\n\n"
    user_content += f"```\n{preprocessed}\n```"

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
                            "content": user_content,
                        }
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            blocks = data.get("content", [])
            text_parts = [b["text"] for b in blocks if b.get("type") == "text"]
            if not text_parts:
                return None
            raw_text = "\n".join(text_parts).strip()
            try:
                parsed = json.loads(raw_text)
                return ExtractionResult(
                    text=parsed.get("text", raw_text),
                    file_paths=parsed.get("files", []),
                )
            except (json.JSONDecodeError, TypeError):
                return ExtractionResult(text=raw_text)
    except httpx.TimeoutException:
        logger.debug("Response extractor timed out after %.1fs", timeout)
        return None
    except Exception:
        logger.debug("Response extractor failed", exc_info=True)
        return None
