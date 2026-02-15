# Agent Forge

**Multi-repo AI agent orchestration with a real-time web dashboard.**

Agent Forge lets you spawn, monitor, and control multiple Claude Code agents across different projects — all from a single interface. Each agent runs in its own isolated git worktree and tmux session, so they can work on separate branches in parallel without stepping on each other.

## What it does

- **Spawn agents on any project** — point them at a repo, give them a task, and watch them work in a live terminal view
- **Multi-project orchestration** — manage agents across multiple repositories simultaneously, each with its own configuration and branch isolation
- **Real-time control** — approve, reject, interrupt, or send messages to agents directly from the dashboard. Keyboard shortcuts included
- **Comparison mode** — spawn multiple agents on the same task with different profiles and compare their approaches side by side
- **Connector system** — relay agent activity to Telegram (or other messaging platforms) so you can monitor and interact with agents from your phone
- **Event logging** — full audit trail of every action, message, and status change stored in SQLite

## Multi-agent orchestration (experimental)

The core idea: instead of running one agent at a time and babysitting it, spin up a fleet of agents across your repos and let them work in parallel. The dashboard gives you a bird's-eye view of all running agents with real-time status updates via WebSocket. Jump into any agent's terminal to see exactly what it's doing, send it corrections, or restart it if it goes off track.

Each agent gets full git isolation (worktree + dedicated branch), so multiple agents can work on the same repo simultaneously without conflicts. When an agent finishes, review its branch and merge.

## Stack

- FastAPI + Jinja2 + Alpine.js + HTMX (no build step, no Node)
- tmux for process management, git worktrees for branch isolation
- SQLite for event persistence
- WebSocket for real-time dashboard updates

## Getting started

```bash
# Configure your projects in config.yaml, then:
uvicorn agent_forge.main:app --reload
```

Open `localhost:8080`, hit Spawn Agent, and go.
