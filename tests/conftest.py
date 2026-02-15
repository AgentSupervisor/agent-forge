"""Shared test fixtures."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_forge.config import DefaultsConfig, ForgeConfig, ProjectConfig
from agent_forge.registry import ProjectRegistry


@pytest.fixture
def tmp_git_repo(tmp_path):
    """Create a temporary directory that looks like a git repo."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    return tmp_path


@pytest.fixture
def sample_config_dict(tmp_git_repo):
    """Return a valid config dict with a real temp path."""
    return {
        "server": {"host": "127.0.0.1", "port": 9090, "secret_key": "test-secret"},
        "telegram": {"bot_token": "", "allowed_users": []},
        "defaults": {
            "max_agents_per_project": 3,
            "sandbox": True,
            "claude_command": "echo",
            "poll_interval_seconds": 1.0,
        },
        "projects": {
            "test-project": {
                "path": str(tmp_git_repo),
                "default_branch": "main",
                "max_agents": 2,
                "description": "Test project",
            }
        },
    }


@pytest.fixture
def sample_config(sample_config_dict):
    """Return a parsed ForgeConfig."""
    return ForgeConfig(**sample_config_dict)


@pytest.fixture
def config_file(tmp_path, sample_config_dict):
    """Write a config.yaml to a temp dir and return its path."""
    import yaml

    config_path = tmp_path / "config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(sample_config_dict, f)
    return str(config_path)


@pytest.fixture
def registry(config_file):
    """Return a ProjectRegistry loaded from the temp config."""
    return ProjectRegistry(config_path=config_file)
