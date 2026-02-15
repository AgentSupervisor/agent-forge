"""Populate mock agents for screenshots and demos.

Usage:
    uvicorn agent_forge.main:app --reload  # then set app.state.demo_mode = True
    # or use the CLI:
    python -m agent_forge.main --demo
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .agent_manager import Agent, AgentManager, AgentStatus
from .config import AgentProfile, ForgeConfig, ProjectConfig


DEMO_PROJECTS: dict[str, dict] = {
    "agent-forge": {
        "path": "/tmp/demo/agent-forge",
        "description": "Multi-agent orchestration platform",
        "default_branch": "main",
    },
    "acme-backend": {
        "path": "/tmp/demo/acme-backend",
        "description": "E-commerce API and microservices",
        "default_branch": "main",
    },
    "mobile-app": {
        "path": "/tmp/demo/mobile-app",
        "description": "React Native cross-platform app",
        "default_branch": "develop",
    },
    "data-pipeline": {
        "path": "/tmp/demo/data-pipeline",
        "description": "ETL jobs and analytics warehouse",
        "default_branch": "main",
    },
}

DEMO_PROFILES: dict[str, dict] = {
    "architect": {"description": "Senior architecture and system design"},
    "senior": {"description": "Experienced full-stack development"},
    "tester": {"description": "Testing and quality assurance"},
}

MOCK_AGENTS: list[dict] = [
    # --- project: agent-forge (4 agents) ---
    {
        "id": "a1b2c3",
        "project_name": "agent-forge",
        "status": AgentStatus.WORKING,
        "task_description": "Refactor WebSocket manager to support per-agent subscriptions and binary frames",
        "profile": "architect",
        "sub_agent_count": 3,
        "minutes_ago": 42,
    },
    {
        "id": "d4e5f6",
        "project_name": "agent-forge",
        "status": AgentStatus.WAITING_INPUT,
        "task_description": "Add dark/light theme toggle with system preference detection",
        "profile": "",
        "sub_agent_count": 0,
        "minutes_ago": 18,
    },
    {
        "id": "7a8b9c",
        "project_name": "agent-forge",
        "status": AgentStatus.IDLE,
        "task_description": "Write integration tests for the connector manager lifecycle",
        "profile": "tester",
        "sub_agent_count": 0,
        "minutes_ago": 95,
    },
    {
        "id": "fe0d1c",
        "project_name": "agent-forge",
        "status": AgentStatus.WORKING,
        "task_description": "Implement agent log export as downloadable markdown files",
        "profile": "",
        "sub_agent_count": 1,
        "minutes_ago": 7,
    },
    # --- project: acme-backend (3 agents) ---
    {
        "id": "b3c4d5",
        "project_name": "acme-backend",
        "status": AgentStatus.WORKING,
        "task_description": "Migrate user auth from JWT to session tokens with Redis store",
        "profile": "senior",
        "sub_agent_count": 2,
        "minutes_ago": 130,
    },
    {
        "id": "e6f7a8",
        "project_name": "acme-backend",
        "status": AgentStatus.ERROR,
        "task_description": "Fix N+1 query in orders endpoint causing 30s response times",
        "profile": "",
        "sub_agent_count": 0,
        "minutes_ago": 55,
    },
    {
        "id": "9b0c1d",
        "project_name": "acme-backend",
        "status": AgentStatus.STARTING,
        "task_description": "Add rate limiting middleware with configurable per-route limits",
        "profile": "architect",
        "sub_agent_count": 0,
        "minutes_ago": 1,
    },
    # --- project: mobile-app (3 agents) ---
    {
        "id": "2e3f4a",
        "project_name": "mobile-app",
        "status": AgentStatus.WORKING,
        "task_description": "Implement offline-first sync engine with conflict resolution",
        "profile": "senior",
        "sub_agent_count": 4,
        "minutes_ago": 210,
    },
    {
        "id": "5b6c7d",
        "project_name": "mobile-app",
        "status": AgentStatus.WAITING_INPUT,
        "task_description": "Replace Moment.js with date-fns to reduce bundle size by 40%",
        "profile": "",
        "sub_agent_count": 0,
        "minutes_ago": 33,
    },
    {
        "id": "8e9f0a",
        "project_name": "mobile-app",
        "status": AgentStatus.IDLE,
        "task_description": "Set up E2E test suite with Detox for the onboarding flow",
        "profile": "tester",
        "sub_agent_count": 0,
        "minutes_ago": 75,
    },
    # --- project: data-pipeline (2 agents) ---
    {
        "id": "c1d2e3",
        "project_name": "data-pipeline",
        "status": AgentStatus.WORKING,
        "task_description": "Build incremental ETL job for Stripe webhook events into BigQuery",
        "profile": "",
        "sub_agent_count": 2,
        "minutes_ago": 160,
    },
    {
        "id": "f4a5b6",
        "project_name": "data-pipeline",
        "status": AgentStatus.STOPPED,
        "task_description": "Add data quality checks and alerting for anomalous row counts",
        "profile": "architect",
        "sub_agent_count": 0,
        "minutes_ago": 320,
    },
]


def inject_demo_config(config: ForgeConfig) -> None:
    """Add demo projects and profiles to the config (without path validation)."""
    for name, proj in DEMO_PROJECTS.items():
        if name not in config.projects:
            # Build ProjectConfig manually to skip path validation
            pc = ProjectConfig.model_construct(
                path=proj["path"],
                default_branch=proj["default_branch"],
                description=proj["description"],
            )
            config.projects[name] = pc

    for name, prof in DEMO_PROFILES.items():
        if name not in config.profiles:
            config.profiles[name] = AgentProfile(**prof)


def populate_mock_agents(manager: AgentManager) -> None:
    """Inject mock agents into the agent manager for demo/screenshot purposes."""
    now = datetime.now()

    for m in MOCK_AGENTS:
        created = now - timedelta(minutes=m["minutes_ago"])
        agent = Agent(
            id=m["id"],
            project_name=m["project_name"],
            session_name=f"forge__{m['project_name']}__{m['id']}",
            worktree_path=f"/tmp/demo-worktrees/{m['id']}",
            branch_name=f"agent/{m['id']}/{m['task_description'][:20].replace(' ', '-').lower()}",
            status=m["status"],
            created_at=created,
            last_activity=now - timedelta(minutes=max(0, m["minutes_ago"] - 2)),
            last_output="",
            task_description=m["task_description"],
            sub_agent_count=m["sub_agent_count"],
            profile=m["profile"],
        )
        manager.agents[agent.id] = agent
