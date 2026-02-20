"""Claude Code token usage collector — reads JSONL conversation logs."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Pricing per million tokens (as of 2025)
MODEL_PRICING = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0, "cache_create": 18.75, "cache_read": 1.50},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_create": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5-20251001": {"input": 0.25, "output": 1.25, "cache_create": 0.30, "cache_read": 0.03},
    # Fallback aliases - map common prefixes
}

CLAUDE_DATA_PATHS = [
    Path.home() / ".claude" / "projects",
    Path.home() / ".config" / "claude" / "projects",
]

SESSION_BLOCK_HOURS = 5  # Anthropic uses 5-hour rate limit windows


class ModelUsage(BaseModel):
    """Token usage aggregated by model."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    entry_count: int = 0


class SessionBlock(BaseModel):
    """Token usage for a single 5-hour rate-limit window."""

    start_time: str  # ISO format
    end_time: str  # ISO format
    is_active: bool
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_creation_tokens: int = 0
    total_cache_read_tokens: int = 0
    total_cost_usd: float = 0.0
    message_count: int = 0
    models: dict[str, ModelUsage] = {}
    burn_rate_tokens_per_min: float | None = None
    burn_rate_cost_per_hour: float | None = None


class ClaudeUsageSnapshot(BaseModel):
    """Point-in-time snapshot of Claude Code token usage."""

    timestamp: float
    current_block: SessionBlock | None = None
    total_tokens_24h: int = 0
    total_cost_24h: float = 0.0
    blocks: list[SessionBlock] = []


class ClaudeUsageCollector:
    """Collects Claude Code token usage from local JSONL conversation logs."""

    def __init__(self, data_path: Path | None = None) -> None:
        # Find the first existing data path from CLAUDE_DATA_PATHS, or use provided
        self.data_path = data_path
        if not self.data_path:
            for p in CLAUDE_DATA_PATHS:
                if p.exists():
                    self.data_path = p
                    break
        if self.data_path:
            logger.info("Claude usage collector: data path = %s", self.data_path)
        else:
            logger.warning("Claude usage collector: no Claude data directory found")

    def collect(self, hours_back: int = 24) -> ClaudeUsageSnapshot:
        """Collect usage data for the last N hours."""
        if not self.data_path or not self.data_path.exists():
            return ClaudeUsageSnapshot(timestamp=time.time())

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        entries = self._load_entries(cutoff)
        blocks = self._create_session_blocks(entries)

        # Calculate totals
        total_tokens = sum(e["input_tokens"] + e["output_tokens"] for e in entries)
        total_cost = sum(e["cost_usd"] for e in entries)

        # Find the active (current) block
        current_block = None
        for block in blocks:
            if block.is_active:
                current_block = block
                break

        return ClaudeUsageSnapshot(
            timestamp=time.time(),
            current_block=current_block,
            total_tokens_24h=total_tokens,
            total_cost_24h=total_cost,
            blocks=blocks,
        )

    def _load_entries(self, cutoff: datetime) -> list[dict]:
        """Load and parse JSONL entries after the cutoff time."""
        entries = []
        seen: set[str] = set()  # Dedup by message_id + request_id

        for jsonl_file in self.data_path.rglob("*.jsonl"):
            # Quick filter: skip files older than cutoff based on mtime
            try:
                mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=timezone.utc)
                if mtime < cutoff:
                    continue
            except OSError:
                continue

            try:
                with open(jsonl_file, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        if data.get("type") != "assistant":
                            continue

                        entry = self._extract_entry(data)
                        if entry is None:
                            continue

                        if entry["timestamp"] < cutoff:
                            continue

                        # Dedup
                        dedup_key = f"{entry.get('message_id', '')}:{entry.get('request_id', '')}"
                        if dedup_key in seen:
                            continue
                        seen.add(dedup_key)

                        entries.append(entry)
            except (OSError, IOError) as exc:
                logger.debug("Failed to read %s: %s", jsonl_file, exc)

        entries.sort(key=lambda e: e["timestamp"])
        return entries

    def _extract_entry(self, data: dict) -> dict | None:
        """Extract token usage from a single JSONL assistant entry."""
        # Get usage dict - try message.usage first, then usage, then top-level
        usage = None
        message = data.get("message", {})
        if isinstance(message, dict):
            usage = message.get("usage")
        if not usage:
            usage = data.get("usage")
        if not usage or not isinstance(usage, dict):
            return None

        # Extract tokens (handle multiple naming conventions)
        input_tokens = (
            usage.get("input_tokens")
            or usage.get("inputTokens")
            or usage.get("prompt_tokens")
            or 0
        )
        output_tokens = (
            usage.get("output_tokens")
            or usage.get("outputTokens")
            or usage.get("completion_tokens")
            or 0
        )
        cache_creation = (
            usage.get("cache_creation_input_tokens")
            or usage.get("cache_creation_tokens")
            or usage.get("cacheCreationInputTokens")
            or 0
        )
        cache_read = (
            usage.get("cache_read_input_tokens")
            or usage.get("cache_read_tokens")
            or usage.get("cacheReadInputTokens")
            or 0
        )

        # Skip entries with no tokens at all
        if input_tokens == 0 and output_tokens == 0:
            return None

        # Get model name
        model = ""
        if isinstance(message, dict):
            model = message.get("model", "")
        if not model:
            model = data.get("model", "")

        # Get timestamp
        ts_str = data.get("timestamp", "")
        try:
            timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

        # Calculate cost
        cost = self._calculate_cost(model, input_tokens, output_tokens, cache_creation, cache_read)

        return {
            "timestamp": timestamp,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_tokens": cache_creation,
            "cache_read_tokens": cache_read,
            "model": model,
            "cost_usd": cost,
            "message_id": message.get("id", "") if isinstance(message, dict) else "",
            "request_id": data.get("requestId", ""),
        }

    def _calculate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_creation: int,
        cache_read: int,
    ) -> float:
        """Calculate cost in USD for given token counts."""
        pricing = self._get_pricing(model)
        if not pricing:
            return 0.0

        cost = (
            (input_tokens / 1_000_000) * pricing["input"]
            + (output_tokens / 1_000_000) * pricing["output"]
            + (cache_creation / 1_000_000) * pricing["cache_create"]
            + (cache_read / 1_000_000) * pricing["cache_read"]
        )
        return cost

    @staticmethod
    def _get_pricing(model: str) -> dict | None:
        """Get pricing for a model, with fuzzy matching."""
        if model in MODEL_PRICING:
            return MODEL_PRICING[model]
        # Try prefix matching
        model_lower = model.lower()
        if "opus" in model_lower:
            return MODEL_PRICING["claude-opus-4-6"]
        if "sonnet" in model_lower:
            return MODEL_PRICING["claude-sonnet-4-6"]
        if "haiku" in model_lower:
            return MODEL_PRICING["claude-haiku-4-5-20251001"]
        return None

    def _create_session_blocks(self, entries: list[dict]) -> list[SessionBlock]:
        """Group entries into 5-hour session blocks."""
        if not entries:
            return []

        now = datetime.now(timezone.utc)
        blocks: list[SessionBlock] = []

        # Determine block boundaries.
        # Current block starts at the most recent 5-hour boundary aligned to midnight UTC.
        current_block_start = now.replace(
            hour=(now.hour // SESSION_BLOCK_HOURS) * SESSION_BLOCK_HOURS,
            minute=0,
            second=0,
            microsecond=0,
        )

        # Create blocks going back to cover the earliest entry
        block_starts: list[datetime] = []
        block_start = current_block_start
        earliest = entries[0]["timestamp"]
        while block_start + timedelta(hours=SESSION_BLOCK_HOURS) > earliest:
            block_starts.append(block_start)
            block_start -= timedelta(hours=SESSION_BLOCK_HOURS)
        block_starts.reverse()

        for start in block_starts:
            end = start + timedelta(hours=SESSION_BLOCK_HOURS)
            is_active = start <= now < end

            block_entries = [e for e in entries if start <= e["timestamp"] < end]
            if not block_entries:
                continue

            # Aggregate per-model stats
            model_stats: dict[str, ModelUsage] = {}
            for entry in block_entries:
                m = entry["model"] or "unknown"
                if m not in model_stats:
                    model_stats[m] = ModelUsage(model=m)
                ms = model_stats[m]
                ms.input_tokens += entry["input_tokens"]
                ms.output_tokens += entry["output_tokens"]
                ms.cache_creation_tokens += entry["cache_creation_tokens"]
                ms.cache_read_tokens += entry["cache_read_tokens"]
                ms.cost_usd += entry["cost_usd"]
                ms.entry_count += 1

            total_input = sum(e["input_tokens"] for e in block_entries)
            total_output = sum(e["output_tokens"] for e in block_entries)
            total_cache_create = sum(e["cache_creation_tokens"] for e in block_entries)
            total_cache_read = sum(e["cache_read_tokens"] for e in block_entries)
            total_cost = sum(e["cost_usd"] for e in block_entries)

            # Burn rate (only for active block with at least two entries)
            burn_rate_tpm = None
            burn_rate_cph = None
            if is_active and len(block_entries) >= 2:
                first_ts = block_entries[0]["timestamp"]
                last_ts = block_entries[-1]["timestamp"]
                duration_min = (last_ts - first_ts).total_seconds() / 60
                if duration_min > 0:
                    total_tokens = total_input + total_output
                    burn_rate_tpm = total_tokens / duration_min
                    burn_rate_cph = (total_cost / duration_min) * 60

            block = SessionBlock(
                start_time=start.isoformat(),
                end_time=end.isoformat(),
                is_active=is_active,
                total_input_tokens=total_input,
                total_output_tokens=total_output,
                total_cache_creation_tokens=total_cache_create,
                total_cache_read_tokens=total_cache_read,
                total_cost_usd=total_cost,
                message_count=len(block_entries),
                models=model_stats,
                burn_rate_tokens_per_min=burn_rate_tpm,
                burn_rate_cost_per_hour=burn_rate_cph,
            )
            blocks.append(block)

        return blocks
