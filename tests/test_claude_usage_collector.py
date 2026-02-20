"""Tests for Claude Code usage collector."""

import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from agent_forge.claude_usage_collector import (
    ClaudeUsageCollector,
    ClaudeUsageSnapshot,
    ModelUsage,
    SessionBlock,
    MODEL_PRICING,
)


@pytest.fixture
def sample_jsonl_data():
    """Create sample JSONL entries mimicking Claude Code output."""
    now = datetime.now(timezone.utc)
    entries = []

    # Entry 1: Opus response
    entries.append(json.dumps({
        "type": "assistant",
        "message": {
            "model": "claude-opus-4-6",
            "id": "msg_001",
            "type": "message",
            "role": "assistant",
            "usage": {
                "input_tokens": 1000,
                "cache_creation_input_tokens": 500,
                "cache_read_input_tokens": 200,
                "output_tokens": 300,
            },
        },
        "requestId": "req_001",
        "timestamp": (now - timedelta(minutes=30)).isoformat(),
        "sessionId": "session-123",
    }))

    # Entry 2: Sonnet response
    entries.append(json.dumps({
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-6",
            "id": "msg_002",
            "type": "message",
            "role": "assistant",
            "usage": {
                "input_tokens": 2000,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 1000,
                "output_tokens": 500,
            },
        },
        "requestId": "req_002",
        "timestamp": (now - timedelta(minutes=15)).isoformat(),
        "sessionId": "session-123",
    }))

    # Entry 3: User message (should be skipped)
    entries.append(json.dumps({
        "type": "user",
        "message": {"content": "Hello"},
        "timestamp": (now - timedelta(minutes=20)).isoformat(),
    }))

    # Entry 4: Haiku response
    entries.append(json.dumps({
        "type": "assistant",
        "message": {
            "model": "claude-haiku-4-5-20251001",
            "id": "msg_003",
            "type": "message",
            "role": "assistant",
            "usage": {
                "input_tokens": 500,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "output_tokens": 100,
            },
        },
        "requestId": "req_003",
        "timestamp": (now - timedelta(minutes=5)).isoformat(),
        "sessionId": "session-456",
    }))

    return "\n".join(entries)


@pytest.fixture
def mock_data_dir(tmp_path, sample_jsonl_data):
    """Create a mock Claude data directory with JSONL files."""
    project_dir = tmp_path / "projects" / "-Users-test-project"
    project_dir.mkdir(parents=True)

    jsonl_file = project_dir / "session-123.jsonl"
    jsonl_file.write_text(sample_jsonl_data)

    return tmp_path / "projects"


class TestClaudeUsageCollector:
    def test_init_with_valid_path(self, mock_data_dir):
        collector = ClaudeUsageCollector(data_path=mock_data_dir)
        assert collector.data_path == mock_data_dir

    def test_init_no_data_path(self, tmp_path):
        # Use a path that doesn't exist
        collector = ClaudeUsageCollector(data_path=tmp_path / "nonexistent")
        assert collector.data_path == tmp_path / "nonexistent"

    def test_collect_returns_snapshot(self, mock_data_dir):
        collector = ClaudeUsageCollector(data_path=mock_data_dir)
        snapshot = collector.collect(hours_back=1)
        assert isinstance(snapshot, ClaudeUsageSnapshot)
        assert snapshot.timestamp > 0

    def test_collect_finds_entries(self, mock_data_dir):
        collector = ClaudeUsageCollector(data_path=mock_data_dir)
        snapshot = collector.collect(hours_back=1)
        # Should find 3 assistant entries (skips the user message)
        assert snapshot.total_tokens_24h > 0
        assert snapshot.total_cost_24h > 0

    def test_collect_skips_user_messages(self, mock_data_dir):
        collector = ClaudeUsageCollector(data_path=mock_data_dir)
        snapshot = collector.collect(hours_back=1)
        # 3 assistant entries: 1000+300 + 2000+500 + 500+100 = 4400 total tokens
        expected_tokens = (1000 + 300) + (2000 + 500) + (500 + 100)
        assert snapshot.total_tokens_24h == expected_tokens

    def test_collect_per_model_breakdown(self, mock_data_dir):
        collector = ClaudeUsageCollector(data_path=mock_data_dir)
        snapshot = collector.collect(hours_back=1)
        assert snapshot.current_block is not None
        models = snapshot.current_block.models
        assert "claude-opus-4-6" in models
        assert "claude-sonnet-4-6" in models
        assert models["claude-opus-4-6"].input_tokens == 1000
        assert models["claude-opus-4-6"].output_tokens == 300
        assert models["claude-sonnet-4-6"].input_tokens == 2000
        assert models["claude-sonnet-4-6"].output_tokens == 500

    def test_collect_empty_directory(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        collector = ClaudeUsageCollector(data_path=empty_dir)
        snapshot = collector.collect()
        assert snapshot.total_tokens_24h == 0
        assert snapshot.total_cost_24h == 0.0
        assert snapshot.current_block is None

    def test_collect_no_data_path(self):
        collector = ClaudeUsageCollector.__new__(ClaudeUsageCollector)
        collector.data_path = None
        snapshot = collector.collect()
        assert snapshot.total_tokens_24h == 0

    def test_cost_calculation_opus(self):
        collector = ClaudeUsageCollector.__new__(ClaudeUsageCollector)
        collector.data_path = None
        cost = collector._calculate_cost(
            "claude-opus-4-6",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_creation=0,
            cache_read=0,
        )
        # $15 input + $75 output = $90
        assert abs(cost - 90.0) < 0.01

    def test_cost_calculation_sonnet(self):
        collector = ClaudeUsageCollector.__new__(ClaudeUsageCollector)
        collector.data_path = None
        cost = collector._calculate_cost(
            "claude-sonnet-4-6",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cache_creation=0,
            cache_read=0,
        )
        # $3 input + $15 output = $18
        assert abs(cost - 18.0) < 0.01

    def test_cost_calculation_with_cache(self):
        collector = ClaudeUsageCollector.__new__(ClaudeUsageCollector)
        collector.data_path = None
        cost = collector._calculate_cost(
            "claude-opus-4-6",
            input_tokens=0,
            output_tokens=0,
            cache_creation=1_000_000,
            cache_read=1_000_000,
        )
        # $18.75 cache_create + $1.50 cache_read = $20.25
        assert abs(cost - 20.25) < 0.01

    def test_model_pricing_fuzzy_match(self):
        pricing = ClaudeUsageCollector._get_pricing("claude-opus-4-6")
        assert pricing is not None
        assert pricing["input"] == 15.0

        # Fuzzy match
        pricing = ClaudeUsageCollector._get_pricing("some-opus-variant")
        assert pricing is not None
        assert pricing["input"] == 15.0

        pricing = ClaudeUsageCollector._get_pricing("unknown-model")
        assert pricing is None

    def test_deduplication(self, tmp_path):
        """Entries with same message_id:request_id should be deduplicated."""
        now = datetime.now(timezone.utc)
        project_dir = tmp_path / "projects" / "-test"
        project_dir.mkdir(parents=True)

        entry = json.dumps({
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-4-6",
                "id": "msg_dup",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            "requestId": "req_dup",
            "timestamp": now.isoformat(),
        })

        # Write same entry twice
        (project_dir / "s1.jsonl").write_text(entry + "\n" + entry)

        collector = ClaudeUsageCollector(data_path=tmp_path / "projects")
        snapshot = collector.collect(hours_back=1)
        # Should count only once
        assert snapshot.total_tokens_24h == 150

    def test_session_blocks(self, mock_data_dir):
        collector = ClaudeUsageCollector(data_path=mock_data_dir)
        snapshot = collector.collect(hours_back=24)
        assert len(snapshot.blocks) >= 1
        # Current block should be active
        active_blocks = [b for b in snapshot.blocks if b.is_active]
        assert len(active_blocks) == 1

    def test_malformed_json_handling(self, tmp_path):
        """Malformed JSON lines should be skipped gracefully."""
        project_dir = tmp_path / "projects" / "-test"
        project_dir.mkdir(parents=True)

        now = datetime.now(timezone.utc)
        valid = json.dumps({
            "type": "assistant",
            "message": {
                "model": "claude-sonnet-4-6",
                "id": "msg_ok",
                "usage": {"input_tokens": 100, "output_tokens": 50},
            },
            "requestId": "req_ok",
            "timestamp": now.isoformat(),
        })

        content = "not valid json\n" + valid + "\n{broken\n"
        (project_dir / "test.jsonl").write_text(content)

        collector = ClaudeUsageCollector(data_path=tmp_path / "projects")
        snapshot = collector.collect(hours_back=1)
        assert snapshot.total_tokens_24h == 150
