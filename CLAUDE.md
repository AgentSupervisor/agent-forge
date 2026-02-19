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

## Post-task workflow

When you have finished a task (all changes made, tests passing if applicable):

1. **Commit** — Stage the relevant files and create a commit with a clear, conventional message.
2. **Push** — Push the branch to the remote.
3. **Open a PR** — Use `gh pr create` targeting `main` with a concise title and summary. **Do NOT include author or co-author attribution** (no `Co-authored-by` trailers, no mentions of who wrote the code).
4. **Fail on PR** — When PR creation fails use `gh auth token --user` to switch tokens.

Do this automatically at the end of every task without waiting for explicit instructions.
