# Agent Forge

Multi-repo agent orchestration with web dashboard and Telegram relay.

## Architecture
- FastAPI backend with Jinja2 + HTMX + Alpine.js frontend
- SQLite for event log and agent snapshots
- tmux for process management, git worktrees for isolation
- python-telegram-bot for IM gateway

## Key commands
- Run: `uvicorn agent_forge.main:app --reload`
- Test: `pytest tests/ -v`

## Project structure
- `agent_forge/` — Python package (config, registry, agent manager, tmux utils, database, status monitor, websocket manager, telegram gateway, media handler, FastAPI app)
- `templates/` — Jinja2 templates (dashboard, agent detail, HTMX partials)
- `static/` — CSS and JS (WebSocket client)
- `tests/` — pytest tests (all async, mocking subprocess calls)

## Conventions
- All async where possible
- Pydantic models for all config and API schemas
- Type hints everywhere
- Tests mock subprocess calls — never require real tmux/git
- tmux session names use `forge__{project}__{id}` format (double underscore delimiter)
- Agent IDs are 6-char hex strings from uuid4
