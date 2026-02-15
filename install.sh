#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────────
# Agent Forge — Install Script
# ──────────────────────────────────────────────────────────────────────────────

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }
info() { echo -e "  ${CYAN}→${NC} $1"; }

echo ""
echo -e "${BOLD}  Agent Forge — Installer${NC}"
echo ""

# ──────────────────────────────────────────────────────────────────────────────
# 1. Check system dependencies
# ──────────────────────────────────────────────────────────────────────────────

echo -e "${BOLD}  Checking dependencies...${NC}"
echo ""

MISSING=0

# Python 3.11+
if command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)")
    PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)")
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 11 ]; then
        ok "Python $PY_VERSION"
    else
        fail "Python $PY_VERSION (need 3.11+)"
        MISSING=1
    fi
else
    fail "Python 3 not found"
    MISSING=1
fi

# tmux
if command -v tmux &>/dev/null; then
    TMUX_VER=$(tmux -V | head -1)
    ok "$TMUX_VER"
else
    fail "tmux not found"
    info "Install: brew install tmux (macOS) / apt install tmux (Linux)"
    MISSING=1
fi

# git
if command -v git &>/dev/null; then
    GIT_VER=$(git --version)
    ok "$GIT_VER"
else
    fail "git not found"
    MISSING=1
fi

# claude (optional but recommended)
if command -v claude &>/dev/null; then
    ok "Claude Code CLI found"
else
    warn "Claude Code CLI not found (install: npm install -g @anthropic-ai/claude-code)"
fi

# ffmpeg (optional)
if command -v ffmpeg &>/dev/null; then
    ok "ffmpeg found (video/media support)"
else
    warn "ffmpeg not found (optional — needed for media handling)"
fi

echo ""

if [ "$MISSING" -ne 0 ]; then
    echo -e "  ${RED}Missing required dependencies. Please install them and re-run.${NC}"
    echo ""
    exit 1
fi

# ──────────────────────────────────────────────────────────────────────────────
# 2. Create virtual environment
# ──────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo -e "${BOLD}  Setting up virtual environment...${NC}"
echo ""

if [ -d "$VENV_DIR" ]; then
    info "Virtual environment already exists at .venv"
else
    python3 -m venv "$VENV_DIR"
    ok "Created virtual environment at .venv"
fi

# Activate
source "$VENV_DIR/bin/activate"
ok "Activated .venv"

# ──────────────────────────────────────────────────────────────────────────────
# 3. Install package
# ──────────────────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}  Installing Agent Forge...${NC}"
echo ""

pip install --upgrade pip -q 2>/dev/null
pip install -e "$SCRIPT_DIR" -q 2>/dev/null
ok "Installed agent-forge and dependencies"

# Install optional IM connector support
echo ""
echo -e "${BOLD}  IM Connectors (optional)${NC}"
echo ""

read -p "  Install Telegram support (python-telegram-bot)? [y/N] " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    pip install -e "$SCRIPT_DIR[telegram]" -q 2>/dev/null
    ok "Installed Telegram support"
fi

read -p "  Install Discord support (discord.py)? [y/N] " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    pip install -e "$SCRIPT_DIR[discord]" -q 2>/dev/null
    ok "Installed Discord support"
fi

read -p "  Install Slack support (slack-bolt)? [y/N] " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    pip install -e "$SCRIPT_DIR[slack]" -q 2>/dev/null
    ok "Installed Slack support"
fi

# ──────────────────────────────────────────────────────────────────────────────
# 4. Verify installation
# ──────────────────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}  Verifying...${NC}"
echo ""

if "$VENV_DIR/bin/forge" --help &>/dev/null; then
    ok "forge command works"
else
    fail "forge command not found — check installation"
    exit 1
fi

# ──────────────────────────────────────────────────────────────────────────────
# 5. Done
# ──────────────────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}${GREEN}  Installation complete!${NC}"
echo ""
echo "  Next steps:"
echo ""
echo -e "    ${CYAN}source .venv/bin/activate${NC}    # activate the environment"
echo -e "    ${CYAN}forge init${NC}                    # create config.yaml"
echo -e "    ${CYAN}forge start${NC}                   # start the server"
echo -e "    ${CYAN}forge start -d${NC}                # start as daemon"
echo ""
echo "  Other commands:"
echo ""
echo -e "    ${CYAN}forge status${NC}                  # check if running"
echo -e "    ${CYAN}forge stop${NC}                    # stop the daemon"
echo -e "    ${CYAN}forge restart${NC}                 # restart the daemon"
echo -e "    ${CYAN}forge service${NC}                 # generate auto-start service"
echo ""
