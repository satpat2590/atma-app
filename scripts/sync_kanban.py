#!/usr/bin/env python3
"""Sync kanban completions back into Atma — closes the delegation loop.

A delegated Atma task is handed to the omni kanban board with idempotency key
``atma-task-<id>``. When the assigned agent finishes, the kanban task reaches
``status='done'``. This worker reads that signal and closes the loop in Atma:

    task_delegations.state = 'completed'  (+ completed_at)
    tasks.is_active        = false        (out of the active queue)

It also surfaces *stale* delegations — tasks still sitting in ``delegated``
state longer than a threshold — so an untouched handoff reflects on the agent
rather than disappearing silently.

Run manually:  python3 scripts/sync_kanban.py
Run on a timer: see systemd/atma-kanban-sync.{service,timer} (optional)
"""

import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2

KANBAN_DB = Path.home() / ".hermes" / "kanban" / "boards" / "omni" / "kanban.db"
STALE_DAYS = 5  # delegated longer than this with no completion is "stale"


def _atma_url() -> str | None:
    """Resolve the Atma Supabase URL: env → gyani profile .env → shell rc."""
    url = os.environ.get("ATMA_DATABASE_URL")
    if url:
        return url
    gyani_env = Path.home() / ".hermes" / "profiles" / "gyani" / ".env"
    if gyani_env.exists():
        for line in gyani_env.read_text().splitlines():
            if line.startswith("ATMA_DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip("\"'")
    for rc in (".zshrc", ".bashrc"):
        p = Path.home() / rc
        if p.exists():
            for line in p.read_text().splitlines():
                if "DATABASE_URL" in line and "supabase" in line:
                    return line.split("=", 1)[1].strip().strip("\"'")
    return None


def _kanban_done_tasks() -> list[dict]:
    """Return kanban tasks that are done AND back-reference an Atma task."""
    if not KANBAN_DB.exists():
        return []
    con = sqlite3.connect(str(KANBAN_DB))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute(
        "SELECT id, assignee, status, completed_at, idempotency_key "
        "FROM tasks WHERE status = 'done' AND idempotency_key LIKE 'atma-task-%'"
    )
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


def _atma_task_id(key: str) -> int | None:
    m = re.match(r"atma-task-(\d+)", key or "")
    return int(m.group(1)) if m else None


def main() -> None:
    url = _atma_url()
    if not url:
        print("ERROR: ATMA_DATABASE_URL not found", file=sys.stderr)
        sys.exit(1)

    con = psycopg2.connect(url, connect_timeout=10)
    cur = con.cursor()

    # Map agent name → user id (case-insensitive)
    cur.execute("SELECT id, name FROM users WHERE user_type = 'agent'")
    agent_id = {r[1].lower(): r[0] for r in cur.fetchall()}

    done = _kanban_done_tasks()
    completed = 0

    for t in done:
        atma_id = _atma_task_id(t["idempotency_key"])
        if atma_id is None:
            continue
        agent = agent_id.get((t["assignee"] or "").lower())
        if agent is None:
            continue
        # Only flip an open delegation; don't clobber an already-completed row.
        cur.execute(
            "UPDATE task_delegations SET state = 'completed', completed_at = NOW() "
            "WHERE task_id = %s AND to_user = %s AND state <> 'completed'",
            (atma_id, agent),
        )
        cur.execute(
            "UPDATE tasks SET is_active = false WHERE id = %s AND owner_type = 'agent'",
            (atma_id,),
        )
        completed += cur.rowcount

    # Stale delegations: still 'delegated' and untouched for > STALE_DAYS.
    cur.execute(
        "SELECT d.task_id, t.title, u.name AS agent, d.created_at "
        "FROM task_delegations d "
        "JOIN tasks t ON t.id = d.task_id "
        "JOIN users u ON u.id = d.to_user "
        "WHERE d.state = 'delegated' AND t.is_active "
        "AND d.created_at < NOW() - INTERVAL '%s days' "
        "ORDER BY d.created_at",
        (STALE_DAYS,),
    )
    stale = [
        {"task_id": r[0], "title": r[1], "agent": r[2],
         "delegated_at": r[3].isoformat() if r[3] else None}
        for r in cur.fetchall()
    ]

    con.commit()
    con.close()

    print(f"kanban completions synced: {completed}")
    print(f"stale delegations (> {STALE_DAYS}d untouched): {len(stale)}")
    for s in stale:
        print(f"  - task {s['task_id']} '{s['title'][:50]}' → {s['agent']} "
              f"(since {s['delegated_at'][:10] if s['delegated_at'] else '?'})")


if __name__ == "__main__":
    main()
