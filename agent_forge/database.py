"""SQLite database layer using aiosqlite — events log and agent snapshots."""

from __future__ import annotations

import json
import logging

import aiosqlite

from .agent_manager import Agent

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    agent_id TEXT NOT NULL,
    project_name TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agent_snapshots (
    agent_id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    session_name TEXT NOT NULL,
    worktree_path TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    status TEXT NOT NULL,
    task_description TEXT,
    created_at TEXT NOT NULL,
    last_activity TEXT NOT NULL,
    last_output TEXT,
    needs_attention INTEGER NOT NULL DEFAULT 0,
    parked INTEGER NOT NULL DEFAULT 0,
    last_response TEXT,
    last_user_message TEXT,
    profile TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id);
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_name);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
"""


async def init_db(path: str = "agent_forge.db") -> aiosqlite.Connection:
    """Create tables and return an open database connection."""
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()
    # Migrate existing tables: add new columns if missing
    await _migrate_add_columns(db)
    logger.info("Database initialised at %s", path)
    return db


async def _migrate_add_columns(db: aiosqlite.Connection) -> None:
    """Add columns introduced after initial schema (idempotent)."""
    cursor = await db.execute("PRAGMA table_info(agent_snapshots)")
    existing = {row[1] for row in await cursor.fetchall()}
    migrations = [
        ("needs_attention", "INTEGER NOT NULL DEFAULT 0"),
        ("parked", "INTEGER NOT NULL DEFAULT 0"),
        ("last_response", "TEXT"),
        ("last_user_message", "TEXT"),
        ("profile", "TEXT"),
        ("location", "TEXT DEFAULT 'local'"),
        ("remote_service", "TEXT"),
        ("ttyd_port", "INTEGER"),
    ]
    for col_name, col_def in migrations:
        if col_name not in existing:
            await db.execute(
                f"ALTER TABLE agent_snapshots ADD COLUMN {col_name} {col_def}"
            )
            logger.info("Migrated agent_snapshots: added column %s", col_name)
    await db.commit()


async def log_event(
    db: aiosqlite.Connection,
    agent_id: str,
    project_name: str,
    event_type: str,
    payload: dict | str | None = None,
) -> None:
    """Insert an event row. *payload* is stored as a JSON string."""
    payload_str = json.dumps(payload) if payload is not None else None
    await db.execute(
        "INSERT INTO events (agent_id, project_name, event_type, payload) VALUES (?, ?, ?, ?)",
        (agent_id, project_name, event_type, payload_str),
    )
    await db.commit()


async def get_events(
    db: aiosqlite.Connection,
    agent_id: str | None = None,
    project_name: str | None = None,
    event_type: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Query events with optional filters, newest first."""
    clauses: list[str] = []
    params: list[str | int] = []

    if agent_id is not None:
        clauses.append("agent_id = ?")
        params.append(agent_id)
    if project_name is not None:
        clauses.append("project_name = ?")
        params.append(project_name)
    if event_type is not None:
        clauses.append("event_type = ?")
        params.append(event_type)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"SELECT * FROM events{where} ORDER BY id DESC LIMIT ?"
    params.append(limit)

    cursor = await db.execute(query, params)
    rows = await cursor.fetchall()
    results = []
    for row in rows:
        d = dict(row)
        if d.get("payload"):
            try:
                d["payload"] = json.loads(d["payload"])
            except (json.JSONDecodeError, TypeError):
                pass
        results.append(d)
    return results


async def save_snapshot(db: aiosqlite.Connection, agent: Agent) -> None:
    """Upsert the current state of an agent into agent_snapshots."""
    await db.execute(
        """INSERT OR REPLACE INTO agent_snapshots
           (agent_id, project_name, session_name, worktree_path, branch_name,
            status, task_description, created_at, last_activity, last_output,
            needs_attention, parked, last_response, last_user_message, profile,
            location, remote_service, ttyd_port)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            agent.id,
            agent.project_name,
            agent.session_name,
            agent.worktree_path,
            agent.branch_name,
            agent.status.value,
            agent.task_description,
            agent.created_at.isoformat(),
            agent.last_activity.isoformat(),
            agent.last_output[-5000:] if agent.last_output else "",
            int(agent.needs_attention),
            int(agent.parked),
            agent.last_response[-5000:] if agent.last_response else "",
            agent.last_user_message[-2000:] if agent.last_user_message else "",
            agent.profile,
            agent.location.value,
            agent.remote_service,
            agent.ttyd_port,
        ),
    )
    await db.commit()


async def load_snapshots(db: aiosqlite.Connection) -> list[dict]:
    """Load all saved agent snapshots."""
    cursor = await db.execute("SELECT * FROM agent_snapshots")
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def delete_snapshot(db: aiosqlite.Connection, agent_id: str) -> None:
    """Remove a snapshot when an agent is killed."""
    await db.execute("DELETE FROM agent_snapshots WHERE agent_id = ?", (agent_id,))
    await db.commit()
