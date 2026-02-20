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
    r"|^\s*⎿"                                  # Claude Code tool output marker
    r"|^\s*…\s*\+\d+\s+lines?\s*\(ctrl\+o"    # expand hint (… +N lines (ctrl+o to expand))
    r"|^(?:Bash|Read|Edit|Write|Grep|Glob|Task|MultiEdit|NotebookEdit|WebFetch|WebSearch|AskUser|Skill|EnterPlan|ExitPlan)\(.*"  # tool call headers
    r"|^(?:diff --git |index [0-9a-f]+\.\.[0-9a-f]+|--- a/|\+\+\+ b/)"  # git diff markers
    r"|^\s*remote:\s"                          # git remote output
    r"|^\s*\[[\w/.:-]+\s+[0-9a-f]{7,}\]"      # git commit output [branch hash] message
)

_BLOCK_MARKER_RE = re.compile(r"^\s*⏺\s?")

_TOOL_HEADER_RE = re.compile(
    r"^(?:Bash|Read|Edit|Write|Grep|Glob|Task|MultiEdit|NotebookEdit|"
    r"WebFetch|WebSearch|AskUser|Skill|EnterPlan|ExitPlan)\("
)

_TOOL_OUTPUT_RE = re.compile(r"^\s*⎿")

_SYSTEM_PROMPT = (
    "You are extracting an AI coding agent's response from raw terminal output. "
    "The terminal contains the agent's tool calls and their output mixed with "
    "the agent's actual response to the user.\n\n"
    "Claude Code uses this format:\n"
    "- Tool calls appear as: ToolName(args) followed by ⎿ output lines\n"
    "- The agent's text responses appear as plain text or after ⏺ markers\n"
    "- Tool names include: Bash, Read, Edit, Write, Grep, Glob, Task, etc.\n\n"
    "Extract ONLY the agent's final response text — the last message it wrote "
    "to communicate its results to the user. This is typically the text AFTER "
    "all tool calls have completed. Exclude:\n"
    "- Tool call invocations and their output (Bash(...), Read(...), ⎿ lines)\n"
    "- File contents being read or written\n"
    "- Command output (test results, build logs, git output, etc.)\n"
    "- Spinner lines, progress indicators, UI decorations\n"
    "- Status lines like 'Read file X' or 'Edit file Y'\n"
    "- Git diffs, commit hashes, remote push output\n\n"
    "Also extract any file paths (screenshots, generated images, documents) that the agent "
    "produced and that are relevant to share with the user.\n\n"
    "Return a JSON object with this exact shape:\n"
    '{"text": "the extracted response text", "files": ["/path/to/file.png"]}\n\n'
    "The 'text' field should contain the response text as-is, preserving formatting. "
    "The 'files' field should list absolute file paths the agent produced for the user. "
    "If no relevant files were produced, use an empty list. "
    "If you cannot identify a clear response, use the last meaningful text the agent produced."
)


def _strip_tool_blocks(lines: list[str]) -> list[str]:
    """Remove Claude Code tool call blocks (header + ⎿ output lines).

    A tool block starts with a tool invocation header (e.g. 'Bash(...)')
    and includes all subsequent lines starting with '⎿' (tool output marker).
    """
    result: list[str] = []
    in_tool_block = False
    for line in lines:
        if _TOOL_HEADER_RE.match(line):
            in_tool_block = True
            continue
        if in_tool_block:
            if _TOOL_OUTPUT_RE.match(line):
                continue
            # Also skip expand hints within tool blocks
            stripped = line.strip()
            if stripped.startswith("…") and "lines" in stripped and "ctrl+o" in stripped:
                continue
            in_tool_block = False
        result.append(line)
    return result


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
    meaningful = _strip_tool_blocks(meaningful)
    result_lines: list[str] = []
    total = 0
    for line in reversed(meaningful):
        if total + len(line) + 1 > 10000:
            break
        result_lines.insert(0, line)
        total += len(line) + 1
    return "\n".join(result_lines)


def extract_response_regex(raw: str) -> ExtractionResult:
    """Block-aware regex-based response extraction fallback.

    Identifies the last response block by looking for ⏺-delimited text
    sections that aren't tool calls. Falls back to last 30 meaningful
    lines with 200-char line truncation.
    """
    cleaned = _ANSI_RE.sub("", raw)
    lines = [ln for ln in cleaned.splitlines() if ln.strip()]

    # Try block-based extraction first: find the last ⏺ text block
    # that is NOT a tool call
    last_response_start = -1
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        # Check for ⏺ followed by text (not a tool call)
        marker_match = _BLOCK_MARKER_RE.match(stripped)
        if marker_match:
            after_marker = _BLOCK_MARKER_RE.sub("", stripped).strip()
            if after_marker and not _TOOL_HEADER_RE.match(after_marker):
                last_response_start = i
                break

    if last_response_start >= 0:
        # Extract from this block marker to the next tool call or block marker
        block_lines: list[str] = []
        for j in range(last_response_start, len(lines)):
            line = lines[j]
            stripped = line.strip()
            # Strip the ⏺ prefix from the first line
            if j == last_response_start:
                line = _BLOCK_MARKER_RE.sub("", line)
                if not line.strip():
                    continue
            elif _BLOCK_MARKER_RE.match(stripped):
                # Next block — check if it's a continuation or new block
                after = _BLOCK_MARKER_RE.sub("", stripped).strip()
                if _TOOL_HEADER_RE.match(after):
                    break  # Tool call block — stop
                if after:
                    break  # Another text block — stop
                continue  # Bare ⏺ — skip
            elif _TOOL_HEADER_RE.match(stripped):
                break
            elif _TOOL_OUTPUT_RE.match(stripped):
                break
            # Filter noise within the block
            if _NOISE_RE.match(line):
                continue
            block_lines.append(line[:200])

        if block_lines:
            return ExtractionResult(text="\n".join(block_lines))

    # Fallback: strip tool blocks and take last 30 lines
    lines = [_BLOCK_MARKER_RE.sub("", ln) for ln in lines]
    lines = [ln for ln in lines if ln.strip()]
    meaningful = [ln for ln in lines if not _NOISE_RE.match(ln)]
    meaningful = _dedup_consecutive(meaningful)
    meaningful = _strip_tool_blocks(meaningful)
    if not meaningful:
        return ExtractionResult(text="")
    tail = [ln[:200] for ln in meaningful[-30:]]
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
