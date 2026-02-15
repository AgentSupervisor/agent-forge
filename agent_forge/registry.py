"""ProjectRegistry — loads config.yaml, validates project paths, provides lookup."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from .config import ConnectorConfig, ForgeConfig, ProjectConfig

logger = logging.getLogger(__name__)


class ProjectRegistry:
    """Loads config.yaml, validates all project paths, provides lookup."""

    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        self.config: ForgeConfig = ForgeConfig()
        self._load(config_path)

    def _load(self, config_path: str) -> None:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(path) as f:
            raw = yaml.safe_load(f)

        if raw is None:
            raw = {}

        self.config = ForgeConfig(**raw)
        self._migrate_legacy_telegram()
        self._validate_projects()

    def _migrate_legacy_telegram(self) -> None:
        """Auto-create a connector entry from legacy telegram config."""
        import os

        token = (
            os.environ.get("AGENT_FORGE_TELEGRAM_TOKEN")
            or self.config.telegram.bot_token
        )
        if token and not self.config.connectors:
            self.config.connectors["telegram"] = ConnectorConfig(
                type="telegram",
                enabled=True,
                credentials={"bot_token": token},
                settings={"allowed_users": self.config.telegram.allowed_users},
            )
            logger.info("Migrated legacy telegram config to connectors")

    def _validate_projects(self) -> None:
        """Validate that all project paths exist and are git repos."""
        errors: list[str] = []
        for name, project in self.config.projects.items():
            project_path = Path(project.path)
            if not project_path.exists():
                errors.append(f"Project '{name}': path does not exist: {project.path}")
                continue
            if not (project_path / ".git").exists():
                errors.append(
                    f"Project '{name}': not a git repo (no .git): {project.path}"
                )
        if errors:
            for err in errors:
                logger.warning(err)

    def get_project(self, name: str) -> ProjectConfig:
        """Get a project by its short name (e.g., 'api')."""
        if name not in self.config.projects:
            raise KeyError(f"Project not found: '{name}'")
        return self.config.projects[name]

    def list_projects(self) -> dict[str, ProjectConfig]:
        """Return all registered projects."""
        return dict(self.config.projects)

    def save(self) -> None:
        """Write current config back to YAML and reload from disk."""
        path = Path(self.config_path)
        data = self.config.model_dump()
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        logger.info("Saved config to %s", self.config_path)
        self.reload()

    def reload(self) -> None:
        """Re-read config.yaml (for hot-reload)."""
        logger.info("Reloading config from %s", self.config_path)
        self._load(self.config_path)
