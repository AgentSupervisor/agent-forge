# Remote Agent Spawning Infrastructure — Handover Document

**Date:** February 20, 2026
**Status:** Planning complete, ready for implementation

---

## 1. Executive Summary

This document describes the architecture for extending Agent Forge with remote Docker Swarm execution. Agent Forge today is a full-featured multi-repo agent orchestration platform: FastAPI backend, SQLite event log, tmux-based agent isolation with git worktrees, xterm.js terminal over WebSocket, psutil/GPU metrics, and a multi-platform IM connector abstraction (Telegram, Discord, Slack, WhatsApp, Signal).

The remote execution extension adds a new execution path inside `AgentManager` so that agents can optionally run on a pool of Docker Swarm workers. The existing local path — tmux sessions, git worktrees, `TerminalBridge`, `StatusMonitor` — is unchanged. Remote agents live alongside local ones in the same dashboard, the same database, the same IM routing, and the same `forge` CLI.

The design is **hybrid by default**: per-project config in `config.yaml` decides whether a project's agents spawn locally or remotely. A new `remote: RemoteConfig` section is added to `ForgeConfig`. Everything that works today continues to work without any remote configuration.

### Key Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Container orchestration | Docker Swarm | Built into Docker, zero extra tooling, `replicated-job` mode fits fire-and-forget agents |
| Job mode | `replicated-job` | Runs to completion, does not restart, Swarm queues when resources are full |
| Connectivity | SSH context from the Mac running Agent Forge | No ports to open besides SSH; `docker --context vm` just works over the existing SSH connection |
| Secrets management | Env vars pushed from Mac at spawn time | Mac is the single source of truth; nothing stored on the VM; fresh per-spawn |
| Claude CLI auth | `CLAUDE_CODE_OAUTH_TOKEN` env var | Same var Agent Forge already reads; no new mechanism needed |
| Remote terminal | ttyd on port 7681 inside the container, published to a dynamic host port | Preserves the xterm.js experience; `TerminalBridgeManager` gets a remote variant |
| CLAUDE.md / skills injection | Cloned from a config repo at container start | No image rebuild when agent instructions change; mirrors how `AgentManager._generate_claude_md` works locally |
| Image registry | GitHub Container Registry (ghcr.io) | Private repos, integrates with existing GitHub workflow |
| Image contents | Toolchain scanner auto-detects from configured projects | No manual package list maintenance; scanner reads marker files |
| Result collection | Git push from container | Agent pushes its branch; branch is the result, consistent with local worktree model |
| Cleanup | Auto-remove completed containers after configurable window | Prevents disk bloat, preserves a debugging window |
| Location decision | Per-project `execution: local/remote` in `config.yaml` | Consistent with how Agent Forge already handles per-project configuration |

---

## 2. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│  Mac running Agent Forge                                              │
│                                                                       │
│  forge start  →  uvicorn agent_forge.main:app                        │
│                                                                       │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │ AgentManager                                                  │    │
│  │                                                               │    │
│  │  spawn_agent()                                                │    │
│  │    │                                                          │    │
│  │    ├── location == LOCAL  (unchanged)                         │    │
│  │    │     git worktree → tmux session → claude command         │    │
│  │    │     session: forge__{project}__{id}                      │    │
│  │    │                                                          │    │
│  │    └── location == REMOTE  (new)                              │    │
│  │          read secrets from local env                          │    │
│  │          docker --context vm service create                   │    │
│  │          mode: replicated-job                                 │    │
│  │          service name: forge__{project}__{id}                 │    │
│  │          ttyd port: dynamic (30000–32767)                     │    │
│  │                                                               │    │
│  │  Agent dataclass gains:  location: AgentLocation              │    │
│  │                          remote_service: str | None           │    │
│  │                          ttyd_port: int | None                │    │
│  └──────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌────────────────────────┐  ┌───────────────────────────────────┐   │
│  │ TerminalBridgeManager  │  │ StatusMonitor                      │   │
│  │                        │  │                                    │   │
│  │ local agent:           │  │ local agent:                       │   │
│  │   tmux -CC attach      │  │   tmux capture-pane                │   │
│  │                        │  │                                    │   │
│  │ remote agent:          │  │ remote agent:                      │   │
│  │   ws://VM:port/ws      │  │   docker --context vm              │   │
│  │   (ttyd WebSocket)     │  │   service logs (tail)              │   │
│  └────────────────────────┘  └───────────────────────────────────┘   │
│                                                                       │
│  ConnectorManager (Telegram/Discord/Slack/WhatsApp/Signal)            │
│    unchanged — routes to agents by ID regardless of location          │
│                                                                       │
└─────────────────────────────────────┬────────────────────────────────┘
                                      │  docker --context vm (over SSH)
                                      ▼
┌──────────────────────────────────────────────────────────────────────┐
│  VM (Docker Swarm — Manager + Worker nodes)                           │
│                                                                       │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │ Agent Container  (replicated-job, service: forge__proj__a1b2c3) │  │
│  │                                                                  │  │
│  │  Entrypoint:                                                     │  │
│  │  1. Write SSH key from env → ~/.ssh/id_rsa                      │  │
│  │  2. Clone config repo → /opt/agent-config                       │  │
│  │  3. Clone task repo + git worktree add                          │  │
│  │  4. Copy CLAUDE.md + .claude/agents/ from config repo           │  │
│  │  5. Write .claude/settings.local.json (hooks → FORGE_SERVER)    │  │
│  │  6. Start tmux session: forge__{project}__{id}                  │  │
│  │  7. Run: cd worktree && FORGE_AGENT_ID={id} ... {claude_cmd}    │  │
│  │  8. Start ttyd on port 7681 (background, basic auth)            │  │
│  │  9. Wait for tmux session to exit                               │  │
│  │  10. Git push branch                                            │  │
│  │  11. Container exits → Swarm marks job complete                  │  │
│  │                                                                  │  │
│  │  Published port: dynamic host port (30000–32767) → 7681         │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  No secrets stored on VM — all pushed as env vars at spawn time       │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. Configuration

### 3.1 What Changes in `config.yaml`

Remote execution is opt-in. A `ForgeConfig` without a `remote` section behaves exactly as it does today. Two additions are needed:

1. A new top-level `remote: RemoteConfig` section with Swarm connection details.
2. A new optional `execution:` field on each `ProjectConfig` to choose `local` or `remote`.

No existing fields change.

**New Pydantic models** (extend `agent_forge/config.py`):

```python
class RemoteConfig(BaseModel):
    docker_context: str = "vm"           # docker context name (ssh://user@host)
    vm_ip: str = ""                      # VM public IP — used for ttyd URLs
    image: str = ""                      # ghcr.io/you/agent-image:latest
    ttyd_port_range_start: int = 30000   # first port in published range
    ttyd_port_range_end: int = 32767     # last port in published range
    cleanup_after_hours: int = 24        # auto-remove completed services
    cpu_limit: str = "1"                 # --limit-cpu per job
    memory_limit: str = "2G"             # --limit-memory per job
    config_repo: str = ""                # git@github.com:you/agent-config.git
    ttyd_user: str = "agent"             # ttyd basic auth username
    ttyd_pass_env: str = "AGENT_TTYD_PASS"  # env var name for ttyd password


class ForgeConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    telegram: TelegramConfig = TelegramConfig()
    connectors: dict[str, ConnectorConfig] = {}
    defaults: DefaultsConfig = DefaultsConfig()
    profiles: dict[str, AgentProfile] = {}
    projects: dict[str, ProjectConfig] = {}
    remote: RemoteConfig | None = None   # None = remote disabled
```

`ProjectConfig` gets one new optional field:

```python
class ProjectConfig(BaseModel):
    path: str
    default_branch: str = "main"
    max_agents: int | None = None
    description: str = ""
    sandbox: SandboxConfig | None = None
    channels: list[ChannelBinding] = []
    agent_instructions: str = ""
    context_files: list[str] = []
    execution: str = "local"             # "local" | "remote"
    execution_reason: str = ""           # human-readable note, e.g. "needs Xcode"
```

### 3.2 Full Config Example

```yaml
# config.yaml

server:
  host: 0.0.0.0
  port: 8080

defaults:
  claude_command: "claude --dangerously-skip-permissions --model opus"
  claude_env:
    CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS: "1"
  poll_interval_seconds: 3.0
  max_agents_per_project: 5

# Remote Swarm connection — omit this entire block to disable remote execution
remote:
  docker_context: vm                               # created via: docker context create vm --docker "host=ssh://user@VM_IP"
  vm_ip: 203.0.113.50                             # VM public IP used to build ttyd URLs
  image: ghcr.io/your-user/agent-image:latest
  ttyd_port_range_start: 30000
  ttyd_port_range_end: 32767
  cleanup_after_hours: 24
  cpu_limit: "1"
  memory_limit: 2G
  config_repo: git@github.com:your-user/agent-config.git
  ttyd_user: agent
  ttyd_pass_env: AGENT_TTYD_PASS                  # env var that holds the ttyd password

projects:

  # Remote project — agents spawn on the Swarm
  backend-api:
    path: ~/projects/backend-api
    default_branch: main
    execution: remote
    max_agents: 8
    channels:
      - connector_id: telegram-main
        channel_id: "-100123456"
        inbound: true
        outbound: true

  # Local-only project — agents stay on Mac (e.g. needs Xcode / macOS tooling)
  ios-app:
    path: ~/projects/ios-app
    default_branch: main
    execution: local
    execution_reason: "iOS build requires Xcode on macOS"
    max_agents: 2

  # Default — no execution field means local (backwards compatible)
  quick-scripts:
    path: ~/projects/quick-scripts
    default_branch: main

connectors:
  telegram-main:
    type: telegram
    enabled: true
    credentials:
      bot_token: "7123456789:AAxxxxxx"
    settings:
      allowed_users: [123456789]
```

### 3.3 Secrets

Secrets are never stored in `config.yaml`. They are read from the Mac's shell environment at spawn time and pushed to the container as env vars. The SSH private key is read from disk because multiline values are awkward in env vars.

| Secret | Env var / source | Purpose |
|--------|-----------------|---------|
| Claude OAuth token | `CLAUDE_CODE_OAUTH_TOKEN` or `~/.claude/.credentials.json` | Claude CLI auth inside container |
| GitHub token | `GITHUB_TOKEN` | Cloning and pushing from container |
| SSH private key | `~/.ssh/id_rsa` (read from file) | SSH access to GitHub / config repo |
| ttyd password | Value of `remote.ttyd_pass_env` (default: `AGENT_TTYD_PASS`) | ttyd basic auth |

**Shell setup (add to `~/.zshrc`):**

```bash
export GITHUB_TOKEN="ghp_your_token_here"
export AGENT_TTYD_PASS="choose-a-strong-password"
# CLAUDE_CODE_OAUTH_TOKEN is written by 'claude setup-token';
# or export it manually from ~/.claude/.credentials.json
```

### 3.4 Config Validation

Add a `forge remote validate` subcommand that checks the remote config end-to-end:

```
$ forge remote validate

  Checking remote configuration...

  remote.docker_context  'vm' — reachable (Docker 27.3)
  remote.image           ghcr.io/your-user/agent-image:latest — found on remote
  remote.config_repo     git@github.com:your-user/agent-config.git — cloneable
  CLAUDE_CODE_OAUTH_TOKEN  set (sk-ant-oat01-...xxxx)
  GITHUB_TOKEN             set (ghp_...xxxx)
  SSH key                  found at ~/.ssh/id_rsa
  AGENT_TTYD_PASS          set

  All checks passed. Remote agent spawning is ready.
```

```
$ forge remote validate

  remote.docker_context  'vm' — connection refused (is the VM running?)
  GITHUB_TOKEN             not set
  SSH key                  ~/.ssh/id_rsa not found

  3 issues. Remote spawning will not work until resolved.
```

---

## 4. Infrastructure Setup

### 4.1 VM Requirements

| Property | Recommendation |
|----------|---------------|
| OS | Ubuntu 22.04+ |
| Docker | Docker Engine (not Docker Desktop) with Swarm mode |
| vCPU | 4+ (agents are API-call heavy, not compute heavy) |
| RAM | 8 GB minimum; each agent job gets 2 GB by default |
| Storage | 50 GB SSD — Docker images + temporary worktrees |
| Network | SSH from Mac (port 22). ttyd port range (30000–32767) accessible from Mac only. |

### 4.2 VM Bootstrap (One-Time)

```bash
# 1. Install Docker Engine
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# 2. Initialize Swarm (single-node manager)
docker swarm init

# 3. Log in to ghcr.io to pull private images
echo "$GITHUB_PAT" | docker login ghcr.io -u YOUR_GITHUB_USER --password-stdin
```

No secrets to configure on the VM. All secrets travel from Mac to container at spawn time.

### 4.3 Mac Setup (One-Time)

```bash
# Create a Docker context pointing to the VM over SSH
docker context create vm --docker "host=ssh://user@VM_IP"

# Verify
docker --context vm info
docker --context vm run --rm hello-world
```

The `docker context` uses your existing SSH key. No additional auth setup needed.

---

## 5. Docker Golden Image

### 5.1 What Goes In — Toolchain Scanner

The image needs every tool agents might need. Maintaining this list manually guarantees misses. The `forge remote scan` command inspects every project configured in `config.yaml` (and optionally the local machine's installed tools) to discover what should be in the image.

#### 5.1.1 Core Layer (Always Included)

These are required by the agent infrastructure and are always present:

| Package | Purpose |
|---------|---------|
| Node.js 20+ | Required by Claude Code CLI |
| `@anthropic-ai/claude-code` | The agent runner |
| `git`, `tmux` | Worktree creation and session management |
| `ttyd` | Remote terminal over WebSocket |
| `openssh-client`, `curl` | Git clone via SSH, miscellaneous downloads |

#### 5.1.2 Toolchain Scanner

`forge remote scan` walks every project path from `config.yaml` and checks for toolchain marker files:

| Marker file | Detected toolchain | Packages added |
|-------------|-------------------|----------------|
| `package.json` | Node.js ecosystem | already in base |
| `requirements.txt`, `pyproject.toml`, `Pipfile` | Python | `python3`, `python3-pip`, `python3-venv` |
| `Gemfile` | Ruby | `ruby`, `ruby-bundler` |
| `go.mod` | Go | `golang` |
| `Cargo.toml` | Rust | `rustc`, `cargo` (via rustup) |
| `pom.xml`, `build.gradle` | Java/Kotlin | `openjdk-17-jdk`, `maven` or `gradle` |
| `Makefile`, `CMakeLists.txt` | C/C++ | `build-essential`, `cmake` |
| `docker-compose.yml` | Docker CLI | `docker.io` |
| `*.tf`, `.terraform/` | Terraform | `terraform` |
| `Podfile` | CocoaPods (iOS) | Skipped — flagged as `execution: local` project |

Projects with `execution: local` are automatically skipped by the scanner since they will never run on the Swarm.

The scanner can also check tools on your local Mac as a secondary signal. It uses a curated allowlist of tools that are safe inside containers (`jq`, `ripgrep`, `fd`, `wget`, `tree`, `sqlite3`, etc.) and ignores macOS-specific binaries.

#### 5.1.3 Toolchain Manifest

The scanner produces a manifest committed to the config repo (or kept locally):

```yaml
# .forge/toolchain-manifest.yaml  (auto-generated, safe to hand-edit)
generated_at: "2026-02-20T14:00:00Z"

scanned_projects:
  - name: backend-api
    path: ~/projects/backend-api
    execution: remote
    markers: [package.json, requirements.txt, docker-compose.yml]
  - name: ios-app
    path: ~/projects/ios-app
    execution: local
    skipped: true
    reason: "execution: local — macOS toolchain required"

toolchains:
  - name: python
    packages_apt: [python3, python3-pip, python3-venv]
  - name: docker-cli
    packages_apt: [docker.io]

utilities:
  - name: jq
    packages_apt: [jq]
  - name: ripgrep
    packages_apt: [ripgrep]

# Manual additions — preserved by scanner re-runs
manual:
  apt: []
  npm_global: []
  pip: []
```

#### 5.1.4 Scanner CLI

```bash
# Scan all remote-execution projects in config.yaml
forge remote scan

# Preview without writing
forge remote scan --dry-run

# Scan without checking local machine tools
forge remote scan --no-local

# Build and push image after scanning
forge remote build-image --scan
```

### 5.2 Runtime Injection

Everything that changes per-spawn is injected as env vars, not baked into the image. The container entrypoint reads them on startup.

| Item | Mechanism | Source |
|------|-----------|--------|
| Claude OAuth token | `CLAUDE_CODE_OAUTH_TOKEN` env var | Mac env / `~/.claude/.credentials.json` |
| GitHub token | `GITHUB_TOKEN` env var | Mac env |
| SSH private key | `SSH_PRIVATE_KEY` env var (multiline) | `~/.ssh/id_rsa` read by `AgentManager` |
| ttyd password | `TTYD_PASS` env var | Mac env (`AGENT_TTYD_PASS`) |
| Agent ID | `FORGE_AGENT_ID` env var | 6-char hex from `AgentManager.spawn_agent()` |
| Forge server URL | `FORGE_SERVER` env var | `http://MAC_IP:port` — for hook callbacks |
| Task repo URL | `REPO_URL` env var | From `ProjectConfig.path` (converted to remote URL) |
| Branch name | `BRANCH` env var | `{branch_prefix}/{id}/{task_slug}` |
| Task prompt | `TASK_PROMPT` env var | From spawn call |
| Config repo | `CONFIG_REPO_URL` env var | From `RemoteConfig.config_repo` |
| Claude command | `CLAUDE_CMD` env var | From `DefaultsConfig.claude_command` |
| Claude env exports | `CLAUDE_ENV_JSON` env var | JSON-encoded `DefaultsConfig.claude_env` dict |

### 5.3 Dockerfile (Generated from Manifest)

The `forge remote build-image` command reads the manifest and generates a `Dockerfile`. You never edit the Dockerfile directly.

```dockerfile
FROM node:20-bookworm-slim

# ── Core layer (always present) ──
RUN apt-get update && apt-get install -y \
    git tmux openssh-client curl \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Install ttyd (static binary)
RUN curl -fsSL https://github.com/tsl0922/ttyd/releases/latest/download/ttyd.x86_64 \
    -o /usr/local/bin/ttyd && chmod +x /usr/local/bin/ttyd

# ── Detected toolchains (from toolchain-manifest.yaml) ──
RUN apt-get update && apt-get install -y \
    python3 python3-pip python3-venv \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# ── Detected utilities ──
RUN apt-get update && apt-get install -y \
    jq ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /workspace

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
```

Each detected category gets its own `RUN` layer. Docker caches layers — adding a utility does not re-download toolchains.

### 5.4 Entrypoint Script

The entrypoint mirrors what `AgentManager.spawn_agent()` does locally, translated for a container environment. It uses the same naming conventions: `forge__{project}__{id}` tmux session, `{branch_prefix}/{id}/{task_slug}` branch, `.claude/settings.local.json` hooks.

```bash
#!/bin/bash
set -euo pipefail

# ── All secrets arrive as env vars from AgentManager ──
# CLAUDE_CODE_OAUTH_TOKEN, GITHUB_TOKEN, SSH_PRIVATE_KEY, TTYD_PASS — all set

# ── SSH setup ──
mkdir -p ~/.ssh
printf '%s\n' "$SSH_PRIVATE_KEY" > ~/.ssh/id_rsa
chmod 600 ~/.ssh/id_rsa
ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null

# ── Clone config repo (CLAUDE.md + .claude/agents/ skills) ──
git clone "$CONFIG_REPO_URL" /opt/agent-config

# ── Clone task repo ──
BASE_DIR="/workspace/${FORGE_AGENT_ID}-base"
WORK_DIR="/workspace/${FORGE_AGENT_ID}"
git clone "$REPO_URL" "$BASE_DIR"

# ── Create worktree on the agent's branch (mirrors AgentManager worktree logic) ──
cd "$BASE_DIR"
git worktree add "$WORK_DIR" -b "$BRANCH" origin/"${DEFAULT_BRANCH:-main}"
cd "$WORK_DIR"

# ── Inject CLAUDE.md and agent skills (mirrors _generate_claude_md + _copy_agent_skills) ──
[ -f /opt/agent-config/CLAUDE.md ] && cp /opt/agent-config/CLAUDE.md "$WORK_DIR/"
if [ -d /opt/agent-config/.claude/agents ]; then
    mkdir -p "$WORK_DIR/.claude/agents"
    cp -r /opt/agent-config/.claude/agents/. "$WORK_DIR/.claude/agents/"
fi

# ── Install hooks (mirrors _install_hooks) ──
# The hook reporter calls FORGE_SERVER so the Mac-side StatusMonitor sees sub-agent events
mkdir -p "$WORK_DIR/.claude"
cat > "$WORK_DIR/.claude/settings.local.json" <<EOF
{
  "hooks": {
    "SubagentStart": [{"matcher": "", "hooks": [{"type": "command", "command": "curl -s -X POST $FORGE_SERVER/api/hooks/event -d '{\"agent_id\":\"$FORGE_AGENT_ID\",\"event\":\"SubagentStart\"}' -H 'Content-Type: application/json'"}]}],
    "SubagentStop":  [{"matcher": "", "hooks": [{"type": "command", "command": "curl -s -X POST $FORGE_SERVER/api/hooks/event -d '{\"agent_id\":\"$FORGE_AGENT_ID\",\"event\":\"SubagentStop\"}' -H 'Content-Type: application/json'"}]}]
  }
}
EOF

# ── Create .media/ dir (mirrors AgentManager worktree setup) ──
mkdir -p "$WORK_DIR/.media"

# ── Export any extra claude_env vars ──
if [ -n "${CLAUDE_ENV_JSON:-}" ]; then
    eval "$(python3 -c "
import json, sys
for k, v in json.loads(sys.argv[1]).items():
    print(f'export {k}={v}')
" "$CLAUDE_ENV_JSON")"
fi

# ── Start tmux session (same naming: forge__{project}__{id}) ──
TMUX_SESSION="forge__${FORGE_PROJECT}__${FORGE_AGENT_ID}"
tmux new-session -d -s "$TMUX_SESSION" -x 250 -y 50 -c "$WORK_DIR"

# ── Run Claude Code (mirrors _build_tmux_command output) ──
tmux send-keys -t "$TMUX_SESSION" \
    "FORGE_AGENT_ID=$FORGE_AGENT_ID FORGE_SERVER=$FORGE_SERVER ${CLAUDE_CMD:-claude}" \
    Enter

# ── Start ttyd (writable, basic auth) ──
TTYD_USER="${TTYD_USER:-agent}"
ttyd -W -p 7681 -c "${TTYD_USER}:${TTYD_PASS}" \
    tmux attach-session -t "$TMUX_SESSION" &
TTYD_PID=$!

# ── Wait for Claude Code to finish ──
while tmux has-session -t "$TMUX_SESSION" 2>/dev/null; do
    sleep 5
done

# ── Push results ──
cd "$WORK_DIR"
git add -A
git commit -m "Agent ${FORGE_AGENT_ID} (${FORGE_PROJECT}): ${TASK_PROMPT:0:72}" || true
git push origin "$BRANCH" || true

# ── Cleanup ttyd ──
kill $TTYD_PID 2>/dev/null || true
echo "Agent ${FORGE_AGENT_ID} completed at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
```

### 5.5 Build and Push

```bash
# Full workflow: scan projects → update manifest → build image → push to ghcr.io → pre-pull on VM
forge remote scan
forge remote build-image

# One-shot with fresh scan
forge remote build-image --scan

# Build without pushing (local testing)
forge remote build-image --no-push

# Force full rebuild (bypass Docker layer cache)
forge remote build-image --no-cache
```

Under the hood, `forge remote build-image`:
1. Reads `.forge/toolchain-manifest.yaml`
2. Generates a `Dockerfile` in a temp directory
3. Runs `docker build -t ghcr.io/YOU/agent-image:latest .`
4. Runs `docker push ghcr.io/YOU/agent-image:latest`
5. Runs `docker --context vm pull ghcr.io/YOU/agent-image:latest` (pre-pull on VM to reduce spawn latency)

---

## 6. AgentManager Extension

### 6.1 Overview

`AgentManager` in `agent_forge/agent_manager.py` gains:

- `AgentLocation` enum (`LOCAL` / `REMOTE`)
- Two new fields on the `Agent` dataclass: `location` and `remote_service`
- A private `_spawn_remote()` method called from `spawn_agent()` when `project.execution == "remote"`
- A private `_kill_remote()` method called from `kill_agent()` for remote agents
- A `_get_remote_ttyd_port()` helper

The public API — `spawn_agent()`, `kill_agent()`, `restart_agent()`, `send_message()`, `send_control()` — does not change. Callers (API routes, IM connector handlers) are unaffected.

### 6.2 Code

```python
# agent_forge/agent_manager.py  — additions only

import json
from enum import Enum


class AgentLocation(str, Enum):
    LOCAL = "local"
    REMOTE = "remote"


@dataclass
class Agent:
    id: str
    project_name: str
    session_name: str
    worktree_path: str
    branch_name: str
    status: AgentStatus = AgentStatus.STARTING
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    last_output: str = ""
    task_description: str = ""
    sub_agent_count: int = 0
    profile: str = ""
    needs_attention: bool = False
    parked: bool = False
    output_log_path: str = ""
    last_relay_offset: int = 0
    last_response: str = ""
    last_user_message: str = ""
    # ── Remote execution (None for local agents) ──
    location: AgentLocation = AgentLocation.LOCAL
    remote_service: str | None = None   # Docker service name on the Swarm
    ttyd_port: int | None = None        # Published host port on VM


class AgentManager:
    # ... existing __init__, helpers, etc. ...

    async def spawn_agent(
        self,
        project_name: str,
        task: str = "",
        branch_prefix: str = "agent",
        profile: str = "",
    ) -> Agent:
        project = self.registry.get_project(project_name)

        # Decide execution location from project config
        execution = getattr(project, "execution", "local")
        remote_cfg = self.registry.config.remote

        if execution == "remote" and remote_cfg:
            return await self._spawn_remote(project_name, task, branch_prefix, profile)
        else:
            return await self._spawn_local(project_name, task, branch_prefix, profile)

    async def _spawn_local(
        self,
        project_name: str,
        task: str,
        branch_prefix: str,
        profile: str,
    ) -> Agent:
        """Existing local spawn logic — unchanged from current implementation."""
        # ... current spawn_agent() body ...
        agent.location = AgentLocation.LOCAL
        return agent

    async def _spawn_remote(
        self,
        project_name: str,
        task: str,
        branch_prefix: str,
        profile: str,
    ) -> Agent:
        """Spawn a replicated-job on the Docker Swarm."""
        config = self.registry.config
        project = self.registry.get_project(project_name)
        remote_cfg = config.remote

        # Enforce per-project agent limit (same check as local)
        max_agents = config.get_max_agents(project_name)
        current_count = len([a for a in self.agents.values()
                              if a.project_name == project_name])
        if current_count >= max_agents:
            raise RuntimeError(
                f"Agent limit reached for '{project_name}': {current_count}/{max_agents}"
            )

        profile_obj: AgentProfile | None = None
        if profile:
            profile_obj = config.get_profile(profile)
            if not profile_obj:
                raise ValueError(f"Profile not found: '{profile}'")

        # Agent identity — same conventions as local
        short_id = uuid.uuid4().hex[:6]
        task_slug = _sanitize_for_branch(task) if task else "task"
        branch_name = f"{branch_prefix}/{short_id}/{task_slug}"
        session_name = f"forge__{project_name}__{short_id}"  # service name = session name

        # Build claude command (mirrors _build_tmux_command)
        claude_cmd = config.defaults.claude_command
        if profile_obj and profile_obj.system_prompt.strip():
            escaped = profile_obj.system_prompt.strip().replace("'", "'\\''")
            claude_cmd = f"{claude_cmd} --append-system-prompt '{escaped}'"

        # Read all secrets from local environment NOW (Mac is source of truth)
        oauth_token = self._read_oauth_token()
        github_token = self._require_env("GITHUB_TOKEN")
        ssh_key = self._read_file(Path.home() / ".ssh" / "id_rsa")
        ttyd_pass = self._require_env(remote_cfg.ttyd_pass_env)

        # Derive task repo URL from project path (assumes it's a git remote)
        repo_url = self._get_repo_url(project.path)

        cmd = [
            "docker", "--context", remote_cfg.docker_context,
            "service", "create",
            "--name", session_name,
            "--mode", "replicated-job",
            "--restart-condition", "none",
            "--limit-cpu", remote_cfg.cpu_limit,
            "--limit-memory", remote_cfg.memory_limit,
            # Secrets from Mac
            "--env", f"CLAUDE_CODE_OAUTH_TOKEN={oauth_token}",
            "--env", f"GITHUB_TOKEN={github_token}",
            "--env", f"SSH_PRIVATE_KEY={ssh_key}",
            "--env", f"TTYD_PASS={ttyd_pass}",
            "--env", f"TTYD_USER={remote_cfg.ttyd_user}",
            # Agent identity
            "--env", f"FORGE_AGENT_ID={short_id}",
            "--env", f"FORGE_PROJECT={project_name}",
            "--env", f"FORGE_SERVER=http://{self._local_ip()}:{config.server.port}",
            # Task parameters
            "--env", f"REPO_URL={repo_url}",
            "--env", f"DEFAULT_BRANCH={project.default_branch}",
            "--env", f"BRANCH={branch_name}",
            "--env", f"TASK_PROMPT={task}",
            "--env", f"CONFIG_REPO_URL={remote_cfg.config_repo}",
            "--env", f"CLAUDE_CMD={claude_cmd}",
            "--env", f"CLAUDE_ENV_JSON={json.dumps(config.defaults.claude_env)}",
            # ttyd port — dynamic host port (0 = Swarm assigns from range)
            "--publish", f"published=0,target=7681,protocol=tcp",
            # Labels for filtering
            "--label", "forge=agent",
            "--label", f"forge.project={project_name}",
            "--label", f"forge.agent_id={short_id}",
            remote_cfg.image,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create Swarm service: {result.stderr.strip()}")

        # Wait for Swarm to assign a published port
        ttyd_port = await self._get_remote_ttyd_port(session_name, remote_cfg)

        agent = Agent(
            id=short_id,
            project_name=project_name,
            session_name=session_name,
            worktree_path="",            # no local worktree for remote agents
            branch_name=branch_name,
            task_description=task,
            profile=profile,
            location=AgentLocation.REMOTE,
            remote_service=session_name,
            ttyd_port=ttyd_port,
        )
        self.agents[short_id] = agent

        logger.info(
            "Spawned remote agent %s for project '%s' on branch '%s' "
            "(service=%s, ttyd_port=%s)",
            short_id, project_name, branch_name, session_name, ttyd_port,
        )
        return agent

    async def kill_agent(self, agent_id: str) -> bool:
        agent = self.agents.get(agent_id)
        if not agent:
            return False

        if agent.location == AgentLocation.REMOTE:
            return await self._kill_remote(agent)
        else:
            return await self._kill_local(agent)

    async def _kill_local(self, agent: Agent) -> bool:
        """Existing kill logic — extracted from kill_agent()."""
        # ... current kill_agent() body ...

    async def _kill_remote(self, agent: Agent) -> bool:
        """Remove the Swarm service for a remote agent."""
        remote_cfg = self.registry.config.remote
        if not remote_cfg:
            return False

        result = subprocess.run(
            ["docker", "--context", remote_cfg.docker_context,
             "service", "rm", agent.remote_service],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            logger.warning(
                "Failed to remove Swarm service %s: %s",
                agent.remote_service, result.stderr.strip(),
            )

        del self.agents[agent.id]
        agent.status = AgentStatus.STOPPED
        logger.info("Killed remote agent %s (service=%s)", agent.id, agent.remote_service)
        return True

    async def _get_remote_ttyd_port(
        self, service_name: str, remote_cfg: "RemoteConfig", retries: int = 15
    ) -> int | None:
        """Poll until the Swarm assigns a published host port to the service."""
        for _ in range(retries):
            result = subprocess.run(
                ["docker", "--context", remote_cfg.docker_context,
                 "service", "inspect", service_name,
                 "--format", "{{(index .Endpoint.Ports 0).PublishedPort}}"],
                capture_output=True, text=True, timeout=10,
            )
            port_str = result.stdout.strip()
            if port_str and port_str not in ("0", "<no value>"):
                return int(port_str)
            await asyncio.sleep(1)
        return None

    def _read_oauth_token(self) -> str:
        token = os.getenv("CLAUDE_CODE_OAUTH_TOKEN")
        if token:
            return token
        cred_path = Path.home() / ".claude" / ".credentials.json"
        if cred_path.exists():
            creds = json.loads(cred_path.read_text())
            return creds.get("claudeAiOauth", {}).get("token", "")
        raise RuntimeError("No Claude OAuth token. Run 'claude setup-token' first.")

    def _require_env(self, name: str) -> str:
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"Required env var {name!r} is not set.")
        return value

    def _read_file(self, path: Path) -> str:
        if not path.exists():
            raise RuntimeError(f"File not found: {path}")
        return path.read_text()

    def _get_repo_url(self, project_path: str) -> str:
        """Get the remote origin URL for the project."""
        result = subprocess.run(
            ["git", "-C", project_path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            raise RuntimeError(f"No git remote 'origin' in {project_path}")
        return result.stdout.strip()

    def _local_ip(self) -> str:
        """Best-effort: get the Mac's LAN IP so containers can reach FORGE_SERVER."""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
```

### 6.3 Database Schema

The `agent_snapshots` table in `agent_forge/database.py` gets two new columns:

```sql
ALTER TABLE agent_snapshots ADD COLUMN location TEXT DEFAULT 'local';
ALTER TABLE agent_snapshots ADD COLUMN remote_service TEXT DEFAULT NULL;
ALTER TABLE agent_snapshots ADD COLUMN ttyd_port INTEGER DEFAULT NULL;
```

`save_snapshot()` and `load_snapshots()` are updated to include these fields. `recover_sessions()` reads `location` from the snapshot so remote agents survive an Agent Forge restart.

---

## 7. Status Monitor Extension

`StatusMonitor` in `agent_forge/status_monitor.py` polls `tmux capture-pane` for local agents. For remote agents it cannot use tmux directly — the session lives inside a container on the VM.

### 7.1 Remote Polling Strategy

```
Remote agent status polling (per poll cycle):

1. docker --context vm service ps {service_name} --format {{.CurrentState}}
   → "Running N minutes ago"    →  check output for status detection
   → "Complete N minutes ago"   →  mark STOPPED, schedule cleanup
   → "Failed N minutes ago"     →  mark ERROR
   → "Preparing ..."            →  stay STARTING

2. If running: fetch last N lines of service logs via
   docker --context vm service logs {service_name} --tail 100 --no-trunc
   → apply StatusMonitor.detect_status() on the log tail
   → same pattern matching (WAITING_INPUT, ERROR, IDLE, WORKING)

3. On WORKING→IDLE: extract response from log tail (_relay_response)
4. Broadcast agent update via WebSocketManager (same as local)
5. Save snapshot to SQLite (same as local)
```

### 7.2 Code Changes to `StatusMonitor._poll()`

```python
async def _poll(self) -> None:
    for agent in self.agent_manager.list_agents():
        if agent.status == AgentStatus.STOPPED:
            continue

        if agent.location == AgentLocation.REMOTE:
            await self._poll_remote_agent(agent)
        else:
            await self._poll_local_agent(agent)   # existing logic, extracted to method

async def _poll_remote_agent(self, agent: Agent) -> None:
    """Poll a remote Swarm service for status."""
    remote_cfg = self.agent_manager.registry.config.remote
    if not remote_cfg:
        return

    # Check service state
    state_result = subprocess.run(
        ["docker", "--context", remote_cfg.docker_context,
         "service", "ps", agent.remote_service,
         "--format", "{{.CurrentState}}", "--no-trunc"],
        capture_output=True, text=True, timeout=10,
    )
    state_line = state_result.stdout.strip().splitlines()[0] if state_result.stdout.strip() else ""

    if "Complete" in state_line:
        if agent.status != AgentStatus.STOPPED:
            old_status = agent.status
            agent.status = AgentStatus.STOPPED
            if old_status == AgentStatus.WORKING:
                await self._relay_response(agent, agent.last_output)
            msg = f"Agent `{agent.id}` ({agent.project_name}) completed on Swarm"
            await self._notify_channels(agent.project_name, msg)
            if self.db:
                await log_event(self.db, agent.id, agent.project_name,
                                "status_change", {"status": AgentStatus.STOPPED.value})
        return

    if "Failed" in state_line:
        agent.status = AgentStatus.ERROR
        agent.needs_attention = True
        await self._notify_channels(
            agent.project_name,
            f"Agent `{agent.id}` ({agent.project_name}) FAILED on Swarm"
        )
        return

    # Fetch log tail for output-based status detection
    log_result = subprocess.run(
        ["docker", "--context", remote_cfg.docker_context,
         "service", "logs", agent.remote_service,
         "--tail", "100", "--no-trunc"],
        capture_output=True, text=True, timeout=15,
    )
    output = log_result.stdout

    new_status = self.detect_status(output, agent.last_output)
    if new_status != agent.status:
        old_status = agent.status
        agent.status = new_status
        if new_status in (AgentStatus.IDLE, AgentStatus.WAITING_INPUT, AgentStatus.ERROR):
            agent.needs_attention = True
        if new_status == AgentStatus.WAITING_INPUT:
            await self._notify_waiting_input(agent.id, agent.project_name, output)
        elif new_status == AgentStatus.IDLE and old_status == AgentStatus.WORKING:
            await self._relay_response(agent, output)
        if self.db:
            await log_event(self.db, agent.id, agent.project_name,
                            "status_change", {"status": new_status.value})

    agent.last_output = output
    if self.db:
        await save_snapshot(self.db, agent)
    await self.ws_manager.broadcast_agent_update(agent)
```

### 7.3 Control Actions for Remote Agents

`send_message()` and `send_control()` in `AgentManager` use `tmux_utils` today. For remote agents they must reach the container's tmux session. Two options:

**Option A: HTTP relay via the Forge hook endpoint running inside the container.**
The entrypoint could start a tiny FastAPI stub on port 7682 that accepts `POST /send` and calls `tmux send-keys` locally. This is clean but adds container complexity.

**Option B: `docker exec` through the Swarm service (simpler for v1).**

```python
async def send_message(self, agent_id: str, message: str) -> bool:
    agent = self.agents.get(agent_id)
    if not agent:
        return False

    if agent.location == AgentLocation.REMOTE:
        return await self._send_to_remote_agent(agent, message)

    # existing local path
    success = tmux_utils.send_keys(agent.session_name, message)
    ...

async def _send_to_remote_agent(self, agent: Agent, message: str) -> bool:
    """Send a message to a remote agent via docker exec into the running task."""
    remote_cfg = self.registry.config.remote
    # Get the container ID of the running task
    inspect = subprocess.run(
        ["docker", "--context", remote_cfg.docker_context,
         "service", "ps", agent.remote_service,
         "--filter", "desired-state=running",
         "--format", "{{.ID}}"],
        capture_output=True, text=True, timeout=10,
    )
    task_id = inspect.stdout.strip().splitlines()[0] if inspect.stdout.strip() else ""
    if not task_id:
        return False

    escaped = message.replace("'", "'\\''")
    result = subprocess.run(
        ["docker", "--context", remote_cfg.docker_context,
         "exec", task_id,
         "tmux", "send-keys", "-t", agent.session_name, "-l", f"'{escaped}'"],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0
```

---

## 8. Terminal Bridge Extension

`TerminalBridgeManager` in `agent_forge/terminal_bridge.py` manages `TerminalBridge` instances keyed by tmux session name. Each `TerminalBridge` runs `tmux -CC attach-session` as a subprocess and pipes output to connected WebSocket clients.

For remote agents, the terminal lives in a container running ttyd. ttyd speaks the same WebSocket protocol that xterm.js uses (the `AttachAddon` protocol). The agent detail page's `AgentTerminal` class in `static/terminal.js` needs to switch between the two modes.

### 8.1 New Class: `RemoteTerminalBridge`

```python
# agent_forge/terminal_bridge.py  — additions

class RemoteTerminalBridge:
    """Proxies a ttyd WebSocket (remote agent) to browser WebSocket clients.

    The browser connects to Agent Forge at ws://forge-host/ws/terminal/{agent_id}.
    Agent Forge connects to the VM's ttyd at ws://VM_IP:ttyd_port/ws.
    All bytes are forwarded verbatim in both directions.
    """

    def __init__(self, agent_id: str, ttyd_url: str, ttyd_user: str, ttyd_pass: str) -> None:
        self.agent_id = agent_id
        self.ttyd_url = ttyd_url       # ws://VM_IP:port/ws
        self.ttyd_user = ttyd_user
        self.ttyd_pass = ttyd_pass
        self._clients: list[WebSocket] = []
        self._ttyd_ws = None           # aiohttp ClientWebSocketResponse
        self._running = False
        self._reader_task: asyncio.Task | None = None

    async def start(self) -> bool:
        """Connect to the remote ttyd WebSocket."""
        import aiohttp, base64
        auth = base64.b64encode(f"{self.ttyd_user}:{self.ttyd_pass}".encode()).decode()
        try:
            session = aiohttp.ClientSession()
            self._ttyd_ws = await session.ws_connect(
                self.ttyd_url,
                headers={"Authorization": f"Basic {auth}"},
            )
        except Exception:
            logger.exception("Failed to connect to ttyd at %s", self.ttyd_url)
            return False
        self._running = True
        self._reader_task = asyncio.create_task(self._read_ttyd())
        logger.info("RemoteTerminalBridge started for agent %s (%s)", self.agent_id, self.ttyd_url)
        return True

    async def _read_ttyd(self) -> None:
        """Forward bytes from ttyd to all browser clients."""
        async for msg in self._ttyd_ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                for ws in list(self._clients):
                    try:
                        await ws.send_bytes(msg.data)
                    except Exception:
                        self._clients.discard(ws)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
        self._running = False

    async def add_client(self, ws: WebSocket) -> None:
        self._clients.append(ws)

    def remove_client(self, ws: WebSocket) -> bool:
        try:
            self._clients.remove(ws)
        except ValueError:
            pass
        return len(self._clients) == 0

    async def handle_input(self, data: bytes) -> None:
        """Forward input bytes from the browser to ttyd."""
        if self._ttyd_ws and not self._ttyd_ws.closed:
            await self._ttyd_ws.send_bytes(data)

    async def stop(self) -> None:
        self._running = False
        if self._ttyd_ws:
            await self._ttyd_ws.close()
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
```

### 8.2 `TerminalBridgeManager` Changes

```python
class TerminalBridgeManager:

    async def get_or_create(
        self,
        session_name: str,
        agent: Agent | None = None,
        remote_cfg: RemoteConfig | None = None,
    ) -> TerminalBridge | RemoteTerminalBridge:
        async with self._lock:
            bridge = self._bridges.get(session_name)
            if bridge is not None and bridge._running:
                return bridge

            if agent and agent.location == AgentLocation.REMOTE and remote_cfg and agent.ttyd_port:
                ttyd_url = f"ws://{remote_cfg.vm_ip}:{agent.ttyd_port}/ws"
                ttyd_pass = os.getenv(remote_cfg.ttyd_pass_env, "")
                bridge = RemoteTerminalBridge(
                    agent_id=agent.id,
                    ttyd_url=ttyd_url,
                    ttyd_user=remote_cfg.ttyd_user,
                    ttyd_pass=ttyd_pass,
                )
            else:
                bridge = TerminalBridge(session_name)

            started = await bridge.start()
            if not started:
                raise RuntimeError(
                    f"Could not connect to terminal for session '{session_name}'"
                )
            self._bridges[session_name] = bridge
            return bridge
```

### 8.3 FastAPI WebSocket Route

The existing `/ws/terminal/{agent_id}` WebSocket route in `agent_forge/main.py` is updated to pass the `Agent` and `RemoteConfig` to `get_or_create`:

```python
@app.websocket("/ws/terminal/{agent_id}")
async def ws_terminal(websocket: WebSocket, agent_id: str):
    agent = agent_manager.get_agent(agent_id)
    if not agent:
        await websocket.close(code=4004)
        return

    remote_cfg = registry.config.remote

    try:
        bridge = await bridge_manager.get_or_create(
            agent.session_name,
            agent=agent,
            remote_cfg=remote_cfg,
        )
    except RuntimeError:
        await websocket.close(code=4004)
        return

    await websocket.accept()
    await bridge.add_client(websocket)
    try:
        while True:
            data = await websocket.receive_bytes()
            await bridge.handle_input(data)
    except Exception:
        pass
    finally:
        bridge.remove_client(websocket)
```

The `AgentTerminal` class in `static/terminal.js` requires no changes — it connects to `/ws/terminal/{id}` regardless of whether the agent is local or remote. The bridge handles the difference transparently.

---

## 9. Web UI Integration

### 9.1 Dashboard Agent Cards

Remote agent cards on the dashboard (`/`) differ from local ones in two ways:

1. A "REMOTE" location badge alongside the existing status badge (working/idle/waiting/error/stopped).
2. The terminal link opens the same `/agent/{id}` page, but the `TerminalBridgeManager` serves a `RemoteTerminalBridge` instead of a local `TerminalBridge`.

In `templates/components/agent_card.html`:

```html
<!-- Location badge — add alongside existing status badge -->
{% if agent.location == "remote" %}
<span class="badge badge-remote">REMOTE</span>
{% endif %}
```

Remote agent cards show the same stats bar (CPU/MEM gauges, sub-agent count) as local ones. The metrics data for remote agents comes from `StatusMonitor`'s service log polling rather than psutil — see section 11.

### 9.2 Agent Detail Page

`/agent/{id}` uses `AgentTerminal` from `static/terminal.js`. The terminal connects to the WebSocket at `/ws/terminal/{id}`. The bridge layer handles the local vs. remote difference transparently, so the agent detail template requires no change.

One addition: display the ttyd direct URL for debugging in the info panel:

```html
{% if agent.location == "remote" and agent.ttyd_port %}
<div class="info-item">
  <span class="label">Terminal (direct)</span>
  <a href="http://{{ remote_config.vm_ip }}:{{ agent.ttyd_port }}" target="_blank">
    ttyd://{{ remote_config.vm_ip }}:{{ agent.ttyd_port }}
  </a>
</div>
{% endif %}
```

### 9.3 Spawn Modal

The spawn modal in `templates/components/spawn_modal.html` adds a location indicator. Projects with `execution: remote` show a "Swarm" tag next to the project name in the dropdown. Projects with `execution: local` show nothing (unchanged). The project config drives the decision; the user does not need to choose.

### 9.4 Metrics Page

The metrics page (`/metrics`) and `MetricsCollector` in `agent_forge/metrics_collector.py` currently use psutil to measure per-agent resource usage by looking up the tmux pane's process tree. For remote agents, psutil cannot see inside the container.

Remote agent metrics are collected separately:

```python
# agent_forge/metrics_collector.py  — remote agent metrics

def collect_remote_agent_metrics(self, agent: Agent, remote_cfg: RemoteConfig) -> dict:
    """Collect CPU/memory for a remote agent via docker stats."""
    result = subprocess.run(
        ["docker", "--context", remote_cfg.docker_context,
         "stats", "--no-stream", "--format",
         "{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"],
        capture_output=True, text=True, timeout=10,
    )
    # Parse and return — or return empty dict on failure
    ...
```

This is best-effort. If `docker stats` is too slow for the 5-second poll interval, remote agent metrics are omitted from the per-agent gauges and a "Remote — metrics unavailable" label is shown.

---

## 10. IM Connector Integration

`ConnectorManager` in `agent_forge/connectors/manager.py` routes inbound messages to agents. It is unaware of agent location — it calls `agent_manager.send_message(agent_id, text)` which dispatches to the right path internally. No connector changes are needed.

Outbound notifications (status changes, WAITING_INPUT alerts with action buttons) also work unchanged — `StatusMonitor._notify_channels()` calls `connector_manager.send_to_project_channels()` regardless of whether the agent is local or remote.

**One consideration**: WAITING_INPUT action buttons (`/approve`, `/reject`, `/interrupt`) call `agent_manager.send_control()`, which in turn calls `_send_to_remote_agent()` for remote agents using `docker exec`. As long as the container is running, this works. If the container has completed, the action is a no-op and `send_control()` returns `False`.

### 10.1 `/spawn` Bot Command

The `ConnectorManager` handles the `/spawn {project} {task}` bot command. It calls `agent_manager.spawn_agent(project_name, task)`. The `AgentManager` resolves execution location from the project config. The bot receives back the spawned `Agent` dataclass and formats the reply. The reply should include the agent's location:

```
New agent a1b2c3 spawned for backend-api [REMOTE]
Task: Fix the N+1 query in user listing
Branch: agent/a1b2c3/fix-n-1-query-in-user-listing
```

---

## 11. Cleanup and Log Retention

### 11.1 Auto-Cleanup in StatusMonitor

When `StatusMonitor._poll_remote_agent()` detects a "Complete" state, it:
1. Marks the agent `STOPPED` in memory and SQLite
2. Notifies IM channels
3. Schedules cleanup after `remote.cleanup_after_hours` hours

The scheduler uses `asyncio.create_task` with a sleep:

```python
async def _schedule_remote_cleanup(self, agent: Agent) -> None:
    remote_cfg = self.agent_manager.registry.config.remote
    await asyncio.sleep(remote_cfg.cleanup_after_hours * 3600)
    # Remove the Swarm service (if it still exists)
    subprocess.run(
        ["docker", "--context", remote_cfg.docker_context,
         "service", "rm", agent.remote_service],
        capture_output=True, timeout=10,
    )
    logger.info("Auto-cleaned remote service %s", agent.remote_service)
```

### 11.2 Cleanup CLI Command

```bash
# Remove all completed Swarm services labelled as forge agents
forge remote cleanup

# Remove services completed more than N hours ago
forge remote cleanup --older-than 48
```

```bash
#!/bin/bash
# Standalone cleanup script for cron on the VM
docker service ls --filter "label=forge=agent" --format "{{.Name}}" | while read svc; do
    state=$(docker service ps "$svc" --format "{{.CurrentState}}" 2>/dev/null | head -1)
    if echo "$state" | grep -q "Complete"; do
        docker service rm "$svc"
        echo "Removed: $svc"
    fi
done
docker container prune -f --filter "until=24h"
docker image prune -f --filter "until=168h"
```

---

## 12. New `forge remote` CLI Subcommands

The existing `forge` CLI (`agent_forge/cli.py`) gains a `remote` subcommand group:

```
forge remote validate          — check remote config, secrets, VM connection, image
forge remote scan              — scan projects, update toolchain manifest
forge remote scan --dry-run    — preview manifest changes without writing
forge remote build-image       — manifest → Dockerfile → build → push → pre-pull on VM
forge remote build-image --scan          — scan then build
forge remote build-image --no-cache      — bypass Docker layer cache
forge remote build-image --no-push       — build locally only
forge remote cleanup           — remove completed Swarm services
forge remote cleanup --older-than N      — remove services completed > N hours ago
forge remote status            — show running Swarm services labelled forge=agent
```

Existing commands (`forge init`, `forge start`, `forge stop`, `forge restart`, `forge status`, `forge service`) are unchanged.

---

## 13. Implementation Roadmap

### Phase 1: Foundation (Days 1–2)

- [ ] Provision VM, install Docker Engine, initialize Swarm (`docker swarm init`)
- [ ] Create SSH Docker context on Mac: `docker context create vm --docker "host=ssh://user@VM_IP"`
- [ ] Verify: `docker --context vm run --rm hello-world`
- [ ] Set shell env vars: `GITHUB_TOKEN`, `AGENT_TTYD_PASS`
- [ ] Run `claude setup-token` (or export `CLAUDE_CODE_OAUTH_TOKEN` manually)
- [ ] Add `remote:` block to `config.yaml` (see section 3.2)
- [ ] Implement `RemoteConfig` Pydantic model in `agent_forge/config.py`
- [ ] Implement `forge remote validate` command
- [ ] Verify: `forge remote validate` — all checks green

### Phase 2: Golden Image (Days 2–3)

- [ ] Implement `forge remote scan` (toolchain scanner reading projects from `config.yaml`)
- [ ] Run scanner against all `execution: remote` projects, review manifest
- [ ] Implement `forge remote build-image` (manifest → Dockerfile → build → push)
- [ ] Build and push to ghcr.io
- [ ] Pre-pull on VM: `docker --context vm pull ghcr.io/YOU/agent-image:latest`
- [ ] Critical: test Claude CLI auth inside container:
  ```bash
  docker --context vm service create \
    --name test-claude --mode replicated-job \
    --env "CLAUDE_CODE_OAUTH_TOKEN=${CLAUDE_CODE_OAUTH_TOKEN}" \
    ghcr.io/YOU/agent-image:latest \
    claude --version
  docker --context vm service logs test-claude
  docker --context vm service rm test-claude
  ```
- [ ] Test ttyd accessibility from Mac browser: `open http://VM_IP:PUBLISHED_PORT`

### Phase 3: AgentManager Extension (Days 3–5)

- [ ] Add `AgentLocation` enum and new fields to `Agent` dataclass
- [ ] Add `execution: str = "local"` field to `ProjectConfig`
- [ ] Implement `_spawn_remote()` in `AgentManager`
- [ ] Implement `_kill_remote()` in `AgentManager`
- [ ] Implement `_get_remote_ttyd_port()` helper
- [ ] Update `kill_agent()` to dispatch by `agent.location`
- [ ] Update `restart_agent()` to handle remote agents
- [ ] Add `location`, `remote_service`, `ttyd_port` columns to `agent_snapshots` table
- [ ] Update `save_snapshot()` and `load_snapshots()` in `database.py`
- [ ] Update `recover_sessions()` to skip remote agents (no local tmux to recover)
- [ ] Full spawn test: `forge remote validate` → set one project to `execution: remote` → spawn via web UI or IM → agent completes → git push confirmed

### Phase 4: Status Monitor Extension (Days 4–6)

- [ ] Extract local polling logic from `_poll()` into `_poll_local_agent()`
- [ ] Implement `_poll_remote_agent()` using `docker service ps` + `service logs`
- [ ] Update `_poll()` to dispatch by `agent.location`
- [ ] Test status detection: WORKING/IDLE/WAITING_INPUT/ERROR via log tail patterns
- [ ] Test WAITING_INPUT notification with action buttons via Telegram
- [ ] Test WORKING→IDLE response relay
- [ ] Implement `_schedule_remote_cleanup()` and `forge remote cleanup` command

### Phase 5: Terminal Bridge Extension (Days 5–7)

- [ ] Implement `RemoteTerminalBridge` (ttyd WebSocket proxy)
- [ ] Update `TerminalBridgeManager.get_or_create()` to dispatch by agent location
- [ ] Update `/ws/terminal/{agent_id}` route to pass `agent` and `remote_cfg`
- [ ] Test: open `/agent/{id}` for a running remote agent, type input, see output
- [ ] Test: connect two browser tabs, verify both see live output

### Phase 6: Web UI Polish (Days 6–8)

- [ ] Add "REMOTE" location badge to agent cards
- [ ] Add ttyd direct URL to agent detail info panel
- [ ] Add "Swarm" tag to remote-execution projects in spawn modal dropdown
- [ ] Verify metrics page gracefully handles agents with no psutil data
- [ ] Verify `forge remote status` shows Swarm service states in terminal

### Phase 7: Hardening (Days 8+)

- [ ] VM firewall: restrict ttyd port range (30000–32767) to Mac IP only
- [ ] VM firewall: confirm port 22 is the only inbound port for Agent Forge traffic
- [ ] Set up cleanup cron on VM (`0 * * * * /opt/scripts/cleanup-forge-agents.sh`)
- [ ] Test failure scenarios: container crash, VM unreachable, token expiry, branch push conflict
- [ ] Load test: spawn 5 remote agents simultaneously, verify Swarm queues correctly
- [ ] Monitor VM resource usage; tune `cpu_limit` / `memory_limit` if needed

---

## 14. Risk Register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Claude CLI auth breaks in a new version | Medium | High (blocks all remote agents) | Pin Claude Code version in Dockerfile; test before upgrading |
| OAuth token expires mid-task | Low | Medium (single agent fails) | Token is long-lived; `AgentManager` reads fresh token per spawn |
| VM runs out of resources | Medium | Medium (jobs queue indefinitely) | `--limit-cpu` and `--limit-memory` on every job; `docker --context vm stats` for monitoring |
| ttyd port exhaustion | Low | Low (new agents cannot expose terminal) | Range 30000–32767 provides ~2700 ports; cleanup frees ports promptly |
| Git push fails from container | Medium | Medium (results lost, agent loop exits anyway) | Entrypoint uses `|| true` to prevent crash on push failure; logs preserved for `cleanup_after_hours` |
| Config repo unavailable at clone time | Low | High (agent starts without CLAUDE.md) | Cache config in a Docker volume on VM as fallback layer in entrypoint |
| `docker exec` for remote `send_message()` is flaky | Medium | Medium (IM commands fail silently) | Implement the HTTP relay endpoint (section 7.3 Option A) if `docker exec` proves unreliable |
| `StatusMonitor` `docker service logs` calls add latency to the poll cycle | Medium | Low (status updates slower for remote agents) | Run remote polling in a separate async task with a longer interval (e.g. 10s) |
| Secret leak via `docker inspect` on VM | Low | High (API keys visible to anyone with VM SSH) | Scope VM SSH access strictly; rotate tokens if VM is compromised; consider Docker Swarm native secrets in v2 |

---

## 15. Future Improvements (v2+)

- **Docker Swarm native secrets** — move credentials out of env vars into `docker secret create` + `--secret` mount for multi-operator setups
- **Auto-scaling** — add worker nodes to the Swarm when the job queue depth exceeds a threshold; remove idle workers after a quiet period
- **Spot/preemptible instances** — reduce cost for long-running agents with preemptible VMs; handle eviction via agent restart
- **Reverse proxy for ttyd** — Caddy or nginx in front of all ttyd sessions, routed by path (`/remote/{agent_id}/`) instead of per-agent port; eliminates the dynamic port range requirement
- **Centralized log shipping** — send container logs to Loki or a similar aggregator instead of relying on `docker service logs`
- **Remote `hook_reporter.py`** — ship `agent_forge/hook_reporter.py` inside the image and call it directly instead of using curl in `settings.local.json`, for richer sub-agent event reporting
- **Metrics via Prometheus** — add a Node Exporter on the VM and a cAdvisor container for proper per-container CPU/memory metrics visible in Agent Forge's metrics page
- **Multi-VM Swarm** — add more worker nodes; the Swarm manager assigns jobs automatically; no Agent Forge code changes needed

---

## 16. Key Reference Commands

```bash
# ── Mac setup ──
docker context create vm --docker "host=ssh://user@VM_IP"
docker context ls
docker --context vm info

# ── Config validation ──
forge remote validate

# ── Toolchain scanner and image ──
forge remote scan
forge remote scan --dry-run
forge remote build-image --scan
forge remote build-image --no-cache

# ── Running Swarm services ──
docker --context vm service ls --filter "label=forge=agent"
docker --context vm service ps forge__myproject__a1b2c3
docker --context vm service logs forge__myproject__a1b2c3
docker --context vm service logs --follow forge__myproject__a1b2c3
docker --context vm service rm forge__myproject__a1b2c3

# ── Debugging ──
docker --context vm service inspect forge__myproject__a1b2c3
docker --context vm node ls
docker --context vm stats --no-stream

# ── Manual cleanup ──
forge remote cleanup
forge remote cleanup --older-than 48
docker --context vm container prune -f --filter "until=24h"
docker --context vm image prune -f --filter "until=168h"

# ── Image ──
docker build -t ghcr.io/YOU/agent-image:latest .
docker push ghcr.io/YOU/agent-image:latest
docker --context vm pull ghcr.io/YOU/agent-image:latest

# ── Forge server (unchanged) ──
forge init
forge start
forge start --daemon
forge stop
forge restart
forge status
forge service            # generate launchd (macOS) or systemd (Linux) service file
```
