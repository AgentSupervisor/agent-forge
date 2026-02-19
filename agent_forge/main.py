"""FastAPI application — routes, WebSocket, lifespan, and CLI entry point."""

from __future__ import annotations

import argparse
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import subprocess

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from .agent_manager import AgentManager
from .config import (
    ChannelBinding,
    ConnectorConfig,
    DefaultsConfig,
    ForgeConfig,
    ProjectConfig,
    TelegramConfig,
)
from .database import delete_snapshot, get_events, init_db, log_event
from .log_manager import LogManager
from .registry import ProjectRegistry
from .websocket_manager import WebSocketManager

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT_DIR / "templates"
STATIC_DIR = ROOT_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    config_path = getattr(app.state, "config_path", "config.yaml")
    demo_mode = getattr(app.state, "demo_mode", False)

    # In demo mode, create a minimal config if none exists
    if demo_mode and not Path(config_path).exists():
        Path(config_path).write_text("server:\n  port: 8080\n")

    registry = ProjectRegistry(config_path)
    config = registry.config

    db = await init_db(str(ROOT_DIR / "agent_forge.db"))

    # Install log manager on root logger to capture all app logs
    log_manager = LogManager(buffer_size=2000)
    log_manager.setLevel(logging.DEBUG)
    root_logger = logging.getLogger()
    root_logger.addHandler(log_manager)
    # Ensure root logger level lets DEBUG through
    if root_logger.level > logging.DEBUG:
        root_logger.setLevel(logging.DEBUG)

    ws_manager = WebSocketManager()
    agent_manager = AgentManager(registry, config.defaults)
    agent_manager._db = db

    # Try to import and start StatusMonitor (Phase 2) — skip in demo mode
    status_monitor = None
    if not demo_mode:
        try:
            from .status_monitor import StatusMonitor

            status_monitor = StatusMonitor(
                agent_manager, ws_manager, db, config.defaults.poll_interval_seconds,
                config=config,
            )
            await status_monitor.start()
            logger.info("StatusMonitor started")
        except ImportError:
            logger.warning("status_monitor module not available; skipping")

    # Migrate legacy telegram config to connectors if needed
    bot_token = config.get_bot_token()
    if bot_token and not config.connectors:
        config.connectors["telegram"] = ConnectorConfig(
            type="telegram",
            enabled=True,
            credentials={"bot_token": bot_token},
            settings={"allowed_users": config.telegram.allowed_users},
        )
        logger.info("Migrated legacy telegram config to connectors")

    # Start ConnectorManager
    connector_manager = None
    if config.connectors:
        try:
            from .connectors.manager import ConnectorManager
            from .media_handler import MediaHandler

            media_handler = MediaHandler()
            connector_manager = ConnectorManager(agent_manager, media_handler, config, registry=registry)
            await connector_manager.start()
            logger.info("ConnectorManager started with %d connector(s)", len(connector_manager.connectors))
        except ImportError:
            logger.warning("connectors module not available; skipping")
        except Exception:
            logger.exception("Failed to start ConnectorManager")

    # Recover existing tmux sessions (skip in demo mode)
    if not demo_mode:
        await agent_manager.recover_sessions()

    # Wire ConnectorManager into StatusMonitor for outbound notifications
    if status_monitor and connector_manager:
        status_monitor.connector_manager = connector_manager
        # Wire metrics_collector into ConnectorManager for /metrics command
        if status_monitor.metrics_collector:
            connector_manager.metrics_collector = status_monitor.metrics_collector

    # Inject demo data if requested
    if demo_mode:
        from .demo import inject_demo_config, populate_mock_agents
        inject_demo_config(config)
        populate_mock_agents(agent_manager)
        logger.info("Demo mode: injected %d mock agents", len(agent_manager.agents))

    # Store on app.state for access in routes
    app.state.config = config
    app.state.registry = registry
    app.state.db = db
    app.state.agent_manager = agent_manager
    app.state.ws_manager = ws_manager
    app.state.status_monitor = status_monitor
    app.state.connector_manager = connector_manager
    app.state.log_manager = log_manager
    app.state.started_at = time.time()

    yield

    # Shutdown — stop monitors and connectors, close db. Do NOT kill agents.
    if status_monitor:
        await status_monitor.stop()
    if connector_manager:
        await connector_manager.stop()
    await db.close()
    logger.info("Agent Forge shut down (agents left running)")


app = FastAPI(title="Agent Forge", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["cache_bust"] = str(int(time.time()))


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health(request: Request):
    mgr: AgentManager = request.app.state.agent_manager
    started = getattr(request.app.state, "started_at", 0)
    uptime_secs = int(time.time() - started) if started else 0
    hours, remainder = divmod(uptime_secs, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        uptime_str = f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        uptime_str = f"{minutes}m {secs}s"
    else:
        uptime_str = f"{secs}s"
    return {
        "status": "ok",
        "agents": len(mgr.list_agents()),
        "uptime": uptime_str,
        "uptime_seconds": uptime_secs,
    }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _agent_to_dict(agent) -> dict:
    """Serialise an Agent dataclass to a JSON-safe dict."""
    return {
        "id": agent.id,
        "project_name": agent.project_name,
        "session_name": agent.session_name,
        "worktree_path": agent.worktree_path,
        "branch_name": agent.branch_name,
        "status": agent.status.value,
        "created_at": agent.created_at.isoformat(),
        "last_activity": agent.last_activity.isoformat(),
        "last_output": agent.last_output[-2000:] if agent.last_output else "",
        "task_description": agent.task_description,
        "sub_agent_count": agent.sub_agent_count,
        "profile": agent.profile,
        "needs_attention": agent.needs_attention,
        "parked": agent.parked,
    }


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    mgr: AgentManager = request.app.state.agent_manager
    config: ForgeConfig = request.app.state.config
    projects = config.projects
    agents = mgr.list_agents()
    agents_by_project: dict[str, int] = {}
    for a in agents:
        agents_by_project[a.project_name] = agents_by_project.get(a.project_name, 0) + 1
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "projects": projects,
            "profiles": config.profiles,
            "agents": agents,
            "agents_by_project": agents_by_project,
            "total_agents": len(agents),
        },
    )


@app.get("/agent/{agent_id}", response_class=HTMLResponse)
async def agent_detail(request: Request, agent_id: str):
    mgr: AgentManager = request.app.state.agent_manager
    agent = mgr.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    from . import tmux_utils
    terminal_output = tmux_utils.capture_pane(agent.session_name, lines=5000)
    return templates.TemplateResponse(
        "agent_detail.html",
        {
            "request": request,
            "agent": agent,
            "terminal_output": terminal_output,
            "total_agents": len(mgr.list_agents()),
        },
    )


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    config: ForgeConfig = request.app.state.config
    mgr: AgentManager = request.app.state.agent_manager
    return templates.TemplateResponse(
        "config.html",
        {
            "request": request,
            "config": config,
            "total_agents": len(mgr.list_agents()),
        },
    )


@app.get("/config/projects/{name}", response_class=HTMLResponse)
async def project_detail_page(request: Request, name: str):
    config: ForgeConfig = request.app.state.config
    mgr: AgentManager = request.app.state.agent_manager
    if name not in config.projects:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")
    project = config.projects[name]
    agents = [a for a in mgr.list_agents() if a.project_name == name]
    # Convert to dict so Jinja2's tojson filter can serialize nested models
    project_dict = project.model_dump()
    return templates.TemplateResponse(
        "project_detail.html",
        {
            "request": request,
            "project_name": name,
            "project": project_dict,
            "config": config,
            "agents": agents,
            "max_agents": config.get_max_agents(name),
            "total_agents": len(mgr.list_agents()),
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    config: ForgeConfig = request.app.state.config
    mgr: AgentManager = request.app.state.agent_manager
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "config": config,
            "total_agents": len(mgr.list_agents()),
        },
    )


@app.get("/console", response_class=HTMLResponse)
async def console_page(request: Request):
    return templates.TemplateResponse("console.html", {"request": request})


@app.get("/metrics", response_class=HTMLResponse)
async def metrics_page(request: Request):
    mgr: AgentManager = request.app.state.agent_manager
    return templates.TemplateResponse(
        "metrics.html",
        {
            "request": request,
            "total_agents": len(mgr.list_agents()),
        },
    )


# ---------------------------------------------------------------------------
# JSON API — Projects
# ---------------------------------------------------------------------------

@app.get("/api/projects")
async def api_list_projects(request: Request):
    config: ForgeConfig = request.app.state.config
    mgr: AgentManager = request.app.state.agent_manager
    agents_by_project = mgr.get_agents_by_project()
    result = []
    for name, project in config.projects.items():
        result.append({
            "name": name,
            "path": project.path,
            "description": project.description,
            "default_branch": project.default_branch,
            "max_agents": config.get_max_agents(name),
            "agent_count": len(agents_by_project.get(name, [])),
        })
    return result


# ---------------------------------------------------------------------------
# JSON API — Agents
# ---------------------------------------------------------------------------

@app.get("/api/agents")
async def api_list_agents(request: Request, project: str | None = None):
    mgr: AgentManager = request.app.state.agent_manager
    agents = mgr.list_agents(project_name=project)
    return [_agent_to_dict(a) for a in agents]


@app.get("/api/agents/{agent_id}")
async def api_get_agent(request: Request, agent_id: str):
    mgr: AgentManager = request.app.state.agent_manager
    agent = mgr.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return _agent_to_dict(agent)


@app.post("/api/agents", status_code=201)
async def api_spawn_agent(request: Request):
    mgr: AgentManager = request.app.state.agent_manager
    config: ForgeConfig = request.app.state.config
    db = request.app.state.db
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    project_name = body.get("project")
    task = body.get("task", "")
    profile = body.get("profile", "")

    if not project_name:
        raise HTTPException(status_code=400, detail="'project' is required")

    # Validate profile exists if provided
    if profile and not config.get_profile(profile):
        raise HTTPException(status_code=400, detail=f"Profile not found: '{profile}'")

    try:
        agent = await mgr.spawn_agent(project_name, task, profile=profile)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Project not found: '{project_name}'")
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await log_event(db, agent.id, project_name, "spawned", {"task": task, "profile": profile})
    return _agent_to_dict(agent)


@app.post("/api/agents/compare", status_code=201)
async def api_spawn_comparison(request: Request):
    """Spawn multiple agents on the same task with different profiles for A/B testing."""
    mgr: AgentManager = request.app.state.agent_manager
    config: ForgeConfig = request.app.state.config
    db = request.app.state.db
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    project_name = body.get("project")
    task = body.get("task", "")
    profiles = body.get("profiles", [])
    count = body.get("count", 0)

    if not project_name:
        raise HTTPException(status_code=400, detail="'project' is required")
    if not profiles:
        raise HTTPException(status_code=400, detail="'profiles' list is required")

    # Validate all profiles exist
    for p in profiles:
        if not config.get_profile(p):
            raise HTTPException(status_code=400, detail=f"Profile not found: '{p}'")

    try:
        agents = await mgr.spawn_comparison(project_name, task, profiles, count)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Project not found: '{project_name}'")
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    for agent in agents:
        await log_event(db, agent.id, project_name, "spawned", {
            "task": task, "profile": agent.profile, "comparison": True,
        })

    return [_agent_to_dict(a) for a in agents]


@app.delete("/api/agents/{agent_id}")
async def api_kill_agent(request: Request, agent_id: str):
    mgr: AgentManager = request.app.state.agent_manager
    db = request.app.state.db
    agent = mgr.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    project_name = agent.project_name
    await log_event(db, agent_id, project_name, "killed", None)
    await delete_snapshot(db, agent_id)
    success = await mgr.kill_agent(agent_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to kill agent")
    return {"status": "killed", "agent_id": agent_id}


@app.post("/api/agents/{agent_id}/message")
async def api_send_message(request: Request, agent_id: str):
    mgr: AgentManager = request.app.state.agent_manager
    db = request.app.state.db
    agent = mgr.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent.needs_attention = False

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    text = body.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="'text' is required")

    success = await mgr.send_message(agent_id, text)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to send message")

    agent.last_user_message = text
    await log_event(db, agent_id, agent.project_name, "message_sent", {"text": text[:500]})
    return {"status": "sent"}


@app.post("/api/agents/{agent_id}/control")
async def api_send_control(request: Request, agent_id: str):
    """Send a control action: approve, approve_all, reject, interrupt, up, down."""
    mgr: AgentManager = request.app.state.agent_manager
    db = request.app.state.db
    agent = mgr.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent.needs_attention = False

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    action = body.get("action", "").strip()
    if action not in ("approve", "approve_all", "reject", "interrupt", "up", "down"):
        raise HTTPException(
            status_code=400,
            detail="'action' must be one of: approve, approve_all, reject, interrupt, up, down",
        )

    success = await mgr.send_control(agent_id, action)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to send control")

    await log_event(db, agent_id, agent.project_name, "control_sent", {"action": action})
    return {"status": "sent", "action": action}


@app.post("/api/agents/{agent_id}/restart")
async def api_restart_agent(request: Request, agent_id: str):
    """Kill and respawn an agent with the same project, task, and profile."""
    mgr: AgentManager = request.app.state.agent_manager
    db = request.app.state.db
    agent = mgr.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    try:
        new_agent = await mgr.restart_agent(agent_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    await log_event(db, new_agent.id, new_agent.project_name, "agent_restarted", {
        "previous_id": agent_id,
    })
    return {
        "status": "restarted",
        "old_id": agent_id,
        "new_id": new_agent.id,
    }


@app.get("/api/agents/{agent_id}/terminal")
async def api_get_terminal(request: Request, agent_id: str):
    mgr: AgentManager = request.app.state.agent_manager
    agent = mgr.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    from . import tmux_utils
    output = tmux_utils.capture_pane(agent.session_name, lines=5000)
    return {"agent_id": agent_id, "output": output}


@app.get("/api/agents/{agent_id}/events")
async def api_agent_events(request: Request, agent_id: str, limit: int = 100):
    mgr: AgentManager = request.app.state.agent_manager
    if not mgr.get_agent(agent_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    db = request.app.state.db
    events = await get_events(db, agent_id=agent_id, limit=limit)
    return events


@app.post("/api/agents/{agent_id}/acknowledge")
async def api_acknowledge_agent(request: Request, agent_id: str):
    mgr: AgentManager = request.app.state.agent_manager
    agent = mgr.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.needs_attention = False
    return {"status": "acknowledged", "agent_id": agent_id}


@app.post("/api/agents/{agent_id}/park")
async def api_park_agent(request: Request, agent_id: str):
    mgr: AgentManager = request.app.state.agent_manager
    agent = mgr.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.parked = True
    agent.needs_attention = False
    return {"status": "parked", "agent_id": agent_id}


@app.post("/api/agents/{agent_id}/unpark")
async def api_unpark_agent(request: Request, agent_id: str):
    mgr: AgentManager = request.app.state.agent_manager
    agent = mgr.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.parked = False
    return {"status": "unparked", "agent_id": agent_id}


# ---------------------------------------------------------------------------
# JSON API — Stats
# ---------------------------------------------------------------------------

@app.get("/api/stats")
async def api_stats(request: Request):
    """Return aggregate stats for the stats bar."""
    mgr: AgentManager = request.app.state.agent_manager
    agents = mgr.list_agents()
    from .agent_manager import AgentStatus
    counts = {s.value: 0 for s in AgentStatus}
    total_sub_agents = 0
    needs_attention_count = 0
    for a in agents:
        counts[a.status.value] = counts.get(a.status.value, 0) + 1
        total_sub_agents += a.sub_agent_count
        if a.needs_attention:
            needs_attention_count += 1
    return {
        "total": len(agents),
        "by_status": counts,
        "total_sub_agents": total_sub_agents,
        "needs_attention_count": needs_attention_count,
    }


@app.get("/api/metrics")
async def api_metrics(request: Request):
    """Return current system and agent metrics."""
    monitor = request.app.state.status_monitor
    if not monitor or not monitor.metrics_collector:
        raise HTTPException(status_code=503, detail="Metrics collection not available")
    snapshot = monitor.metrics_collector.collect_all(request.app.state.agent_manager)
    return snapshot.model_dump(mode="json")


# ---------------------------------------------------------------------------
# JSON API — Events (global)
# ---------------------------------------------------------------------------

@app.get("/api/events")
async def api_get_events(
    request: Request,
    agent_id: str | None = None,
    project: str | None = None,
    limit: int = 100,
):
    db = request.app.state.db
    events = await get_events(db, agent_id=agent_id, project_name=project, limit=limit)
    return events


# ---------------------------------------------------------------------------
# JSON API — Config
# ---------------------------------------------------------------------------

class ProjectCreateRequest(BaseModel):
    name: str
    path: str
    default_branch: str = ""
    description: str = ""
    max_agents: int | None = None


class ProjectUpdateRequest(BaseModel):
    path: str | None = None
    default_branch: str | None = None
    description: str | None = None
    max_agents: int | None = None
    agent_instructions: str | None = None
    context_files: list[str] | None = None


class TelegramUpdateRequest(BaseModel):
    bot_token: str = ""
    allowed_users: list[int] = []


class DefaultsUpdateRequest(BaseModel):
    max_agents_per_project: int | None = None
    claude_command: str | None = None
    claude_env: dict[str, str] | None = None
    poll_interval_seconds: float | None = None
    agent_instructions: str | None = None


# ---------------------------------------------------------------------------
# JSON API — Profiles
# ---------------------------------------------------------------------------

@app.get("/api/profiles")
async def api_list_profiles(request: Request):
    config: ForgeConfig = request.app.state.config
    return {
        name: {"description": p.description, "system_prompt": bool(p.system_prompt), "instructions": bool(p.instructions), "start_sequence_steps": len(p.start_sequence)}
        for name, p in config.profiles.items()
    }


@app.get("/api/profiles/{name}")
async def api_get_profile(request: Request, name: str):
    config: ForgeConfig = request.app.state.config
    profile = config.get_profile(name)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile not found: '{name}'")
    return {"name": name, **profile.model_dump()}


class ProfileCreateRequest(BaseModel):
    description: str = ""
    system_prompt: str = ""
    instructions: str = ""
    start_sequence: list[dict] = []


@app.post("/api/config/profiles/{name}", status_code=201)
async def api_create_profile(request: Request, name: str, body: ProfileCreateRequest):
    from .config import AgentProfile, StartSequenceStep
    registry: ProjectRegistry = request.app.state.registry
    config = registry.config

    if name in config.profiles:
        raise HTTPException(status_code=409, detail=f"Profile '{name}' already exists")

    steps = [StartSequenceStep(**s) for s in body.start_sequence]
    config.profiles[name] = AgentProfile(
        description=body.description,
        system_prompt=body.system_prompt,
        instructions=body.instructions,
        start_sequence=steps,
    )
    registry.save()
    request.app.state.config = registry.config
    return {"status": "created", "name": name}


@app.put("/api/config/profiles/{name}")
async def api_update_profile(request: Request, name: str, body: ProfileCreateRequest):
    from .config import AgentProfile, StartSequenceStep
    registry: ProjectRegistry = request.app.state.registry
    config = registry.config

    if name not in config.profiles:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")

    steps = [StartSequenceStep(**s) for s in body.start_sequence]
    config.profiles[name] = AgentProfile(
        description=body.description,
        system_prompt=body.system_prompt,
        instructions=body.instructions,
        start_sequence=steps,
    )
    registry.save()
    request.app.state.config = registry.config
    return {"status": "updated", "name": name}


@app.delete("/api/config/profiles/{name}")
async def api_delete_profile(request: Request, name: str):
    registry: ProjectRegistry = request.app.state.registry
    config = registry.config

    if name not in config.profiles:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")

    del config.profiles[name]
    registry.save()
    request.app.state.config = registry.config
    return {"status": "deleted", "name": name}


@app.post("/api/config/reload")
async def api_reload_config(request: Request):
    registry: ProjectRegistry = request.app.state.registry
    try:
        registry.reload()
        request.app.state.config = registry.config
        return {"status": "reloaded"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/config")
async def api_get_config(request: Request):
    config: ForgeConfig = request.app.state.config
    data = config.model_dump()
    # Mask the telegram bot token
    token = data.get("telegram", {}).get("bot_token", "")
    if token:
        data["telegram"]["bot_token"] = token[:4] + "***" + token[-4:] if len(token) > 8 else "***"
    # Mask connector credentials
    for conn_id, conn_data in data.get("connectors", {}).items():
        for key, val in conn_data.get("credentials", {}).items():
            if val and isinstance(val, str) and len(val) > 6:
                conn_data["credentials"][key] = val[:3] + "***" + val[-3:]
            elif val:
                conn_data["credentials"][key] = "***"
    return data


@app.post("/api/config/projects", status_code=201)
async def api_add_project(request: Request, body: ProjectCreateRequest):
    registry: ProjectRegistry = request.app.state.registry
    config = registry.config

    if body.name in config.projects:
        raise HTTPException(status_code=409, detail=f"Project '{body.name}' already exists")

    project_path = Path(body.path).expanduser().resolve()
    if not project_path.exists():
        raise HTTPException(status_code=400, detail=f"Path does not exist: {body.path}")
    if not (project_path / ".git").exists():
        raise HTTPException(status_code=400, detail=f"Not a git repository: {body.path}")

    # Auto-detect default branch if not provided
    default_branch = body.default_branch
    if not default_branch:
        try:
            result = subprocess.run(
                ["git", "-C", str(project_path), "symbolic-ref", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            default_branch = result.stdout.strip() if result.returncode == 0 else "main"
        except Exception:
            default_branch = "main"

    project = ProjectConfig(
        path=str(project_path),
        default_branch=default_branch,
        description=body.description,
        max_agents=body.max_agents,
    )
    config.projects[body.name] = project
    registry.save()
    request.app.state.config = registry.config
    return {"status": "created", "name": body.name}


@app.put("/api/config/projects/{name}")
async def api_update_project(request: Request, name: str, body: ProjectUpdateRequest):
    registry: ProjectRegistry = request.app.state.registry
    config = registry.config

    if name not in config.projects:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    project = config.projects[name]
    if body.path is not None:
        project_path = Path(body.path).expanduser().resolve()
        if not project_path.exists():
            raise HTTPException(status_code=400, detail=f"Path does not exist: {body.path}")
        if not (project_path / ".git").exists():
            raise HTTPException(status_code=400, detail=f"Not a git repository: {body.path}")
        project.path = str(project_path)
    if body.default_branch is not None:
        project.default_branch = body.default_branch
    if body.description is not None:
        project.description = body.description
    if body.max_agents is not None:
        project.max_agents = body.max_agents
    if body.agent_instructions is not None:
        project.agent_instructions = body.agent_instructions
    if body.context_files is not None:
        project.context_files = body.context_files

    registry.save()
    request.app.state.config = registry.config
    return {"status": "updated", "name": name}


@app.delete("/api/config/projects/{name}")
async def api_delete_project(request: Request, name: str):
    registry: ProjectRegistry = request.app.state.registry
    config = registry.config
    mgr: AgentManager = request.app.state.agent_manager

    if name not in config.projects:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    # Check for active agents
    active = [a for a in mgr.list_agents() if a.project_name == name]
    if active:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete project '{name}': {len(active)} active agent(s)",
        )

    del config.projects[name]
    registry.save()
    request.app.state.config = registry.config
    return {"status": "deleted", "name": name}


@app.put("/api/config/telegram")
async def api_update_telegram(request: Request, body: TelegramUpdateRequest):
    registry: ProjectRegistry = request.app.state.registry
    config = registry.config

    config.telegram.bot_token = body.bot_token
    config.telegram.allowed_users = body.allowed_users

    # Sync to connectors config — keep a "telegram" connector in sync
    if body.bot_token:
        config.connectors["telegram"] = ConnectorConfig(
            type="telegram",
            enabled=True,
            credentials={"bot_token": body.bot_token},
            settings={"allowed_users": body.allowed_users},
        )
    elif "telegram" in config.connectors:
        del config.connectors["telegram"]

    registry.save()
    request.app.state.config = registry.config

    # Restart telegram connector via ConnectorManager
    connector_manager = getattr(request.app.state, "connector_manager", None)
    if connector_manager:
        try:
            await connector_manager.restart_connector("telegram")
            logger.info("Telegram connector restarted via ConnectorManager")
        except Exception:
            logger.exception("Failed to restart telegram connector")

    return {"status": "updated"}


@app.put("/api/config/defaults")
async def api_update_defaults(request: Request, body: DefaultsUpdateRequest):
    registry: ProjectRegistry = request.app.state.registry
    config = registry.config

    if body.max_agents_per_project is not None:
        config.defaults.max_agents_per_project = body.max_agents_per_project
    if body.claude_command is not None:
        config.defaults.claude_command = body.claude_command
    if body.claude_env is not None:
        config.defaults.claude_env = body.claude_env
    if body.poll_interval_seconds is not None:
        config.defaults.poll_interval_seconds = body.poll_interval_seconds
    if body.agent_instructions is not None:
        config.defaults.agent_instructions = body.agent_instructions

    registry.save()
    request.app.state.config = registry.config

    # Update agent manager defaults
    request.app.state.agent_manager.defaults = registry.config.defaults

    return {"status": "updated"}


# ---------------------------------------------------------------------------
# JSON API — Connectors
# ---------------------------------------------------------------------------

class ConnectorCreateRequest(BaseModel):
    id: str
    type: str
    enabled: bool = True
    credentials: dict[str, str] = {}
    settings: dict = {}


class ConnectorUpdateRequest(BaseModel):
    enabled: bool | None = None
    credentials: dict[str, str] | None = None
    settings: dict | None = None


class ConnectorTestRequest(BaseModel):
    channel_id: str = ""


class ChannelValidateRequest(BaseModel):
    channel_id: str


class ChannelBindingRequest(BaseModel):
    connector_id: str
    channel_id: str
    channel_name: str = ""
    inbound: bool = True
    outbound: bool = True


@app.get("/api/config/connectors")
async def api_list_connectors(request: Request):
    config: ForgeConfig = request.app.state.config
    result = {}
    for conn_id, conn_cfg in config.connectors.items():
        data = conn_cfg.model_dump()
        # Mask credentials
        for key, val in data.get("credentials", {}).items():
            if val and isinstance(val, str) and len(val) > 6:
                data["credentials"][key] = val[:3] + "***" + val[-3:]
            elif val:
                data["credentials"][key] = "***"
        # Add runtime status
        connector_manager = getattr(request.app.state, "connector_manager", None)
        data["running"] = (
            connector_manager is not None and conn_id in connector_manager.connectors
        )
        result[conn_id] = data
    return result


async def _ensure_connector_manager(app_state) -> "ConnectorManager | None":
    """Lazily create a ConnectorManager if one doesn't exist yet."""
    manager = getattr(app_state, "connector_manager", None)
    if manager is not None:
        return manager
    try:
        from .connectors.manager import ConnectorManager
        from .media_handler import MediaHandler

        media_handler = MediaHandler()
        manager = ConnectorManager(app_state.agent_manager, media_handler, app_state.config, registry=app_state.registry)
        await manager.start()
        app_state.connector_manager = manager
        status_monitor = getattr(app_state, "status_monitor", None)
        if status_monitor:
            status_monitor.connector_manager = manager
        logger.info("ConnectorManager created lazily with %d connector(s)", len(manager.connectors))
        return manager
    except ImportError:
        logger.warning("connectors module not available; cannot create ConnectorManager")
        return None
    except Exception:
        logger.exception("Failed to create ConnectorManager lazily")
        return None


@app.post("/api/config/connectors", status_code=201)
async def api_add_connector(request: Request, body: ConnectorCreateRequest):
    registry: ProjectRegistry = request.app.state.registry
    config = registry.config

    if body.id in config.connectors:
        raise HTTPException(status_code=409, detail=f"Connector '{body.id}' already exists")

    valid_types = {"telegram", "discord", "slack", "whatsapp", "signal"}
    if body.type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid type '{body.type}'. Must be one of: {', '.join(sorted(valid_types))}",
        )

    config.connectors[body.id] = ConnectorConfig(
        type=body.type,
        enabled=body.enabled,
        credentials=body.credentials,
        settings=body.settings,
    )
    registry.save()
    request.app.state.config = registry.config

    # Start the connector if enabled
    if body.enabled:
        connector_manager = await _ensure_connector_manager(request.app.state)
        if connector_manager:
            try:
                await connector_manager.restart_connector(body.id)
            except Exception:
                logger.exception("Failed to start new connector '%s'", body.id)

    return {"status": "created", "id": body.id}


@app.put("/api/config/connectors/{connector_id}")
async def api_update_connector(request: Request, connector_id: str, body: ConnectorUpdateRequest):
    registry: ProjectRegistry = request.app.state.registry
    config = registry.config

    if connector_id not in config.connectors:
        raise HTTPException(status_code=404, detail=f"Connector '{connector_id}' not found")

    conn = config.connectors[connector_id]
    if body.enabled is not None:
        conn.enabled = body.enabled
    if body.credentials is not None:
        # Merge: only overwrite keys that were provided (allows partial updates)
        conn.credentials.update(body.credentials)
    if body.settings is not None:
        conn.settings = body.settings

    registry.save()
    request.app.state.config = registry.config

    # Restart the connector
    connector_manager = await _ensure_connector_manager(request.app.state)
    if connector_manager:
        try:
            await connector_manager.restart_connector(connector_id)
        except Exception:
            logger.exception("Failed to restart connector '%s'", connector_id)

    return {"status": "updated", "id": connector_id}


@app.delete("/api/config/connectors/{connector_id}")
async def api_delete_connector(request: Request, connector_id: str):
    registry: ProjectRegistry = request.app.state.registry
    config = registry.config

    if connector_id not in config.connectors:
        raise HTTPException(status_code=404, detail=f"Connector '{connector_id}' not found")

    # Stop the connector if running
    connector_manager = getattr(request.app.state, "connector_manager", None)
    if connector_manager:
        old = connector_manager.connectors.pop(connector_id, None)
        if old:
            try:
                await old.stop()
            except Exception:
                pass

    del config.connectors[connector_id]

    # Remove channel bindings that reference this connector
    for project_cfg in config.projects.values():
        project_cfg.channels = [
            b for b in project_cfg.channels if b.connector_id != connector_id
        ]

    registry.save()
    request.app.state.config = registry.config

    return {"status": "deleted", "id": connector_id}


@app.post("/api/config/connectors/{connector_id}/test")
async def api_test_connector(request: Request, connector_id: str, body: ConnectorTestRequest | None = None):
    connector_manager = await _ensure_connector_manager(request.app.state)
    if not connector_manager:
        raise HTTPException(status_code=503, detail="ConnectorManager not running")

    connector = connector_manager.get_connector(connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail=f"Connector '{connector_id}' not running")

    try:
        result = await connector.health_check()
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}

    # If a channel_id was provided, send a test message
    channel_id = body.channel_id if body else ""
    if channel_id:
        try:
            send_result = await connector.send_test_message(channel_id)
            result["test_message"] = send_result
        except Exception as exc:
            result["test_message"] = {"sent": False, "detail": str(exc)}

    return {"status": "ok", "health": result}


@app.get("/api/config/connectors/{connector_id}/channels")
async def api_list_connector_channels(request: Request, connector_id: str):
    connector_manager = await _ensure_connector_manager(request.app.state)
    if not connector_manager:
        raise HTTPException(status_code=503, detail="ConnectorManager not running")

    connector = connector_manager.get_connector(connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail=f"Connector '{connector_id}' not running")

    try:
        # Start with in-memory recent chats
        channels = await connector.list_channels()
        logger.info(
            "list_channels for '%s': %d in-memory chats (recent_chats=%s)",
            connector_id, len(channels),
            getattr(connector, "_recent_chats", "N/A"),
        )
        seen_ids = {ch["id"] for ch in channels}

        # Also resolve channel IDs from existing project bindings
        config: ForgeConfig = request.app.state.config
        for project_cfg in config.projects.values():
            for binding in getattr(project_cfg, "channels", []):
                if binding.connector_id != connector_id:
                    continue
                if binding.channel_id in seen_ids:
                    continue
                seen_ids.add(binding.channel_id)
                info = await connector.get_channel_info(binding.channel_id)
                if info:
                    channels.append({
                        "id": info.get("id", binding.channel_id),
                        "name": info.get("name", binding.channel_name or binding.channel_id),
                        "type": info.get("type", ""),
                    })
                elif binding.channel_name:
                    channels.append({
                        "id": binding.channel_id,
                        "name": binding.channel_name,
                        "type": "",
                    })

        # Persist known chats so they survive restarts
        if hasattr(connector, "get_known_chats"):
            known = connector.get_known_chats()
            if known:
                connector_cfg = config.connectors.get(connector_id)
                if connector_cfg and connector_cfg.settings.get("known_chats") != known:
                    connector_cfg.settings["known_chats"] = known
                    registry: ProjectRegistry = request.app.state.registry
                    registry.save()

        return {"channels": channels}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/api/config/connectors/{connector_id}/validate-channel")
async def api_validate_channel(request: Request, connector_id: str, body: ChannelValidateRequest):
    connector_manager = await _ensure_connector_manager(request.app.state)
    if not connector_manager:
        raise HTTPException(status_code=503, detail="ConnectorManager not running")

    connector = connector_manager.get_connector(connector_id)
    if not connector:
        raise HTTPException(status_code=404, detail=f"Connector '{connector_id}' not running")

    try:
        info = await connector.get_channel_info(body.channel_id)
        if info:
            return {"valid": True, "channel": info}
        return {"valid": False, "channel": {}}
    except Exception as exc:
        return {"valid": False, "detail": str(exc)}


# ---------------------------------------------------------------------------
# JSON API — Channel Bindings
# ---------------------------------------------------------------------------

@app.post("/api/config/projects/{name}/channels", status_code=201)
async def api_add_channel_binding(request: Request, name: str, body: ChannelBindingRequest):
    registry: ProjectRegistry = request.app.state.registry
    config = registry.config

    if name not in config.projects:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")
    if body.connector_id not in config.connectors:
        raise HTTPException(status_code=400, detail=f"Connector '{body.connector_id}' not found")

    binding = ChannelBinding(
        connector_id=body.connector_id,
        channel_id=body.channel_id,
        channel_name=body.channel_name,
        inbound=body.inbound,
        outbound=body.outbound,
    )
    config.projects[name].channels.append(binding)
    registry.save()
    request.app.state.config = registry.config

    # Rebuild channel map
    connector_manager = getattr(request.app.state, "connector_manager", None)
    if connector_manager:
        connector_manager._rebuild_channel_map()

    return {"status": "created", "project": name, "channel_count": len(config.projects[name].channels)}


@app.delete("/api/config/projects/{name}/channels/{idx}")
async def api_remove_channel_binding(request: Request, name: str, idx: int):
    registry: ProjectRegistry = request.app.state.registry
    config = registry.config

    if name not in config.projects:
        raise HTTPException(status_code=404, detail=f"Project '{name}' not found")

    channels = config.projects[name].channels
    if idx < 0 or idx >= len(channels):
        raise HTTPException(status_code=404, detail=f"Channel binding index {idx} out of range")

    channels.pop(idx)
    registry.save()
    request.app.state.config = registry.config

    # Rebuild channel map
    connector_manager = getattr(request.app.state, "connector_manager", None)
    if connector_manager:
        connector_manager._rebuild_channel_map()

    return {"status": "deleted", "project": name, "channel_count": len(channels)}


# ---------------------------------------------------------------------------
# Hooks — Claude Code hook event receiver
# ---------------------------------------------------------------------------

@app.post("/api/hooks/event")
async def api_hook_event(request: Request):
    """Receive hook events from Claude Code agents (SubagentStart/Stop)."""
    mgr: AgentManager = request.app.state.agent_manager
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    agent_id = body.get("agent_id", "")
    event = body.get("hook_event", "")

    agent = mgr.get_agent(agent_id)
    if not agent:
        return {"status": "ignored", "reason": "unknown agent"}

    if event == "SubagentStart":
        agent.sub_agent_count += 1
        logger.info("Sub-agent started for agent %s (now %d)", agent_id, agent.sub_agent_count)
    elif event == "SubagentStop":
        agent.sub_agent_count = max(0, agent.sub_agent_count - 1)
        logger.info("Sub-agent stopped for agent %s (now %d)", agent_id, agent.sub_agent_count)

    return {"status": "ok", "sub_agent_count": agent.sub_agent_count}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    ws_manager: WebSocketManager = websocket.app.state.ws_manager
    await ws_manager.connect(websocket)
    try:
        while True:
            # Keep connection open; clients only send pings
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    log_mgr: LogManager = websocket.app.state.log_manager
    await log_mgr.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        log_mgr.disconnect(websocket)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def cli():
    parser = argparse.ArgumentParser(description="Agent Forge")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--host", default=None, help="Override server host")
    parser.add_argument("--port", type=int, default=None, help="Override server port")
    parser.add_argument("--demo", action="store_true", help="Populate mock agents for screenshots")
    args = parser.parse_args()

    # Pre-load config to extract host/port defaults
    with open(args.config) as f:
        raw = yaml.safe_load(f) or {}
    server_cfg = raw.get("server", {})
    host = args.host or server_cfg.get("host", "0.0.0.0")
    port = args.port or server_cfg.get("port", 8080)

    app.state.config_path = args.config
    app.state.demo_mode = args.demo

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("agent-forge.log"),
        ],
    )

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    cli()
