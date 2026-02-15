<p align="center">
  <img src="logo-transparent.png" alt="Agent Forge" width="160">
</p>

<h1 align="center">Agent Forge</h1>

<p align="center">
  A multi-repository orchestration platform for Claude Code agents.<br>
  Spawn, monitor, and control a fleet of AI coding agents from a single dashboard.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11+-blue?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey?style=flat-square" alt="Platform">
</p>

<p align="center">
  <img src="screenshot-1.png" alt="Dashboard" width="720">
</p>

---

## What is Agent Forge?

Agent Forge lets you run multiple [Claude Code](https://docs.anthropic.com/en/docs/claude-code) agents in parallel across different git repositories — each in its own isolated branch — and manage them all through a real-time web dashboard or IM connectors (Telegram, Discord, Slack, WhatsApp, Signal).

Each agent runs in its own tmux session with a dedicated git worktree, so agents never interfere with each other or your working branches. The dashboard gives you live terminal output, status tracking, interactive controls, and sub-agent visibility for every running agent.

<p align="center">
  <img src="screenshot-2.png" alt="Agent Detail" width="720">
</p>

### Key Features

- **Multi-repo, multi-agent** — Run agents across any number of git repositories simultaneously
- **Real-time dashboard** — Live terminal output, status indicators with pulsing animations, agent card grid
- **Agent profiles** — Named presets with system prompts, instructions, and start sequences for repeatable workflows
- **A/B comparison** — Spawn multiple agents on the same task with different profiles to compare approaches
- **Sub-agent tracking** — See how many Claude Code sub-agents each agent has spawned (via hooks)
- **Interactive controls** — Approve, reject, interrupt, restart, and send messages to agents from the browser
- **Git worktree isolation** — Each agent works on its own branch in a lightweight worktree
- **Agent instructions** — Global and per-project instructions injected into every agent's prompt
- **Context files** — Per-project file lists automatically provided to agents as context
- **Multi-IM connectors** — Telegram, Discord, Slack, WhatsApp, and Signal with per-project channel bindings
- **Media handling** — Send images, video frames (ffmpeg), and voice (whisper) to agents
- **Server console** — Live server log streaming in the browser
- **Session recovery** — Agents survive server restarts
- **Daemon mode** — Run as a background service with auto-start support
- **Config hot-reload** — Add projects without restarting
- **Full config API** — CRUD for projects, connectors, profiles, and settings via REST

---

## Installation

### Prerequisites

| Dependency | Required | Notes |
|---|---|---|
| Python 3.11+ | Yes | |
| tmux | Yes | `brew install tmux` / `apt install tmux` |
| git | Yes | |
| Claude Code CLI | Yes | `npm install -g @anthropic-ai/claude-code` |
| ffmpeg | No | For video/media handling |

### Quick Install

```bash
git clone https://github.com/yourname/agent-forge.git
cd agent-forge
./install.sh
```

The install script will check dependencies, create a virtual environment, and install the package.

### Manual Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Optional: IM connector support
pip install -e ".[telegram]"   # Telegram (python-telegram-bot)
pip install -e ".[discord]"    # Discord (discord.py)
pip install -e ".[slack]"      # Slack (slack-bolt)
```

---

## Getting Started

### 1. Create a config

```bash
forge init
```

This walks you through an interactive setup:
- Server host/port
- Claude Code command and model
- Agent team support
- Git repositories to manage

### 2. Start the server

```bash
# Foreground
forge start

# Background (daemon)
forge start -d
```

### 3. Open the dashboard

Navigate to **http://localhost:8080** and spawn your first agent.

---

## Usage

### CLI Commands

```
forge init              Create config.yaml interactively
forge start             Start the server (add -d for daemon mode)
forge stop              Stop the daemon
forge restart           Restart the daemon
forge status            Check if the server is running
forge service           Generate a systemd/launchd auto-start service
```

### Dashboard

The dashboard shows all running agents as cards in a grid with live-updating status:

- **Stats bar** — Total agents and breakdown by status (working, waiting, idle, error)
- **Agent cards** — Status, project, task description, uptime, branch, sub-agent count, profile
- **Spawn modal** — Select a project, choose a profile, and describe the task (`Cmd+K` shortcut)
- **Sidebar** — Navigate between Dashboard, Configuration, Settings, and Console

### Agent Detail

Click an agent card to open the detail view:

- **Full-screen terminal** — Live output from the agent's tmux session
- **Message input** — Send instructions to the agent (like a terminal prompt)
- **Quick controls** — Approve, Always Allow, Reject, Interrupt (Ctrl+C), Restart, arrow keys
- **Event log** — History of all agent events

### Agent Profiles

Profiles are named presets that configure how an agent behaves. Define them in `config.yaml` or via the API:

```yaml
profiles:
  careful-reviewer:
    description: "Thorough code reviewer"
    system_prompt: "You are a meticulous code reviewer..."
    instructions: "Review all changes carefully before committing"
    start_sequence:
      - action: wait
        value: "3"
      - action: send
        value: "/review"
```

- **system_prompt** — Injected as the agent's system prompt
- **instructions** — Appended to the task description
- **start_sequence** — Automated steps after agent boot (`wait`, `send`, `wait_for_idle`)

Use the **A/B comparison** feature to spawn multiple agents with different profiles on the same task and compare their approaches side-by-side.

### IM Connectors

Agent Forge supports multiple IM platforms through its connector system. Configure connectors in the Settings page or directly in `config.yaml`:

| Platform | Library | Status |
|---|---|---|
| **Telegram** | `python-telegram-bot>=21.0` | Full support |
| **Discord** | `discord.py>=2.3` | Stub (coming soon) |
| **Slack** | `slack-bolt>=1.18` | Stub (coming soon) |
| **WhatsApp** | Baileys (Node.js sidecar) | Stub (coming soon) |
| **Signal** | signal-cli (subprocess) | Stub (coming soon) |

Each project can bind to specific channels across different connectors. When a channel is bound to exactly one project, messages are auto-routed without needing the `@project` prefix.

**Commands** (supported on all connectors):

| Command | Description |
|---|---|
| `/status` | Show all projects and agents |
| `/projects` | List registered projects |
| `/spawn project [task]` | Spawn a new agent |
| `/kill agent_id` | Kill an agent |
| `@project message` | Send message to most recent agent in project |
| `@project:agent_id message` | Send to a specific agent |

Attach images, videos, audio, or documents to any message — they'll be processed and staged in the agent's worktree.

**Per-project channel bindings** allow you to control which channels receive agent status notifications (outbound) and which channels can send commands to agents (inbound).

---

## Configuration

Edit `config.yaml` directly or use `forge init` to generate it:

```yaml
server:
  host: "0.0.0.0"
  port: 8080
  secret_key: "change-me-in-production"

connectors:
  my-telegram:
    type: telegram
    enabled: true
    credentials:
      bot_token: "123456:ABC..."
    settings:
      allowed_users: []  # Empty = allow all
  company-discord:
    type: discord
    enabled: true
    credentials:
      bot_token: "MTIz..."

defaults:
  max_agents_per_project: 5
  sandbox: true
  claude_command: "claude --dangerously-skip-permissions --model opus"
  claude_env:
    CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS: "1"
  poll_interval_seconds: 3
  agent_instructions: |
    # Instructions for all agents
    Always run tests after making changes.

profiles:
  fast-writer:
    description: "Quick implementation agent"
    instructions: "Focus on speed, skip extensive testing"
  careful-reviewer:
    description: "Thorough code reviewer"
    system_prompt: "You are a meticulous code reviewer..."
    start_sequence:
      - action: wait
        value: "3"
      - action: send
        value: "/review"

projects:
  my-api:
    path: "~/repos/my-api"
    default_branch: "main"
    max_agents: 3
    description: "Backend REST API"
    agent_instructions: "Use pytest for all tests"
    context_files:
      - "docs/architecture.md"
      - "CLAUDE.md"
    channels:
      - connector_id: my-telegram
        channel_id: "-1001234567890"
        channel_name: "API Dev Chat"
        inbound: true
        outbound: true
```

Legacy `telegram:` config is automatically migrated to the `connectors:` format on first load.

Reload config without restarting: `POST /api/config/reload` or use the button in the dashboard.

---

## API

### Core

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check (status, agents, uptime) |
| `GET` | `/api/stats` | Agent count breakdown by status |
| `GET` | `/api/projects` | List registered projects |
| `GET` | `/api/config` | Get full config (credentials masked) |
| `POST` | `/api/config/reload` | Reload config.yaml |

### Agents

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/agents` | List agents (`?project=` to filter) |
| `GET` | `/api/agents/{id}` | Get single agent |
| `POST` | `/api/agents` | Spawn agent `{project, task, profile?}` |
| `POST` | `/api/agents/compare` | Spawn comparison `{project, task, profiles}` |
| `DELETE` | `/api/agents/{id}` | Kill agent |
| `POST` | `/api/agents/{id}/message` | Send message `{text}` |
| `POST` | `/api/agents/{id}/control` | Send control `{action}` |
| `POST` | `/api/agents/{id}/restart` | Kill and respawn with same config |
| `GET` | `/api/agents/{id}/terminal` | Get terminal output |
| `GET` | `/api/agents/{id}/events` | Get agent events |
| `GET` | `/api/events` | All events (filterable) |

### Projects

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/config/projects` | Add project |
| `PUT` | `/api/config/projects/{name}` | Update project |
| `DELETE` | `/api/config/projects/{name}` | Delete project |
| `POST` | `/api/config/projects/{name}/channels` | Add channel binding |
| `DELETE` | `/api/config/projects/{name}/channels/{idx}` | Remove channel binding |

### Profiles

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/profiles` | List all profiles |
| `GET` | `/api/profiles/{name}` | Get profile details |
| `POST` | `/api/config/profiles/{name}` | Create profile |
| `PUT` | `/api/config/profiles/{name}` | Update profile |
| `DELETE` | `/api/config/profiles/{name}` | Delete profile |

### Connectors

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/config/connectors` | List all connectors |
| `POST` | `/api/config/connectors` | Add connector |
| `PUT` | `/api/config/connectors/{id}` | Update connector |
| `DELETE` | `/api/config/connectors/{id}` | Delete connector |
| `POST` | `/api/config/connectors/{id}/test` | Test connector connectivity |
| `GET` | `/api/config/connectors/{id}/channels` | List available channels |
| `POST` | `/api/config/connectors/{id}/validate-channel` | Validate a channel ID |

### Settings & WebSocket

| Method | Path | Description |
|---|---|---|
| `PUT` | `/api/config/defaults` | Update default settings |
| `PUT` | `/api/config/telegram` | Update legacy telegram config |
| `POST` | `/api/hooks/event` | Receive Claude Code hook events |
| `WS` | `/ws` | Real-time status and terminal updates |
| `WS` | `/ws/logs` | Server log streaming |

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│              Web Dashboard (:8080)               │
│    Dashboard · Agent Detail · Config · Console   │
└────────────────────┬────────────────────────────┘
                     │ WebSocket + REST
┌────────────────────▼────────────────────────────┐
│            Agent Forge Core (FastAPI)            │
│                                                  │
│  ProjectRegistry ─── config.yaml + profiles      │
│  AgentManager ────── tmux sessions + worktrees   │
│  StatusMonitor ───── polls + broadcasts          │
│  LogManager ──────── server log streaming        │
│  HookReporter ────── sub-agent event receiver    │
│  ConnectorManager ── IM routing + lifecycle      │
│  │ ├─ TelegramConnector                          │
│  │ ├─ DiscordConnector                           │
│  │ ├─ SlackConnector                             │
│  │ ├─ WhatsAppConnector                          │
│  │ └─ SignalConnector                            │
│  MediaHandler ────── ffmpeg / whisper            │
└──────┬──────────────────────┬───────────────────┘
       │                      │
  ~/repos/api            ~/repos/web
  └─ .worktrees/         └─ .worktrees/
     ├─ agent-a1b2          └─ agent-c3d4
     │  └─ branch: agent/a1b2/fix-auth
     └─ agent-e5f6
        └─ branch: agent/e5f6/add-tests
```

Each agent runs as a Claude Code process inside a tmux session, working in an isolated git worktree with its own branch. The StatusMonitor polls tmux output to detect status changes and broadcasts updates over WebSocket to all connected dashboard clients. The HookReporter receives lifecycle events from Claude Code hooks to track sub-agent spawns in real time.

---

## Auto-Start Service

Generate a service file for your OS to start Agent Forge on boot:

```bash
# Preview what will be generated
forge service --dry-run

# Install the service
forge service
```

- **macOS**: Creates a `~/Library/LaunchAgents/com.agentforge.server.plist`
- **Linux**: Creates a `~/.config/systemd/user/agent-forge.service`

---

## Development

```bash
pip install -e ".[dev]"
python3 -m pytest tests/ -v
```

Tests mock all subprocess calls (tmux, git, ffmpeg, whisper) — no external dependencies needed for testing.

---

## License

MIT
