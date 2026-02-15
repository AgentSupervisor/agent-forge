#!/usr/bin/env python3
"""Hook reporter — forwards Claude Code hook events to Agent Forge server.

This script is invoked by Claude Code hooks (SubagentStart, SubagentStop)
configured in each agent's worktree. It reads the event JSON from stdin,
enriches it with the agent ID, and POSTs it to the Agent Forge server.

Usage (from .claude/settings.local.json hook config):
    python3 /path/to/hook_reporter.py <agent_id> <event_type> <server_url>
"""

import json
import sys
import urllib.request


def main() -> None:
    if len(sys.argv) < 3:
        return

    agent_id = sys.argv[1]
    event_type = sys.argv[2]
    server_url = sys.argv[3] if len(sys.argv) > 3 else "http://localhost:8080"

    # Read event data from stdin (Claude Code pipes JSON)
    try:
        stdin_data = sys.stdin.read()
        payload = json.loads(stdin_data) if stdin_data.strip() else {}
    except Exception:
        payload = {}

    payload["agent_id"] = agent_id
    payload["hook_event"] = event_type

    try:
        req = urllib.request.Request(
            f"{server_url}/api/hooks/event",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # Never block the agent if reporting fails


if __name__ == "__main__":
    main()
