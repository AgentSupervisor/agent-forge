"""CLI entry point — forge init / start / stop / status / service / remote."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import yaml


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT_DIR / "config.yaml"
EXAMPLE_CONFIG = ROOT_DIR / "config.example.yaml"
PID_FILE = ROOT_DIR / ".forge.pid"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_bin(name: str) -> str | None:
    """Return the path to a binary or None."""
    try:
        result = subprocess.run(
            ["which", name], capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def _read_pid() -> int | None:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            # Check if process is alive
            os.kill(pid, 0)
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            PID_FILE.unlink(missing_ok=True)
    return None


def _write_pid(pid: int) -> None:
    PID_FILE.write_text(str(pid))


# ---------------------------------------------------------------------------
# forge init
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> None:
    """Interactive config generator."""
    config_path = Path(args.config)

    if config_path.exists() and not args.force:
        print(f"Config already exists at {config_path}")
        overwrite = input("Overwrite? [y/N] ").strip().lower()
        if overwrite != "y":
            print("Aborted.")
            return

    # Load example config as base template (carries agent_instructions, etc.)
    base: dict = {}
    if EXAMPLE_CONFIG.exists():
        with open(EXAMPLE_CONFIG) as f:
            base = yaml.safe_load(f) or {}

    print("\n  Agent Forge — Configuration Setup\n")

    # Server settings
    host = input("  Server host [0.0.0.0]: ").strip() or "0.0.0.0"
    port_str = input("  Server port [8080]: ").strip() or "8080"
    port = int(port_str)

    # Claude command
    print()
    print("  Claude command — how to launch Claude Code for each agent.")
    print("  Common options:")
    print("    1. claude --dangerously-skip-permissions --model opus")
    print("    2. claude --dangerously-skip-permissions --model sonnet")
    print("    3. claude (default, will prompt for permissions)")
    choice = input("  Choose [1/2/3] or type custom: ").strip()
    if choice == "1":
        claude_cmd = "claude --dangerously-skip-permissions --model opus"
    elif choice == "2":
        claude_cmd = "claude --dangerously-skip-permissions --model sonnet"
    elif choice == "3" or not choice:
        claude_cmd = "claude"
    else:
        claude_cmd = choice

    # Agent teams env var
    enable_teams = input("\n  Enable Claude Code agent teams? [Y/n] ").strip().lower()
    claude_env = {}
    if enable_teams != "n":
        claude_env["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"

    # Max agents
    max_agents_str = input("  Max agents per project [5]: ").strip() or "5"
    max_agents = int(max_agents_str)

    # Poll interval
    poll_str = input("  Status poll interval in seconds [3]: ").strip() or "3"
    poll_interval = float(poll_str)

    # Telegram (optional)
    print()
    telegram_token = input("  Telegram bot token (leave empty to skip): ").strip()
    allowed_users: list[int] = []
    if telegram_token:
        users_str = input("  Allowed Telegram user IDs (comma-separated, empty=all): ").strip()
        if users_str:
            allowed_users = [int(u.strip()) for u in users_str.split(",") if u.strip()]

    # Projects
    print("\n  Projects — add the git repositories your agents will work on.")
    print("  Enter an empty path when done.\n")
    projects: dict[str, dict] = {}
    while True:
        path = input("  Project path (or Enter to finish): ").strip()
        if not path:
            break

        path = str(Path(os.path.expanduser(path)).resolve())
        if not Path(path).is_dir():
            print(f"    Warning: {path} does not exist")

        # Auto-detect name from directory
        default_name = Path(path).name
        name = input(f"    Project name [{default_name}]: ").strip() or default_name

        # Auto-detect default branch
        detected_branch = "main"
        try:
            result = subprocess.run(
                ["git", "-C", path, "symbolic-ref", "--short", "HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                detected_branch = result.stdout.strip()
        except Exception:
            pass
        branch = input(f"    Default branch [{detected_branch}]: ").strip() or detected_branch

        description = input(f"    Description [{name}]: ").strip() or name
        max_proj_str = input(f"    Max agents [{max_agents}]: ").strip()
        max_proj = int(max_proj_str) if max_proj_str else None

        projects[name] = {
            "path": path,
            "default_branch": branch,
            "description": description,
        }
        if max_proj is not None:
            projects[name]["max_agents"] = max_proj

        print(f"    Added: {name}\n")

    # Build config — start from example template, overlay user settings
    config = base

    config["server"] = {
        "host": host,
        "port": port,
        "secret_key": "change-me-in-production",
    }
    config["telegram"] = {
        "bot_token": telegram_token or "",
        "allowed_users": allowed_users,
    }

    # Merge defaults, preserving agent_instructions and summary from template
    base_defaults = config.get("defaults", {})
    base_defaults.update({
        "max_agents_per_project": max_agents,
        "sandbox": True,
        "claude_command": claude_cmd,
        "claude_env": claude_env,
        "poll_interval_seconds": poll_interval,
    })
    config["defaults"] = base_defaults

    config["projects"] = projects

    # Write
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print(f"\n  Config written to {config_path}")
    print(f"  {len(projects)} project(s) configured.")
    print(f"\n  Next: run 'forge start' to launch the server.\n")


# ---------------------------------------------------------------------------
# forge start
# ---------------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> None:
    """Start the Agent Forge server."""
    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        print("Run 'forge init' first to create a config file.")
        sys.exit(1)

    existing_pid = _read_pid()
    if existing_pid:
        print(f"Agent Forge is already running (PID {existing_pid}).")
        print("Run 'forge stop' first, or 'forge restart'.")
        sys.exit(1)

    # Load config for host/port
    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}
    server_cfg = raw.get("server", {})
    host = args.host or server_cfg.get("host", "0.0.0.0")
    port = args.port or server_cfg.get("port", 8080)

    if args.daemon:
        # Daemonized: run as background process
        log_file = ROOT_DIR / "agent-forge.log"
        print(f"Starting Agent Forge on {host}:{port} (daemon)...")
        print(f"Logs: {log_file}")

        with open(log_file, "a") as lf:
            proc = subprocess.Popen(
                [
                    sys.executable, "-m", "agent_forge.main",
                    "--config", str(config_path),
                    "--host", host,
                    "--port", str(port),
                ],
                stdout=lf,
                stderr=lf,
                start_new_session=True,
                cwd=str(ROOT_DIR),
            )
        _write_pid(proc.pid)
        print(f"Started (PID {proc.pid}). Dashboard: http://{host}:{port}")
    else:
        # Foreground
        print(f"Starting Agent Forge on {host}:{port}...")
        print(f"Dashboard: http://{host}:{port}")
        print("Press Ctrl+C to stop.\n")

        from .main import app
        import uvicorn

        app.state.config_path = str(config_path)

        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(str(ROOT_DIR / "agent-forge.log")),
            ],
        )

        uvicorn.run(app, host=host, port=port)


# ---------------------------------------------------------------------------
# forge stop
# ---------------------------------------------------------------------------

def cmd_stop(args: argparse.Namespace) -> None:
    """Stop a running daemon."""
    pid = _read_pid()
    if not pid:
        print("Agent Forge is not running.")
        return

    print(f"Stopping Agent Forge (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
        PID_FILE.unlink(missing_ok=True)
        print("Stopped.")
    except ProcessLookupError:
        PID_FILE.unlink(missing_ok=True)
        print("Process already gone. Cleaned up PID file.")


# ---------------------------------------------------------------------------
# forge restart
# ---------------------------------------------------------------------------

def cmd_restart(args: argparse.Namespace) -> None:
    """Restart the server."""
    pid = _read_pid()
    if pid:
        print(f"Stopping Agent Forge (PID {pid})...")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        PID_FILE.unlink(missing_ok=True)

        # Wait briefly for port to free
        import time
        time.sleep(1)

    args.daemon = True
    cmd_start(args)


# ---------------------------------------------------------------------------
# forge status
# ---------------------------------------------------------------------------

def cmd_status(args: argparse.Namespace) -> None:
    """Check server status."""
    pid = _read_pid()

    config_path = Path(args.config)
    if not config_path.exists():
        print("Config not found. Run 'forge init' first.")
        return

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}
    server_cfg = raw.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    port = server_cfg.get("port", 8080)

    # Use localhost for health check
    check_host = "127.0.0.1" if host == "0.0.0.0" else host

    if not pid:
        print("Agent Forge is not running (no PID file).")
        return

    print(f"Agent Forge — PID {pid}")

    # Try health endpoint
    try:
        import urllib.request
        url = f"http://{check_host}:{port}/health"
        req = urllib.request.urlopen(url, timeout=3)
        data = json.loads(req.read())
        print(f"  Status:  running")
        print(f"  URL:     http://{check_host}:{port}")
        print(f"  Agents:  {data.get('agents', '?')}")
        print(f"  Uptime:  {data.get('uptime', '?')}")
    except Exception:
        print(f"  Status:  process running but not responding")
        print(f"  URL:     http://{check_host}:{port}")


# ---------------------------------------------------------------------------
# forge service install
# ---------------------------------------------------------------------------

def cmd_service(args: argparse.Namespace) -> None:
    """Generate a systemd or launchd service file."""
    import platform
    system = platform.system()

    forge_bin = _check_bin("forge") or f"{sys.executable} -m agent_forge.cli"
    config_path = str(Path(args.config).resolve())
    working_dir = str(ROOT_DIR)

    if system == "Darwin":
        # macOS launchd
        label = "com.agentforge.server"
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        plist = textwrap.dedent(f"""\
            <?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
              "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            <plist version="1.0">
            <dict>
                <key>Label</key>
                <string>{label}</string>
                <key>ProgramArguments</key>
                <array>
                    <string>{sys.executable}</string>
                    <string>-m</string>
                    <string>agent_forge.main</string>
                    <string>--config</string>
                    <string>{config_path}</string>
                </array>
                <key>WorkingDirectory</key>
                <string>{working_dir}</string>
                <key>RunAtLoad</key>
                <true/>
                <key>KeepAlive</key>
                <true/>
                <key>StandardOutPath</key>
                <string>{working_dir}/agent-forge.log</string>
                <key>StandardErrorPath</key>
                <string>{working_dir}/agent-forge.log</string>
            </dict>
            </plist>
        """)

        if args.dry_run:
            print(f"Would write to: {plist_path}\n")
            print(plist)
        else:
            plist_path.parent.mkdir(parents=True, exist_ok=True)
            plist_path.write_text(plist)
            print(f"Written: {plist_path}")
            print(f"\nTo enable:  launchctl load {plist_path}")
            print(f"To disable: launchctl unload {plist_path}")

    elif system == "Linux":
        # systemd
        user = os.environ.get("USER", "root")
        unit_path = Path.home() / ".config" / "systemd" / "user" / "agent-forge.service"
        unit = textwrap.dedent(f"""\
            [Unit]
            Description=Agent Forge — Claude Code Orchestrator
            After=network.target

            [Service]
            Type=simple
            WorkingDirectory={working_dir}
            ExecStart={sys.executable} -m agent_forge.main --config {config_path}
            Restart=on-failure
            RestartSec=5

            [Install]
            WantedBy=default.target
        """)

        if args.dry_run:
            print(f"Would write to: {unit_path}\n")
            print(unit)
        else:
            unit_path.parent.mkdir(parents=True, exist_ok=True)
            unit_path.write_text(unit)
            print(f"Written: {unit_path}")
            print(f"\nTo enable:  systemctl --user enable --now agent-forge")
            print(f"To disable: systemctl --user disable --now agent-forge")
            print(f"View logs:  journalctl --user -u agent-forge -f")

    else:
        print(f"Unsupported platform: {system}")
        print("Manually run: forge start --daemon")


# ---------------------------------------------------------------------------
# forge remote validate
# ---------------------------------------------------------------------------

def cmd_remote_validate(args: argparse.Namespace) -> None:
    """Validate remote execution configuration."""
    from .registry import ProjectRegistry

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        print("Run 'forge init' first to create a config file.")
        sys.exit(1)

    registry = ProjectRegistry(config_path=str(config_path))
    remote = registry.config.remote

    if remote is None:
        print("No remote config found in config.yaml.")
        print("Add a 'remote:' section to enable remote execution.")
        sys.exit(1)

    passed = 0
    failed = 0

    def check(name: str, ok: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if ok:
            passed += 1
            print(f"  PASS  {name}")
        else:
            failed += 1
            msg = f"  FAIL  {name}"
            if detail:
                msg += f" — {detail}"
            print(msg)

    print("Validating remote config...\n")

    # 1. Docker context
    try:
        result = subprocess.run(
            ["docker", "--context", remote.docker_context, "info"],
            capture_output=True, text=True, timeout=30,
        )
        check("Docker context", result.returncode == 0,
              f"'docker --context {remote.docker_context} info' failed" if result.returncode != 0 else "")
    except FileNotFoundError:
        check("Docker context", False, "docker not found in PATH")
    except subprocess.TimeoutExpired:
        check("Docker context", False, "timed out")

    # 2. Docker image on remote
    try:
        result = subprocess.run(
            ["docker", "--context", remote.docker_context, "image", "inspect", remote.image],
            capture_output=True, text=True, timeout=30,
        )
        check("Docker image", result.returncode == 0,
              f"image '{remote.image}' not found on remote" if result.returncode != 0 else "")
    except FileNotFoundError:
        check("Docker image", False, "docker not found in PATH")
    except subprocess.TimeoutExpired:
        check("Docker image", False, "timed out")

    # 3. Config repo
    if remote.config_repo:
        try:
            result = subprocess.run(
                ["git", "ls-remote", remote.config_repo],
                capture_output=True, text=True, timeout=30,
            )
            check("Config repo", result.returncode == 0,
                  f"cannot reach '{remote.config_repo}'" if result.returncode != 0 else "")
        except FileNotFoundError:
            check("Config repo", False, "git not found in PATH")
        except subprocess.TimeoutExpired:
            check("Config repo", False, "timed out")
    else:
        check("Config repo", False, "config_repo is empty")

    # 4. CLAUDE_CODE_OAUTH_TOKEN
    has_oauth = bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))
    if not has_oauth:
        creds_path = Path.home() / ".claude" / ".credentials.json"
        has_oauth = creds_path.exists()
    check("Claude OAuth token", has_oauth,
          "set CLAUDE_CODE_OAUTH_TOKEN or ensure ~/.claude/.credentials.json exists" if not has_oauth else "")

    # 5. GITHUB_TOKEN
    has_gh = bool(os.environ.get("GITHUB_TOKEN"))
    check("GITHUB_TOKEN", has_gh,
          "set GITHUB_TOKEN env var" if not has_gh else "")

    # 6. SSH key
    ssh_key = Path.home() / ".ssh" / "id_rsa"
    check("SSH key", ssh_key.exists(),
          f"{ssh_key} not found" if not ssh_key.exists() else "")

    # 7. ttyd password env var
    ttyd_env = remote.ttyd_pass_env
    has_ttyd = bool(os.environ.get(ttyd_env))
    check(f"ttyd password ({ttyd_env})", has_ttyd,
          f"set {ttyd_env} env var" if not has_ttyd else "")

    # Summary
    total = passed + failed
    print(f"\n{passed}/{total} checks passed.")
    if failed:
        print(f"{failed} issue(s) found.")
        sys.exit(1)
    else:
        print("All checks passed.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="forge",
        description="Agent Forge — Multi-repo Claude Code orchestrator",
    )
    parser.add_argument(
        "--config", default=str(DEFAULT_CONFIG),
        help="Path to config.yaml (default: config.yaml in project root)",
    )

    sub = parser.add_subparsers(dest="command")

    # init
    p_init = sub.add_parser("init", help="Create a config.yaml interactively")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing config")

    # start
    p_start = sub.add_parser("start", help="Start the server")
    p_start.add_argument("--host", default=None, help="Override host")
    p_start.add_argument("--port", type=int, default=None, help="Override port")
    p_start.add_argument("-d", "--daemon", action="store_true", help="Run in background")

    # stop
    sub.add_parser("stop", help="Stop the daemon")

    # restart
    p_restart = sub.add_parser("restart", help="Restart the daemon")
    p_restart.add_argument("--host", default=None, help="Override host")
    p_restart.add_argument("--port", type=int, default=None, help="Override port")

    # status
    sub.add_parser("status", help="Check server status")

    # service
    p_svc = sub.add_parser("service", help="Generate systemd/launchd service file")
    p_svc.add_argument("--dry-run", action="store_true", help="Print without writing")

    # remote
    p_remote = sub.add_parser("remote", help="Remote execution commands")
    remote_sub = p_remote.add_subparsers(dest="remote_command")
    remote_sub.add_parser("validate", help="Validate remote execution config")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == "remote":
        if not getattr(args, "remote_command", None):
            p_remote.print_help()
            sys.exit(0)
        remote_commands = {
            "validate": cmd_remote_validate,
        }
        remote_commands[args.remote_command](args)
        return

    commands = {
        "init": cmd_init,
        "start": cmd_start,
        "stop": cmd_stop,
        "restart": cmd_restart,
        "status": cmd_status,
        "service": cmd_service,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
