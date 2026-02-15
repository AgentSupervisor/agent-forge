"""Tests for config loading and ProjectRegistry."""

import tempfile
from pathlib import Path

import pytest
import yaml

from agent_forge.config import ForgeConfig, ProjectConfig
from agent_forge.registry import ProjectRegistry


class TestForgeConfig:
    def test_default_config(self):
        config = ForgeConfig()
        assert config.server.host == "0.0.0.0"
        assert config.server.port == 8080
        assert config.defaults.max_agents_per_project == 5
        assert config.projects == {}

    def test_path_expansion(self):
        project = ProjectConfig(path="~/some/path")
        assert "~" not in project.path
        assert project.path.startswith("/")

    def test_get_max_agents_project_override(self):
        config = ForgeConfig(
            projects={"test": ProjectConfig(path="/tmp", max_agents=10)}
        )
        assert config.get_max_agents("test") == 10

    def test_get_max_agents_falls_back_to_default(self):
        config = ForgeConfig(
            projects={"test": ProjectConfig(path="/tmp")}
        )
        assert config.get_max_agents("test") == 5


class TestProjectRegistry:
    def test_load_valid_config(self, registry):
        projects = registry.list_projects()
        assert "test-project" in projects

    def test_get_project(self, registry):
        project = registry.get_project("test-project")
        assert project.description == "Test project"
        assert project.default_branch == "main"

    def test_get_nonexistent_project(self, registry):
        with pytest.raises(KeyError):
            registry.get_project("nonexistent")

    def test_missing_config_file(self):
        with pytest.raises(FileNotFoundError):
            ProjectRegistry(config_path="/nonexistent/config.yaml")

    def test_reload(self, config_file, registry):
        # Initial state
        assert "test-project" in registry.list_projects()
        # Modify config and reload
        with open(config_file) as f:
            data = yaml.safe_load(f)
        data["projects"]["new-project"] = {
            "path": data["projects"]["test-project"]["path"],
            "default_branch": "main",
            "description": "New project",
        }
        with open(config_file, "w") as f:
            yaml.dump(data, f)
        registry.reload()
        assert "new-project" in registry.list_projects()

    def test_empty_config_file(self, tmp_path):
        config_path = tmp_path / "empty.yaml"
        config_path.write_text("")
        reg = ProjectRegistry(config_path=str(config_path))
        assert reg.list_projects() == {}

    def test_save_writes_valid_yaml(self, config_file, registry):
        """Test that save() writes valid YAML that can be re-loaded."""
        # Save current config to disk
        registry.save()

        # Read the file back and parse as YAML
        with open(config_file) as f:
            raw = yaml.safe_load(f)

        # Verify it's valid YAML with expected structure
        assert isinstance(raw, dict)
        assert "server" in raw
        assert "defaults" in raw
        assert "projects" in raw
        assert "test-project" in raw["projects"]

        # Verify we can create a new registry from the saved file
        new_registry = ProjectRegistry(config_path=config_file)
        assert "test-project" in new_registry.list_projects()
        project = new_registry.get_project("test-project")
        assert project.description == "Test project"
        assert project.default_branch == "main"

    def test_save_persists_in_memory_changes(self, config_file, registry, tmp_git_repo):
        """Test that modifying config in-memory then calling save() persists the change."""
        # Modify config in memory — add a new project
        registry.config.projects["added-project"] = ProjectConfig(
            path=str(tmp_git_repo),
            default_branch="develop",
            description="Added via in-memory mutation",
        )

        # Save to disk
        registry.save()

        # Load a fresh registry from the same file to confirm persistence
        fresh = ProjectRegistry(config_path=config_file)
        assert "added-project" in fresh.list_projects()
        added = fresh.get_project("added-project")
        assert added.default_branch == "develop"
        assert added.description == "Added via in-memory mutation"

        # Original project should still be there
        assert "test-project" in fresh.list_projects()
