"""Pydantic models for config.yaml parsing and validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, field_validator


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    secret_key: str = "change-me-in-production"


class TelegramConfig(BaseModel):
    bot_token: str = ""
    allowed_users: list[int] = []


class StartSequenceStep(BaseModel):
    """A single step in an agent's boot sequence."""
    action: str  # "wait", "send", "wait_for_idle"
    value: str = ""

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        allowed = {"wait", "send", "wait_for_idle"}
        if v not in allowed:
            raise ValueError(f"action must be one of {allowed}, got '{v}'")
        return v


class AgentProfile(BaseModel):
    """Named preset with system prompt, instructions, and start sequence."""
    description: str = ""
    system_prompt: str = ""
    instructions: str = ""
    start_sequence: list[StartSequenceStep] = []


class SummaryConfig(BaseModel):
    enabled: bool = False
    api_key: str = ""
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 300
    timeout_seconds: float = 10.0


class ResponseRelayConfig(BaseModel):
    enabled: bool = True
    model: str = "claude-haiku-4-5-20251001"
    max_tokens: int = 4000
    timeout_seconds: float = 15.0
    fallback_to_summary: bool = True


class MetricsConfig(BaseModel):
    enabled: bool = True
    collect_interval_seconds: float = 5.0
    enable_gpu: bool = True
    enable_per_agent: bool = True


class DefaultsConfig(BaseModel):
    max_agents_per_project: int = 5
    sandbox: bool = True
    claude_command: str = "claude"
    claude_env: dict[str, str] = {}
    poll_interval_seconds: float = 3.0
    agent_instructions: str = ""
    summary: SummaryConfig = SummaryConfig()
    response_relay: ResponseRelayConfig = ResponseRelayConfig()
    metrics: MetricsConfig = MetricsConfig()


class SandboxConfig(BaseModel):
    allowed_hosts: list[str] = []


class ConnectorConfig(BaseModel):
    type: str  # "telegram", "discord", "slack", "whatsapp", "signal"
    enabled: bool = True
    credentials: dict[str, str] = {}
    settings: dict[str, Any] = {}


class ChannelBinding(BaseModel):
    connector_id: str
    channel_id: str
    channel_name: str = ""
    inbound: bool = True
    outbound: bool = True


class ProjectConfig(BaseModel):
    path: str
    default_branch: str = "main"
    max_agents: int | None = None
    description: str = ""
    sandbox: SandboxConfig | None = None
    channels: list[ChannelBinding] = []
    agent_instructions: str = ""
    context_files: list[str] = []
    execution: str = "local"
    execution_reason: str = ""

    @field_validator("path")
    @classmethod
    def expand_path(cls, v: str) -> str:
        return str(Path(os.path.expanduser(v)).resolve())

    @field_validator("execution")
    @classmethod
    def validate_execution(cls, v: str) -> str:
        allowed = {"local", "remote"}
        if v not in allowed:
            raise ValueError(f"execution must be one of {allowed}, got '{v}'")
        return v


class RemoteConfig(BaseModel):
    docker_context: str = "vm"
    vm_ip: str = ""
    image: str = ""
    ttyd_port_range_start: int = 30000
    ttyd_port_range_end: int = 32767
    cleanup_after_hours: int = 24
    cpu_limit: str = "1"
    memory_limit: str = "2G"
    config_repo: str = ""
    ttyd_user: str = "agent"
    ttyd_pass_env: str = "AGENT_TTYD_PASS"


class ForgeConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    telegram: TelegramConfig = TelegramConfig()
    connectors: dict[str, ConnectorConfig] = {}
    defaults: DefaultsConfig = DefaultsConfig()
    profiles: dict[str, AgentProfile] = {}
    projects: dict[str, ProjectConfig] = {}
    remote: RemoteConfig | None = None

    def get_profile(self, name: str) -> AgentProfile | None:
        """Get a profile by name, or None if not found."""
        return self.profiles.get(name)

    def get_max_agents(self, project_name: str) -> int:
        """Get max agents for a project, falling back to defaults."""
        project = self.projects.get(project_name)
        if project and project.max_agents is not None:
            return project.max_agents
        return self.defaults.max_agents_per_project

    def get_bot_token(self) -> str:
        """Get bot token from config or environment variable."""
        return (
            os.environ.get("AGENT_FORGE_TELEGRAM_TOKEN")
            or self.telegram.bot_token
        )

    def get_summary_api_key(self) -> str:
        """Get Anthropic API key for activity summarization.

        Resolution order: AGENT_FORGE_ANTHROPIC_API_KEY > ANTHROPIC_API_KEY > config value.
        """
        return (
            os.environ.get("AGENT_FORGE_ANTHROPIC_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or self.defaults.summary.api_key
        )
