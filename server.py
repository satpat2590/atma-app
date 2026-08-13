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
import subprocess

import psycopg2

import aiohttp

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ── Paths ────────────────────────────────────────────────────
APP_DIR = Path(__file__).parent
STATIC = APP_DIR / "static"
OBSIDIAN_CURRICULUM = Path.home() / "Obsidian" / "Curriculum"
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
    """Atma Supabase URL. Prefer env var; fall back to shell rc for dev shells."""
    global _SUPABASE_URL
    if _SUPABASE_URL is not None:
        return _SUPABASE_URL
    env_url = os.environ.get("ATMA_DATABASE_URL")
    if env_url:
        _SUPABASE_URL = env_url
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


@app.get("/api/gyani/lesson")
def gyani_lesson_content(path: str = Query(..., description="Relative path from home, e.g. Obsidian/Curriculum/finance/2026-08-07-black-scholes.md")):
    """Serve raw lesson markdown content for in-page viewing."""
    full_path = Path.home() / path
    if not full_path.exists() or not full_path.is_relative_to(Path.home()):
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        content = full_path.read_text()
        return {"path": path, "content": content, "size": len(content)}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/gyani/verifications")
def gyani_verifications():
    """Pending + verified tasks with proficiency-gated unlocking."""
    url = _supabase_url()
    if not url:
        return JSONResponse({"error": "no supabase"}, status_code=503)
    try:
        con = psycopg2.connect(url, connect_timeout=8)
        cur = con.cursor()

        # ── compute per-tag proficiency from the ledger ──
        cur.execute(
            "SELECT tag_id, ROUND(AVG(quality)::numeric, 2) AS prof, "
            "COUNT(*) AS exams, MAX(completed_at) AS last "
            "FROM verifications WHERE state = 'passed' AND tag_id IS NOT NULL "
            "GROUP BY tag_id"
        )
        tag_prof = {r[0]: {"proficiency": r[1], "exams": r[2], "last": r[3]} for r in cur.fetchall()}

        # ── exam history ──
        cur.execute(
            "SELECT v.id, v.task_id, t.title, v.state, v.quality, "
            "v.max_level_reached, v.proficiency_notes, v.started_at, v.completed_at "
            "FROM verifications v LEFT JOIN tasks t ON t.id = v.task_id "
            "ORDER BY v.started_at DESC LIMIT 20"
        )
        exams = [
            {"verification_id": r[0], "task_id": r[1], "title": r[2],
             "state": r[3], "quality": r[4], "max_level": r[5],
             "notes": r[6], "started_at": r[7].isoformat() if r[7] else None,
             "completed_at": r[8].isoformat() if r[8] else None}
            for r in cur.fetchall()
        ]
        exam_by_task = {}
        for e in exams:
            if e["task_id"] is not None and e["task_id"] not in exam_by_task:
                exam_by_task[e["task_id"]] = e

        # ── tag tree with proficiency + dependencies ──
        cur.execute("SELECT id, name, parent_tag_id, required_proficiency FROM tags ORDER BY id")
        tag_rows = cur.fetchall()
        tag_index = {r[0]: {"name": r[1], "parent": r[2], "required": float(r[3] or 3.0)} for r in tag_rows}

        # Tag dependencies
        cur.execute("SELECT tag_id, depends_on_tag_id, required_proficiency FROM tag_dependencies")
        deps_by_tag = {}
        for r in cur.fetchall():
            deps_by_tag.setdefault(r[0], []).append({"depends_on": r[1], "required": float(r[2] or 3.0)})

        # Compute unlocked tags: BFS from roots — a tag is unlocked when ALL its
        # parents AND dependency tags have proficiency >= required_proficiency
        unlocked_tags = set()
        # Roots: tags with no parent
        roots = [tid for tid, t in tag_index.items() if t["parent"] is None]
        # Also include any tag whose parent's proficiency meets threshold
        def _unlocked(tid, visited=None):
            if visited is None: visited = set()
            if tid in visited: return tid in unlocked_tags
            visited.add(tid)
            t = tag_index[tid]
            # Root tags are always visible
            if t["parent"] is None:
                unlocked_tags.add(tid)
                return True
            # Check parent proficiency
            parent_id = t["parent"]
            parent_prof = tag_prof.get(parent_id, {}).get("proficiency", 0)
            if parent_prof >= t["required"] and _unlocked(parent_id, visited):
                # Check cross-domain dependencies too
                deps_ok = True
                for dep in deps_by_tag.get(tid, []):
                    dep_prof = tag_prof.get(dep["depends_on"], {}).get("proficiency", 0)
                    if dep_prof < dep["required"]:
                        deps_ok = False
                        break
                if deps_ok:
                    unlocked_tags.add(tid)
                    return True
            return False

        for tid in tag_index:
            if tid not in unlocked_tags:
                _unlocked(tid)

        # ── active skill tasks ──
        cur.execute(
            "SELECT id, title, category, priority, created_at "
            "FROM tasks WHERE (assigned_to = 1 OR assigned_to IS NULL) "
            "AND is_active AND category IN ('mental','financial') "
            "AND priority >= 3 ORDER BY created_at DESC"
        )
        rows = cur.fetchall()

        # Lesson index
        lesson_index = {}
        if OBSIDIAN_CURRICULUM.exists():
            for domain_dir in OBSIDIAN_CURRICULUM.iterdir():
                if not domain_dir.is_dir(): continue
                for md_file in domain_dir.glob("*.md"):
                    if md_file.name.startswith("gyani-"): continue
                    lesson_index[_parse_lesson_title(md_file).lower()] = str(
                        md_file.relative_to(Path.home()))

        # Resolve tag for each task
        cur.execute("SELECT task_id, tag_id FROM task_tags")
        task_tag = {r[0]: r[1] for r in cur.fetchall()}

        pending = []
        for r in rows:
            tid, title, cat, pri, created = r
            created_iso = created.isoformat() if created else None
            task_tag_id = task_tag.get(tid)

            # Lesson matching
            matched_lesson = None
            tl = (title or "").lower()
            for lt, lp in lesson_index.items():
                if tl == lt: matched_lesson = lp; break
            if not matched_lesson:
                tw = set(w for w in tl.split() if len(w) > 1)
                for lt, lp in lesson_index.items():
                    if len(tw & set(w for w in lt.split() if len(w) > 1)) >= 2:
                        matched_lesson = lp; break

            # Locked? Tag must be in unlocked_tags (proficiency gate passed)
            locked = False
            if task_tag_id is not None and task_tag_id not in unlocked_tags:
                locked = True

            # Stage
            exam = exam_by_task.get(tid)
            if exam:
                stage = {"in_progress": "examining", "passed": "passed",
                         "failed": "failed", "abandoned": "abandoned"}.get(
                             exam["state"], "designed")
            elif locked:
                stage = "locked"
            else:
                stage = "ready" if matched_lesson else "designing"

            # What's locking it? Show the first unmet gate
            lock_reason = None
            if locked and task_tag_id is not None:
                t = tag_index.get(task_tag_id, {})
                parent_id = t.get("parent")
                if parent_id:
                    parent_prof = tag_prof.get(parent_id, {}).get("proficiency", 0)
                    parent_req = t.get("required", 3.0)
                    if parent_prof < parent_req:
                        lock_reason = (tag_index[parent_id]["name"]
                                       if parent_id in tag_index else f"tag#{parent_id}")

            pending.append({
                "task_id": tid, "title": title, "category": cat,
                "priority": pri, "created_at": created_iso,
                "lesson_path": matched_lesson, "stage": stage,
                "locked": locked,
                "tag_id": task_tag_id,
                "tag_name": tag_index[task_tag_id]["name"] if task_tag_id in tag_index else None,
                "lock_reason": lock_reason,
                "tag_proficiency": tag_prof.get(task_tag_id, {}).get("proficiency", 0) if task_tag_id else None,
                "quality": exam["quality"] if exam else None,
                "max_level": exam["max_level"] if exam else None,
                "notes": exam["notes"] if exam else None,
            })

        # Recently verified
        cur.execute(
            "SELECT DISTINCT ON (t.id) t.id, t.title, t.category, "
            "tc.completed_at, tc.completion_quality "
            "FROM tasks t JOIN task_completions tc ON tc.task_id = t.id "
            "WHERE t.assigned_to = 1 AND t.category IN ('mental','financial') "
            "AND tc.completed_at >= NOW() - INTERVAL '30 days' "
            "ORDER BY t.id, tc.completed_at DESC"
        )
        verified = [
            {"task_id": r[0], "title": r[1], "category": r[2],
             "completed_at": r[3].isoformat() if r[3] else None, "quality": r[4]}
            for r in cur.fetchall()
        ]
        con.close()
        return {
            "pending": pending,
            "verified": verified,
            "exams": exams,
            "tag_proficiency": {str(k): v for k, v in tag_prof.items()},
            "unlocked_tags": sorted(list(unlocked_tags)),
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=503)
@app.get("/api/gyani/habits")
def gyani_habits():
    """7-day habit grid: workout, reading, shipping."""
    habits = {"workout": [], "reading": [], "shipping": []}
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


# ── Tutor Chat ──────────────────────────────────────────────
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GYANI_MODEL = "deepseek/deepseek-v4-pro"


def _openrouter_key():
    """Read OpenRouter key from Gyani's profile .env (same one Gyani uses)."""
    env_path = Path.home() / ".hermes" / "profiles" / "gyani" / ".env"
    if not env_path.exists():
        return os.environ.get("OPENROUTER_API_KEY")
    for line in env_path.read_text().splitlines():
        if line.startswith("OPENROUTER_API_KEY="):
            return line.split("=", 1)[1].strip()
    return os.environ.get("OPENROUTER_API_KEY")


def _build_tutor_context() -> str:
    """Build Gyani's tutoring context: proficiency, active lessons, recent exams."""
    url = _supabase_url()
    if not url:
        return "No Atma data available."

    parts = []
    con = psycopg2.connect(url, connect_timeout=8)
    cur = con.cursor()

    # Proficiency per tag
    cur.execute(
        "SELECT tag_id, ROUND(AVG(quality)::numeric,2), COUNT(*), MAX(completed_at) "
        "FROM verifications WHERE state='passed' AND tag_id IS NOT NULL "
        "GROUP BY tag_id ORDER BY AVG(quality) DESC"
    )
    prof_rows = cur.fetchall()
    if prof_rows:
        cur.execute("SELECT id, name FROM tags WHERE id = ANY(%s)", ([r[0] for r in prof_rows],))
        tag_names = {r[0]: r[1] for r in cur.fetchall()}
        parts.append("=== SATYAM'S PROFICIENCY ===")
        for r in prof_rows:
            name = tag_names.get(r[0], f"tag#{r[0]}")
            parts.append(f"  {name}: q={r[1]} ({r[2]} exams, last={r[3].isoformat()[:10] if r[3] else '?'})")
    else:
        parts.append("=== SATYAM'S PROFICIENCY ===\n  (no exams passed yet)")

    # Active lessons
    parts.append("\n=== ACTIVE LESSONS ===")
    if OBSIDIAN_CURRICULUM.exists():
        lessons = []
        for domain_dir in OBSIDIAN_CURRICULUM.iterdir():
            if not domain_dir.is_dir() or domain_dir.name.startswith("."):
                continue
            for md_file in sorted(domain_dir.glob("*.md"), reverse=True)[:5]:
                if md_file.name.startswith("gyani-"):
                    continue
                title = _parse_lesson_title(md_file)
                lessons.append(f"  [{domain_dir.name}] {title}")
        if lessons:
            parts.extend(lessons[:8])
        else:
            parts.append("  (no lessons designed yet)")
    else:
        parts.append("  (curriculum directory not found)")

    # Recent exams
    parts.append("\n=== RECENT EXAMS ===")
    cur.execute(
        "SELECT v.state, v.quality, t.title, v.proficiency_notes "
        "FROM verifications v LEFT JOIN tasks t ON t.id = v.task_id "
        "ORDER BY v.started_at DESC LIMIT 5"
    )
    exam_rows = cur.fetchall()
    if exam_rows:
        for r in exam_rows:
            title = (r[2] or "?")[:50]
            parts.append(f"  [{r[0]}] q{r[1]} — {title}")
            if r[3]:
                parts.append(f"    {r[3][:120]}")
    else:
        parts.append("  (no exams taken yet)")

    # Pending tasks
    parts.append("\n=== PENDING TASKS ===")
    cur.execute(
        "SELECT t.title, t.priority, g.name FROM tasks t "
        "JOIN task_tags tt ON tt.task_id = t.id "
        "JOIN tags g ON g.id = tt.tag_id "
        "WHERE t.is_active AND t.category IN ('mental','financial') "
        "AND t.priority >= 3 ORDER BY t.created_at DESC LIMIT 10"
    )
    task_rows = cur.fetchall()
    for r in task_rows:
        parts.append(f"  [{r[2] or '?'}] pri{r[1]} — {r[0][:60]}")

    con.close()
    return "\n".join(parts)


@app.post("/api/gyani/tutor")
async def gyani_tutor(request: Request):
    """Single-turn tutoring chat. Gyani responds with full knowledge of Satyam's progress.

    Request: {"message": "...", "mode": "tutor"|"practice"}
    Response: {"reply": "Gyani's response..."}

    mode="tutor":    explain concepts, use analogies (default)
    mode="practice": immersive language practice — stay in target language,
                     romaji only on request, correct errors inline
    """
    body = await request.json()
    user_message = body.get("message", "").strip()
    mode = body.get("mode", "tutor")
    if not user_message:
        return JSONResponse({"error": "message required"}, status_code=400)
    if len(user_message) > 2000:
        return JSONResponse({"error": "message too long"}, status_code=400)

    api_key = _openrouter_key()
    if not api_key:
        return JSONResponse({"error": "no API key configured"}, status_code=503)

    context = _build_tutor_context()

    if mode == "practice":
        system_prompt = (
            "You are Gyani (ज्ञानी), Satyam's language practice partner. You are in "
            "PRACTICE MODE — immersive, not tutoring, not examining.\n\n"
            "RULES:\n"
            "- Converse in the TARGET LANGUAGE the student is learning (currently Japanese).\n"
            "- Keep it beginner-appropriate: short sentences, common vocabulary.\n"
            "- Provide romaji ONLY when the student asks or is clearly stuck. Never "
            "dump a full translation — that is a crutch.\n"
            "- When the student makes a mistake, correct it inline and briefly explain WHY "
            "(e.g. 'it's は not が here because you're marking the topic').\n"
            "- Never grade, never judge, never write to the ledger. This is safe practice.\n"
            "- After your in-language reply, you may add ONE short English note if it helps "
            "clarity, but keep the language immersion primary.\n\n"
            "Satyam's current state:\n" + context + "\n\n"
            "Respond as Gyani. Stay in the target language. Keep it warm and encouraging."
        )
    else:
        system_prompt = (
            "You are Gyani (ज्ञानी), Satyam's personal professor. You are tutoring — "
            "NOT examining. No grading, no ledger writes, no judgment. Your role is to "
            "help Satyam understand concepts he's struggling with. Be patient, be clear, "
            "use analogies. Reference his actual proficiency and active lessons when "
            "relevant. If he asks about something he hasn't studied yet, point him to "
            "the right lesson. If he asks about something he already passed, acknowledge "
            "his existing knowledge and build on it.\n\n"
            "Satyam's current state:\n" + context + "\n\n"
            "Respond as Gyani. Keep responses focused, warm, and educational. "
            "Maximum 3 paragraphs unless he specifically asks for depth."
        )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OPENROUTER_URL,
                json={
                    "model": GYANI_MODEL,
                    "messages": messages,
                    "max_tokens": 1024,
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return JSONResponse({"error": f"API error {resp.status}: {text[:200]}"}, status_code=502)
                data = await resp.json()
                reply = data["choices"][0]["message"]["content"]
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    return {"reply": reply, "model": GYANI_MODEL}


# ── Curate (Goal → Tag Tree + Tasks) ──────────────────────────

@app.post("/api/gyani/curate")
async def gyani_curate(request: Request):
    """Decompose a learning goal into a tag tree + sub-tasks, persist to Atma.

    Request: {"goal": "I want to learn Spanish from scratch"}
    Response: {"tags": [...], "tasks": [...], "created": true}
    """
    body = await request.json()
    goal = body.get("goal", "").strip()
    if not goal or len(goal) > 500:
        return JSONResponse({"error": "goal required (max 500 chars)"}, status_code=400)

    api_key = _openrouter_key()
    if not api_key:
        return JSONResponse({"error": "no API key"}, status_code=503)

    # Build context: existing tag tree so we don't create duplicates
    url = _supabase_url()
    existing_tags = ""
    if url:
        try:
            con = psycopg2.connect(url, connect_timeout=8)
            cur = con.cursor()
            cur.execute("SELECT id, name, parent_tag_id, category FROM tags ORDER BY id")
            tag_rows = cur.fetchall()
            tag_map = {r[0]: {"name": r[1], "parent": r[2], "cat": r[3]} for r in tag_rows}
            existing_tags = "Existing tags (DO NOT recreate these — build ON TOP of them):\n"
            for tid, t in sorted(tag_map.items()):
                parent = f" (parent: {tag_map[t['parent']]['name']})" if t['parent'] and t['parent'] in tag_map else ""
                existing_tags += f"  #{tid} {t['name']} [{t['cat']}]{parent}\n"
            con.close()
        except Exception:
            existing_tags = ""

    prompt = (
        "You are Gyani, an educational professor who decomposes learning goals into "
        "structured curriculum trees. Given a student's goal, create:\n\n"
        "1. A TAG TREE — hierarchical knowledge categories (3-6 tags). Each tag must have:\n"
        "   - name: descriptive, domain-prefixed (e.g. 'Spanish — Vocabulary')\n"
        "   - parent_tag: null for root, exact child_of name for children\n"
        "   - category: 'mental' | 'physical' | 'financial'\n\n"
        "2. CONCRETE TASKS — individually examinable learning objectives (3-8 tasks). Each:\n"
        "   - title: specific, examinable via Socratic dialogue\n"
        "   - tag_name: which tag this task belongs to (must match a tag name)\n"
        "   - priority: 3 (standard), 4 (harder), 5 (the hardest prerequisite)\n"
        "   - description: 1-2 sentences about what the student will learn\n\n"
        "RULES:\n"
        "- NEVER create a tag with the same name as an existing one — build on top.\n"
        "- NEVER create a task called 'Go work out' (that already exists).\n"
        "- Tasks must be SOCRATICALLY EXAMINABLE — no 'write code', no 'build a project'.\n"
        "- Start from absolute basics. The student may know nothing about this domain.\n"
        "- Prioritize foundational concepts before advanced ones.\n"
        "- Tags form a tree: one root, children hang from it.\n"
        "- CRITICAL: The ROOT tag MUST have at least one task under it — otherwise the "
        "student can never gain proficiency on the root, and its children stay locked forever. "
        "That root task should be a gentle, conceptual introduction (e.g. 'Explain what makes "
        "X unique') that requires no prior knowledge.\n\n"
        + existing_tags + "\n\n"
        "Goal: " + goal + "\n\n"
        "Respond with ONLY valid JSON — no markdown, no explanation:\n"
        '{"tags": [{"name": "...", "parent_tag": null|"name", "category": "mental"}, ...],\n'
        ' "tasks": [{"title": "...", "tag_name": "...", "priority": 3, "description": "..."}, ...]}\n'
    )

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Curate a learning plan for: {goal}"},
    ]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                OPENROUTER_URL,
                json={"model": GYANI_MODEL, "messages": messages, "max_tokens": 4096},
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=90),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    return JSONResponse({"error": f"LLM error {resp.status}"}, status_code=502)
                data = await resp.json()
                raw = data["choices"][0]["message"]["content"]
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    # Parse the JSON (strip markdown fences, tolerate trailing truncation)
    m = re.search(r'\{[\s\S]*\}', raw)
    if not m:
        return JSONResponse({"error": "could not parse curation JSON", "raw": raw[:500]}, status_code=502)

    try:
        plan = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        # Attempt recovery: truncate to last complete task/tag boundary
        raw_trimmed = raw[:e.pos]
        # Close any open array
        if '"tasks"' in raw_trimmed and ']' not in raw_trimmed[raw_trimmed.rfind('"tasks"'):]:
            raw_trimmed += ']}'
        try:
            plan = json.loads(re.search(r'\{[\s\S]*\}', raw_trimmed).group(0))
        except Exception:
            return JSONResponse({"error": "invalid JSON from LLM", "raw": raw[:500]}, status_code=502)

    # Persist to Atma
    created_tags = []
    created_tasks = []
    created_errors = []

    if url:
        try:
            con = psycopg2.connect(url, connect_timeout=8)
            cur = con.cursor()

            # Get current max tag id
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM tags")
            next_tag_id = cur.fetchone()[0] + 1

            # Create tags (idempotent — skip if name already exists)
            tag_id_map = {}
            for t in plan.get("tags", []):
                name = t.get("name", "").strip()
                if not name: continue
                cur.execute("SELECT id FROM tags WHERE name = %s", (name,))
                existing = cur.fetchone()
                if existing:
                    tag_id_map[name] = existing[0]
                    continue

                parent_id = None
                parent_name = t.get("parent_tag")
                if parent_name and parent_name in tag_id_map:
                    parent_id = tag_id_map[parent_name]

                cat = t.get("category", "mental")
                cur.execute(
                    "INSERT INTO tags (id, name, parent_tag_id, category, required_proficiency, created_at) "
                    "VALUES (%s, %s, %s, %s, 3.0, NOW()) RETURNING id",
                    (next_tag_id, name, parent_id, cat))
                tag_id = cur.fetchone()[0]
                tag_id_map[name] = tag_id
                next_tag_id += 1
                created_tags.append({"id": tag_id, "name": name, "parent": parent_id, "category": cat})

            # Create tasks
            for tsk in plan.get("tasks", []):
                title = tsk.get("title", "").strip()
                tag_name = tsk.get("tag_name", "").strip()
                if not title or not tag_name: continue
                tag_id = tag_id_map.get(tag_name)
                if not tag_id:
                    created_errors.append(f"tag '{tag_name}' not found for task '{title}'")
                    continue

                pri = tsk.get("priority", 3)
                desc = tsk.get("description", "")
                cur.execute(
                    "INSERT INTO tasks (title, category, priority, description, assigned_to, created_by, is_active, created_at) "
                    "VALUES (%s, 'mental', %s, %s, 1, 1, TRUE, NOW()) RETURNING id",
                    (title, pri, desc))
                task_id = cur.fetchone()[0]
                cur.execute("INSERT INTO task_tags (task_id, tag_id) VALUES (%s, %s)", (task_id, tag_id))
                created_tasks.append({"id": task_id, "title": title, "tag_id": tag_id, "priority": pri})

            con.commit()
            con.close()
        except Exception as e:
            created_errors.append(str(e))

    # Trigger mindmap rebuild
    mindmap_path = APP_DIR / "mindmap" / "mindmap.py"
    if mindmap_path.exists():
        subprocess.run(["python3", str(mindmap_path)], capture_output=True, timeout=60)

    return {
        "goal": goal,
        "tags": created_tags,
        "tasks": created_tasks,
        "errors": created_errors,
        "total_tags": len(created_tags),
        "total_tasks": len(created_tasks),
    }


def mindmap_data():
    """Serve pre-generated graph.json."""
    if GRAPH_JSON.exists():
        return FileResponse(GRAPH_JSON)
    return JSONResponse({"error": "graph not generated"}, status_code=503)


# ── Submissions (Google Drive) ───────────────────────────────

GWS_SCRIPT = (Path.home() / ".hermes" / "profiles" / "gyani" / "skills" /
              "productivity" / "google-workspace" / "scripts" / "google_api.py")
GYANI_HOME = Path.home() / ".hermes" / "profiles" / "gyani"
SUBMISSIONS_FOLDER = "Gyani-Submissions"


def _gws(*args, timeout=30):
    """Run the Google Workspace CLI with Gyani's credentials."""
    if not GWS_SCRIPT.exists():
        return None
    env = dict(os.environ, HERMES_HOME=str(GYANI_HOME))
    try:
        r = subprocess.run(["python3", str(GWS_SCRIPT), *args],
                           capture_output=True, text=True,
                           timeout=timeout, env=env)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout) if r.stdout.strip() else None
    except Exception:
        return None


@app.get("/api/gyani/submissions")
def gyani_submissions():
    """Artifact submissions in the Gyani-Submissions Drive folder."""
    files = _gws("drive", "search", SUBMISSIONS_FOLDER, "--max", "1")
    folder_id = None
    if isinstance(files, list):
        for f in files:
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                folder_id = f.get("id")
                break
    listing = _gws("drive", "search", f"'{folder_id}' in parents and trashed=false",
                   "--raw-query", "--max", "25") if folder_id else None

    items = []
    for f in (listing or []):
        if f.get("mimeType") == "application/vnd.google-apps.folder":
            continue
        mime = f.get("mimeType", "")
        kind = ("image" if mime.startswith("image/")
                else "doc" if "document" in mime or "wordprocessing" in mime
                else "pdf" if "pdf" in mime
                else "sheet" if "spreadsheet" in mime
                else "file")
        items.append({
            "file_id": f.get("id"),
            "name": f.get("name"),
            "kind": kind,
            "mime": mime,
            "web_link": f.get("webViewLink"),
            "modified": f.get("modifiedTime"),
        })

    # Cross-reference the ledger for review status
    reviewed = {}
    url = _supabase_url()
    if url:
        try:
            con = psycopg2.connect(url, connect_timeout=8)
            cur = con.cursor()
            cur.execute(
                """SELECT task_id, submissions FROM verifications
                   WHERE submissions IS NOT NULL"""
            )
            for task_id, subs in cur.fetchall():
                for s in (subs or []):
                    if s.get("file_id"):
                        reviewed[s["file_id"]] = {
                            "task_id": task_id,
                            "assessment": s.get("assessment"),
                        }
            con.close()
        except Exception:
            pass

    for it in items:
        r = reviewed.get(it["file_id"])
        it["reviewed"] = bool(r and r.get("assessment"))
        it["task_id"] = r.get("task_id") if r else None
        it["assessment"] = r.get("assessment") if r else None

    return {
        "folder": SUBMISSIONS_FOLDER,
        "folder_id": folder_id,
        "folder_link": (f"https://drive.google.com/drive/folders/{folder_id}"
                        if folder_id else None),
        "submissions": items,
        "unreviewed": sum(1 for i in items if not i["reviewed"]),
    }


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