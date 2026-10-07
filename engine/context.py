"""Context-source rail — the pluggable personalization layer.

This is where "personalization" lives.  The engine core is identical for every
user; the *context sources* are what differ.  Satyam attaches WHOOP + Obsidian
+ git + Edoras.  A grandmother learning nurse-English attaches a different set
(her own devices / tools / irrelevant for her).

Each adapter is a small unit that answers a single question and returns a
compact, deterministic string (no LLM).  Gyani composes the enabled adapters
into a "current state" block and feeds it to the exam/tutor/lesson prompt.
Disabling a source is simply a matter of not registering it.

Adapters must be:
  - fail-open: a broken source degrades to "unknown", never raises into the loop
  - read-only: they never write to the source they observe
  - time-bounded: each returns fast (sub-second I/O, no network fan-out)
"""

from __future__ import annotations

import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

HOME = Path.home()
WHOOP_DB = HOME / "whoop-sync" / "health.db"
OBSIDIAN_CURRICULUM = HOME / "Obsidian" / "Curriculum"
OBSIDIAN_DAILY = HOME / "Obsidian" / "15-Daily-Notes"
EDORAS_REPO = HOME / "edoras"


@dataclass
class ContextSource:
    """A named adapter that yields one line of learner state."""

    key: str
    label: str
    fn: Callable[[], str | None]


def _whoop() -> str | None:
    """Recovery score for the most recent cycle (None if unavailable)."""
    if not WHOOP_DB.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{WHOOP_DB}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute(
            "SELECT recovery_score FROM daily_cycles "
            "WHERE cycle_date <= date('now') "
            "ORDER BY cycle_date DESC LIMIT 1"
        )
        row = cur.fetchone()
        con.close()
        if row and row[0] is not None:
            return f"WHOOP recovery {round(float(row[0]))}%"
        return None
    except (sqlite3.Error, ValueError):
        return None


def _obsidian_activity() -> str | None:
    """Days since last daily note / reading (None if vault missing)."""
    if not OBSIDIAN_DAILY.exists():
        return None
    notes = sorted(OBSIDIAN_DAILY.glob("*.md"))
    if not notes:
        return "no Obsidian activity yet"
    latest = datetime.fromtimestamp(max(p.stat().st_mtime for p in notes)).date()
    days = (date.today() - latest).days
    return f"Obsidian last note {days}d ago (today={latest.isoformat() if days == 0 else latest.isoformat()})"


def _git_momentum() -> str | None:
    """Recent commits across Edoras in the last 2 days (None if no repo)."""
    if not (EDORAS_REPO / ".git").exists():
        return None
    try:
        out = subprocess.run(
            ["git", "log", "--since=2 days ago", "--oneline"],
            cwd=EDORAS_REPO,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        n = len(out.splitlines()) if out else 0
        return f"git {n} commit(s) in 2d" if n else "no git commits in 2d"
    except (OSError, subprocess.SubprocessError):
        return None


# ── The registry — Satyam's default rail ────────────────────────────────
# Personalization = editing this list.  For a different user, assemble a
# different registry (e.g. a "nurse English" source that pulls her vocab list).
DEFAULT_SOURCES: list[ContextSource] = [
    ContextSource("whoop", "WHOOP", _whoop),
    ContextSource("obsidian", "Obsidian", _obsidian_activity),
    ContextSource("git", "git", _git_momentum),
]


def render(sources: list[ContextSource] | None = None) -> str:
    """Compose enabled sources into a compact state block for downstream LLM.

    Returns a string like:
        WHOOP: recovery 82%
        Obsidian: last note 0d ago
        git: 3 commit(s) in 2d

    Always returns a string (possibly empty); never raises.
    """
    srcs = sources if sources is not None else DEFAULT_SOURCES
    lines: list[str] = []
    for src in srcs:
        try:
            value = src.fn()
        except Exception:
            value = None  # fail-open
        if value:
            lines.append(f"{src.label}: {value}")
    return "\n".join(lines)
