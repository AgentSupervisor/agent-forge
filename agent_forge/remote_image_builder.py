"""Docker image builder — generates Dockerfile from toolchain manifest and builds/pushes."""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

import yaml

from .config import RemoteConfig

logger = logging.getLogger(__name__)

ENTRYPOINT_SH = r"""#!/bin/bash
set -euo pipefail

# ── All secrets arrive as env vars from AgentManager ──

# ── SSH setup ──
mkdir -p ~/.ssh
printf '%s\n' "$SSH_PRIVATE_KEY" > ~/.ssh/id_rsa
chmod 600 ~/.ssh/id_rsa
ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null

# ── Clone config repo (CLAUDE.md + .claude/agents/ skills) ──
if [ -n "${CONFIG_REPO_URL:-}" ]; then
    git clone "$CONFIG_REPO_URL" /opt/agent-config
fi

# ── Clone task repo ──
BASE_DIR="/workspace/${FORGE_AGENT_ID}-base"
WORK_DIR="/workspace/${FORGE_AGENT_ID}"
git clone "$REPO_URL" "$BASE_DIR"

# ── Create worktree on the agent's branch ──
cd "$BASE_DIR"
git worktree add "$WORK_DIR" -b "$BRANCH" origin/"${DEFAULT_BRANCH:-main}"
cd "$WORK_DIR"

# ── Inject CLAUDE.md and agent skills ──
if [ -d /opt/agent-config ]; then
    [ -f /opt/agent-config/CLAUDE.md ] && cp /opt/agent-config/CLAUDE.md "$WORK_DIR/"
    if [ -d /opt/agent-config/.claude/agents ]; then
        mkdir -p "$WORK_DIR/.claude/agents"
        cp -r /opt/agent-config/.claude/agents/. "$WORK_DIR/.claude/agents/"
    fi
fi

# ── Install hooks ──
mkdir -p "$WORK_DIR/.claude"
cat > "$WORK_DIR/.claude/settings.local.json" <<HOOKEOF
{
  "hooks": {
    "SubagentStart": [{"matcher": "", "hooks": [{"type": "command", "command": "curl -s -X POST $FORGE_SERVER/api/hooks/event -d '{\"agent_id\":\"$FORGE_AGENT_ID\",\"event\":\"SubagentStart\"}' -H 'Content-Type: application/json'"}]}],
    "SubagentStop":  [{"matcher": "", "hooks": [{"type": "command", "command": "curl -s -X POST $FORGE_SERVER/api/hooks/event -d '{\"agent_id\":\"$FORGE_AGENT_ID\",\"event\":\"SubagentStop\"}' -H 'Content-Type: application/json'"}]}]
  }
}
HOOKEOF

# ── Create .media/ dir ──
mkdir -p "$WORK_DIR/.media"

# ── Export any extra claude_env vars ──
if [ -n "${CLAUDE_ENV_JSON:-}" ]; then
    eval "$(python3 -c "
import json, sys, shlex
for k, v in json.loads(sys.argv[1]).items():
    print(f'export {k}={shlex.quote(v)}')
" "$CLAUDE_ENV_JSON")"
fi

# ── Start tmux session ──
TMUX_SESSION="forge__${FORGE_PROJECT}__${FORGE_AGENT_ID}"
tmux new-session -d -s "$TMUX_SESSION" -x 250 -y 50 -c "$WORK_DIR"

# ── Run Claude Code ──
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
"""


def generate_dockerfile(manifest: dict) -> str:
    """Generate a Dockerfile from a toolchain manifest.

    Args:
        manifest: Parsed toolchain manifest dict.

    Returns:
        Dockerfile contents as a string.
    """
    lines = [
        "FROM node:20-bookworm-slim",
        "",
        "# ── Core layer (always present) ──",
        "RUN apt-get update && apt-get install -y \\",
        "    git tmux openssh-client curl \\",
        "    && rm -rf /var/lib/apt/lists/*",
        "",
        "# Install Claude Code CLI",
        "RUN npm install -g @anthropic-ai/claude-code",
        "",
        "# Install ttyd (static binary)",
        "RUN curl -fsSL https://github.com/tsl0922/ttyd/releases/latest/download/ttyd.x86_64 \\",
        "    -o /usr/local/bin/ttyd && chmod +x /usr/local/bin/ttyd",
    ]

    # Detected toolchains
    toolchains = manifest.get("toolchains", [])
    apt_packages: list[str] = []
    install_scripts: list[str] = []

    for tc in toolchains:
        apt_packages.extend(tc.get("packages_apt", []))
        if "install_script" in tc:
            install_scripts.append(tc["install_script"])

    # Manual apt additions
    manual = manifest.get("manual", {})
    apt_packages.extend(manual.get("apt", []))

    if apt_packages:
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for pkg in apt_packages:
            if pkg not in seen:
                seen.add(pkg)
                unique.append(pkg)
        lines.append("")
        lines.append("# ── Detected toolchains ──")
        lines.append("RUN apt-get update && apt-get install -y \\")
        lines.append("    " + " ".join(unique) + " \\")
        lines.append("    && rm -rf /var/lib/apt/lists/*")

    for script in install_scripts:
        lines.append("")
        lines.append(f"RUN {script}")

    # Utilities
    utilities = manifest.get("utilities", [])
    util_apt: list[str] = []
    for u in utilities:
        util_apt.extend(u.get("packages_apt", []))

    if util_apt:
        seen_util = set()
        unique_util = []
        for pkg in util_apt:
            if pkg not in seen_util:
                seen_util.add(pkg)
                unique_util.append(pkg)
        lines.append("")
        lines.append("# ── Detected utilities ──")
        lines.append("RUN apt-get update && apt-get install -y \\")
        lines.append("    " + " ".join(unique_util) + " \\")
        lines.append("    && rm -rf /var/lib/apt/lists/*")

    # Manual npm global
    npm_global = manual.get("npm_global", [])
    if npm_global:
        lines.append("")
        lines.append("# ── Manual npm global packages ──")
        lines.append(f"RUN npm install -g {' '.join(npm_global)}")

    # Manual pip
    pip_packages = manual.get("pip", [])
    if pip_packages:
        lines.append("")
        lines.append("# ── Manual pip packages ──")
        lines.append(f"RUN pip3 install --break-system-packages {' '.join(pip_packages)}")

    lines.extend([
        "",
        "RUN mkdir -p /workspace",
        "",
        "COPY entrypoint.sh /entrypoint.sh",
        "RUN chmod +x /entrypoint.sh",
        "",
        'ENTRYPOINT ["/entrypoint.sh"]',
        "",
    ])

    return "\n".join(lines)


def build_image(
    manifest_path: str,
    remote_config: RemoteConfig,
    no_push: bool = False,
    no_cache: bool = False,
) -> bool:
    """Build and optionally push the Docker image from a manifest.

    Args:
        manifest_path: Path to toolchain-manifest.yaml.
        remote_config: RemoteConfig with image name and docker_context.
        no_push: If True, skip push and pre-pull.
        no_cache: If True, pass --no-cache to docker build.

    Returns:
        True on success, False on failure.
    """
    path = Path(manifest_path)
    if not path.exists():
        logger.error("Manifest not found: %s", manifest_path)
        print(f"Manifest not found: {manifest_path}")
        print("Run 'forge remote scan' first.")
        return False

    with open(path) as f:
        manifest = yaml.safe_load(f)
    if not manifest:
        logger.error("Empty manifest: %s", manifest_path)
        return False

    image = remote_config.image
    if not image:
        print("No image name configured in remote.image.")
        return False

    dockerfile_content = generate_dockerfile(manifest)

    with tempfile.TemporaryDirectory() as tmpdir:
        dockerfile_path = Path(tmpdir) / "Dockerfile"
        dockerfile_path.write_text(dockerfile_content)

        entrypoint_path = Path(tmpdir) / "entrypoint.sh"
        entrypoint_path.write_text(ENTRYPOINT_SH)

        # Build
        print(f"Building image {image}...")
        build_cmd = ["docker", "build", "-t", image, str(tmpdir)]
        if no_cache:
            build_cmd.insert(2, "--no-cache")

        result = subprocess.run(build_cmd, timeout=600)
        if result.returncode != 0:
            print("Docker build failed.")
            return False
        print(f"Built: {image}")

        if no_push:
            print("Skipping push (--no-push).")
            return True

        # Push
        print(f"Pushing {image}...")
        result = subprocess.run(["docker", "push", image], timeout=600)
        if result.returncode != 0:
            print("Docker push failed.")
            return False
        print(f"Pushed: {image}")

        # Pre-pull on VM
        print(f"Pre-pulling on remote ({remote_config.docker_context})...")
        result = subprocess.run(
            ["docker", "--context", remote_config.docker_context, "pull", image],
            timeout=600,
        )
        if result.returncode != 0:
            print("Pre-pull on remote failed (non-fatal).")
        else:
            print("Pre-pulled on remote.")

    return True
