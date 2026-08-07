#!/usr/bin/env python3
"""Atma Dashboard — FastAPI backend for atma.omnisatya.cc

Serves:
  - Mindmap (cytoscape.js) with growth scores + domain colors
  - Gyani panels: lessons, verifications, habits, alignment, report cards
  - Unified Atma + Gyani state

Port 8601 — independent from omni.omnisatya.cc on 8600.
"""

import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ── Paths ────────────────────────────────────────────────────
APP_DIR = Path(__file__).parent
STATIC = APP_DIR / "static"
OBSIDIAN_CURRICULUM = Path.home() / "Obsidian" / "Curriculum"
OBSIDIAN_ROAMINGS = Path.home() / "Obsidian" / "Satya's Roamings"
OBSIDIAN_DAILY = Path.home() / "Obsidian" / "15-Daily-Notes"
WHOOP_DB = Path.home() / "whoop-sync" / "health.db"
EDORAS_REPO = Path.home() / "edoras"
GRAPH_JSON = STATIC / "graph.json"
ALIGNMENT_DIR = OBSIDIAN_CURRICULUM / "alignment-reports"
# Atma Supabase — parsed from ~/.zshrc at runtime
_SUPABASE_URL = None

app = FastAPI(title="Atma Dashboard — atma.omnisatya.cc")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Helpers ───────────────────────────────────────────────────

def _supabase_url():
    global _SUPABASE_URL
    if _SUPABASE_URL is not None:
        return _SUPABASE_URL
    for rc in (".zshrc", ".bashrc"):
        p = os.path.expanduser("~/" + rc)
        if not os.path.exists(p):
            continue
        for line in open(p):
            if "DATABASE_URL" in line and "supabase" in line:
                _SUPABASE_URL = line.split("=", 1)[1].strip().strip("\"'")
                return _SUPABASE_URL
    return None


def _parse_lesson_title(md_path: Path) -> str:
    """Extract human-readable title from markdown H1, falling back to filename."""
    try:
        first_line = md_path.read_text().split("\n", 1)[0].strip()
        if first_line.startswith("# "):
            title = first_line[2:].strip()
            # Strip date suffix like " — 2026-08-07"
            if " — " in title:
                title = title.rsplit(" — ", 1)[0]
            return title
    except Exception:
        pass
    return md_path.stem.replace("-", " ").title()


def _score_color(score: float) -> str:
    if score >= 1.0:
        return "green"
    if score >= 0.4:
        return "amber"
    return "red"


# ── Growth API ────────────────────────────────────────────────

@app.get("/api/growth")
def growth():
    """Per-task growth scores from Supabase growth_scores_28d view,
    merged with active tasks that have 0 completions (not in the view)."""
    url = _supabase_url()
    if not url:
        return JSONResponse({"error": "no supabase url"}, status_code=503)
    try:
        import psycopg2
        con = psycopg2.connect(url, connect_timeout=8)
        cur = con.cursor()
        cur.execute(
            """SELECT task_id, title, category, completions_28d, expected_28d,
                      growth_score, last_completed
               FROM growth_scores_28d WHERE assigned_to = 1"""
        )
        rows = cur.fetchall()
        FOLD = {"edoras": "financial"}
        tasks = []
        seen_ids = set()
        for r in rows:
            seen_ids.add(r[0])
            domain = FOLD.get(r[2], r[2])
            tasks.append({
                "task_id": r[0],
                "title": r[1],
                "category": r[2],
                "domain": domain,
                "completions_28d": r[3],
                "expected_28d": float(r[4] or 1),
                "growth_score": float(r[5] or 0),
                "score_color": _score_color(float(r[5] or 0)),
                "last_completed": r[6].isoformat() if r[6] else None,
            })

        # Merge 0-completion active tasks not in growth_scores_28d
        cur.execute(
            """SELECT id, title, category
               FROM tasks
               WHERE (assigned_to = 1 OR assigned_to IS NULL)
                 AND is_active
                 AND id NOT IN (SELECT task_id FROM growth_scores_28d WHERE assigned_to = 1)
               ORDER BY category, title"""
        )
        for r in cur.fetchall():
            tid = r[0]
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            domain = FOLD.get(r[2], r[2])
            tasks.append({
                "task_id": tid,
                "title": r[1],
                "category": r[2],
                "domain": domain,
                "completions_28d": 0,
                "expected_28d": 1.0,
                "growth_score": 0.0,
                "score_color": "red",
                "last_completed": None,
            })

        # Domain rollup (includes 0-score tasks now)
        domains = {}
        for t in tasks:
            d = domains.setdefault(t["domain"], {"num": 0.0, "den": 0.0, "tasks": 0, "completed_28d": 0})
            w = t["expected_28d"] or 1.0
            d["num"] += t["growth_score"] * w
            d["den"] += w
            d["tasks"] += 1
            d["completed_28d"] += t["completions_28d"]
        for name, d in domains.items():
            d["growth_score"] = round(d["num"] / d["den"], 2) if d["den"] else 0.0
            d["score_color"] = _score_color(d["growth_score"])
            del d["num"], d["den"]

        # Today verified
        cur.execute(
            """SELECT COUNT(DISTINCT t.id) FROM tasks t
               JOIN task_completions tc ON tc.task_id = t.id
               WHERE t.assigned_to = 1 AND t.is_active AND t.is_recurring
                 AND t.recurrence_pattern ILIKE '%daily%'
                 AND tc.completed_at::date = NOW()::date"""
        )
        done_today = (cur.fetchone() or [0])[0]
        cur.execute(
            """SELECT COUNT(*) FROM tasks WHERE assigned_to = 1 AND is_active
               AND is_recurring AND recurrence_pattern ILIKE '%daily%'"""
        )
        daily_total = (cur.fetchone() or [0])[0]
        con.close()
        return {"tasks": tasks, "domains": domains,
                "today": {"verified": done_today, "daily_total": daily_total}}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)


# ── Gyani API ────────────────────────────────────────────────

@app.get("/api/gyani/lessons")
def gyani_lessons():
    """List all lessons Gyani has designed."""
    lessons = []
    if OBSIDIAN_CURRICULUM.exists():
        for domain_dir in sorted(OBSIDIAN_CURRICULUM.iterdir()):
            if not domain_dir.is_dir() or domain_dir.name.startswith("."):
                continue
            for md_file in sorted(domain_dir.glob("*.md"), reverse=True)[:50]:
                if md_file.name.startswith("gyani-"):
                    continue
                stat = md_file.stat()
                lessons.append({
                    "domain": domain_dir.name,
                    "title": _parse_lesson_title(md_file),
                    "path": str(md_file.relative_to(Path.home())),
                    "date": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "size_kb": round(stat.st_size / 1024, 1),
                })
    return {"lessons": lessons}


@app.get("/api/gyani/verifications")
def gyani_verifications():
    """Pending + verified lessons from Atma tasks with lesson links."""
    url = _supabase_url()
    if not url:
        return JSONResponse({"error": "no supabase"}, status_code=503)
    try:
        import psycopg2
        con = psycopg2.connect(url, connect_timeout=8)
        cur = con.cursor()
        # Active skill tasks — all of them, cross-referenced with Obsidian lessons
        cur.execute(
            """SELECT id, title, category, priority, created_at
               FROM tasks WHERE (assigned_to = 1 OR assigned_to IS NULL)
               AND is_active
               AND category IN ('mental','financial')
               AND priority >= 3
               ORDER BY created_at DESC"""
        )
        # Build lesson index from Obsidian for matching
        lesson_index = {}  # task title words → lesson path
        if OBSIDIAN_CURRICULUM.exists():
            for domain_dir in OBSIDIAN_CURRICULUM.iterdir():
                if not domain_dir.is_dir():
                    continue
                for md_file in domain_dir.glob("*.md"):
                    if md_file.name.startswith("gyani-"):
                        continue
                    lesson_index[_parse_lesson_title(md_file).lower()] = str(
                        md_file.relative_to(Path.home()))

        pending = []
        for r in cur.fetchall():
            task_title = r[1] or ""
            task_lower = task_title.lower()
            matched_lesson = None
            # 1) Exact match first
            for lesson_title, lesson_path in lesson_index.items():
                if task_lower == lesson_title:
                    matched_lesson = lesson_path
                    break
            # 2) Fuzzy fallback: >=3 significant word overlap
            if not matched_lesson:
                task_words = set(w for w in task_lower.split() if len(w) > 1)
                for lesson_title, lesson_path in lesson_index.items():
                    lesson_words = set(w for w in lesson_title.split() if len(w) > 1)
                    overlap = task_words & lesson_words
                    if len(overlap) >= 2:
                        matched_lesson = lesson_path
                        break
            pending.append({
                "task_id": r[0],
                "title": r[1],
                "category": r[2],
                "priority": r[3],
                "created_at": r[4].isoformat() if r[4] else None,
                "lesson_path": matched_lesson,
            })
        # Recently verified — deduped by task (latest completion only)
        cur.execute(
            """SELECT DISTINCT ON (t.id) t.id, t.title, t.category, tc.completed_at, tc.completion_quality
               FROM tasks t JOIN task_completions tc ON tc.task_id = t.id
               WHERE t.assigned_to = 1 AND t.category IN ('mental','financial')
               AND tc.completed_at >= NOW() - INTERVAL '30 days'
               ORDER BY t.id, tc.completed_at DESC"""
        )
        verified = [
            {
                "task_id": r[0],
                "title": r[1],
                "category": r[2],
                "completed_at": r[3].isoformat() if r[3] else None,
                "quality": r[4],
            }
            for r in cur.fetchall()
        ]
        con.close()
        return {"pending": pending, "verified": verified}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)


@app.get("/api/gyani/habits")
def gyani_habits():
    """7-day habit grid: workout, roaming, reading, shipping."""
    habits = {"workout": [], "roaming": [], "reading": [], "shipping": []}
    today = date.today()
    # Workout — last 7 days from WHOOP
    if WHOOP_DB.exists():
        con = sqlite3.connect(str(WHOOP_DB))
        cur = con.cursor()
        cur.execute(
            """SELECT strftime('%Y-%m-%d', start_time) AS day, COUNT(*) as count
               FROM workouts WHERE start_time >= ? AND start_time < ?
               GROUP BY day ORDER BY day""",
            (today - timedelta(days=7), today + timedelta(days=1))
        )
        workout_days = {r[0]: r[1] for r in cur.fetchall()}
        con.close()
        for i in range(6, -1, -1):
            d = (today - timedelta(days=i)).isoformat()
            habits["workout"].append({"date": d, "done": workout_days.get(d, 0) > 0})
    else:
        for i in range(6, -1, -1):
            habits["workout"].append({"date": (today - timedelta(days=i)).isoformat(), "done": False})

    # Roaming — any .md file in Satya's Roamings/ per day
    roaming_days = set()
    if OBSIDIAN_ROAMINGS.exists():
        for f in OBSIDIAN_ROAMINGS.glob("*.md"):
            d = date.fromtimestamp(f.stat().st_mtime).isoformat()
            roaming_days.add(d)
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        habits["roaming"].append({"date": d, "done": d in roaming_days})

    # Reading — check all daily notes in the 7-day window
    reading_days = set()
    for i in range(7):
        d = today - timedelta(days=i)
        daily_note = OBSIDIAN_DAILY / f"{d.isoformat()}.md"
        if daily_note.exists():
            try:
                text = daily_note.read_text()
                if re.search(r"#+\s*(Reading|Research)", text, re.IGNORECASE):
                    reading_days.add(d.isoformat())
            except Exception:
                pass
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        habits["reading"].append({"date": d, "done": d in reading_days})

    # Shipping — git commits in edoras/
    ship_days = set()
    if EDORAS_REPO.exists():
        import subprocess
        try:
            result = subprocess.run(
                ["git", "-C", str(EDORAS_REPO), "log", "--since=7 days ago", "--format=%cs", "--author=patelsatyam100@gmail.com"],
                capture_output=True, text=True
            )
            for line in result.stdout.strip().split("\n"):
                if line:
                    ship_days.add(line)
        except Exception:
            pass
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        habits["shipping"].append({"date": d, "done": d in ship_days})

    return {"habits": habits}


@app.get("/api/gyani/alignment")
def gyani_alignment():
    """Latest alignment report summary."""
    reports = []
    if ALIGNMENT_DIR.exists():
        latest = sorted(ALIGNMENT_DIR.glob("*.md"), reverse=True)
        if latest:
            f = latest[0]
            reports.append({
                "path": str(f.relative_to(Path.home())),
                "date": f.stem,
                "preview": f.read_text()[:500] if f.stat().st_size < 10000 else f.read_text()[:200] + "...",
            })
    return {"reports": reports}


@app.get("/api/gyani/report-card")
def gyani_report_card():
    """Current report card: lessons, verifications, habits, growth, alignment."""
    today_iso = date.today().isoformat()
    report = {
        "generated": today_iso,
        "lessons_designed": 0,
        "lessons_verified": 0,
        "habits": {},
        "growth": {},
        "alignment_latest": None,
    }

    # Count lessons
    if OBSIDIAN_CURRICULUM.exists():
        for domain_dir in OBSIDIAN_CURRICULUM.iterdir():
            if domain_dir.is_dir() and not domain_dir.name.startswith("."):
                report["lessons_designed"] += len(list(domain_dir.glob("*.md")))

    # Habits summary
    if WHOOP_DB.exists():
        con = sqlite3.connect(str(WHOOP_DB))
        cur = con.cursor()
        cur.execute(
            "SELECT AVG(recovery_score) FROM daily_cycles WHERE cycle_date >= ?",
            (today_iso,)
        )
        row = cur.fetchone()
        report["habits"]["avg_recovery"] = round(row[0], 1) if row and row[0] else None

        cur.execute(
            "SELECT COUNT(*) FROM workouts WHERE start_time >= ? AND start_time < ?",
            (today_iso, today_iso + "T23:59:59")
        )
        report["habits"]["workout_today"] = (cur.fetchone() or [0])[0] > 0
        con.close()

    # Growth from Atma
    url = _supabase_url()
    if url:
        try:
            import psycopg2
            con = psycopg2.connect(url, connect_timeout=8)
            cur = con.cursor()
            cur.execute(
                "SELECT category, AVG(growth_score) FROM growth_scores_28d "
                "WHERE assigned_to = 1 GROUP BY category"
            )
            domain_scores = {r[0]: float(r[1] or 0) for r in cur.fetchall()}
            report["growth"]["domains"] = domain_scores
            report["growth"]["overall"] = round(sum(domain_scores.values()) / max(len(domain_scores), 1), 2)
            con.close()
        except Exception:
            report["growth"]["error"] = "unavailable"

    # Latest alignment
    if ALIGNMENT_DIR.exists():
        latest = sorted(ALIGNMENT_DIR.glob("*.md"), reverse=True)
        if latest:
            report["alignment_latest"] = str(latest[0].relative_to(Path.home()))

    # Lessons verified
    if url:
        try:
            import psycopg2
            con = psycopg2.connect(url, connect_timeout=8)
            cur = con.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM task_completions WHERE completed_at >= ?",
                (today_iso,)
            )
            report["lessons_verified"] = (cur.fetchone() or [0])[0]
            con.close()
        except Exception:
            pass

    return {"report_card": report}


# ── Mindmap ──────────────────────────────────────────────────

@app.get("/api/mindmap")
def mindmap_data():
    """Serve pre-generated graph.json."""
    if GRAPH_JSON.exists():
        return FileResponse(GRAPH_JSON)
    return JSONResponse({"error": "graph not generated"}, status_code=503)


# ── Health ──────────────────────────────────────────────────

@app.get("/api/health")
def health():
    """Service health check."""
    return {
        "status": "ok",
        "service": "atma-dashboard",
        "version": "1.0.0",
    }


# ── Static Files ─────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/mindmap")
def mindmap_page():
    return FileResponse(STATIC / "mindmap.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")