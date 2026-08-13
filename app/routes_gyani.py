from app.db import get_db, _supabase_url
import psycopg2
from app.helpers import _parse_lesson_title, _score_color, build_lesson_index
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, FileResponse
import json, os, re, sqlite3, subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

router = APIRouter()
OBSIDIAN_CURRICULUM = Path.home() / "Obsidian" / "Curriculum"
OBSIDIAN_DAILY = Path.home() / "Obsidian" / "15-Daily-Notes"
WHOOP_DB = Path.home() / "whoop-sync" / "health.db"
EDORAS_REPO = Path.home() / "edoras"
ALIGNMENT_DIR = OBSIDIAN_CURRICULUM / "alignment-reports"
GRAPH_JSON = Path(__file__).parent.parent / "static" / "graph.json"
STATIC = Path(__file__).parent.parent / "static"


@router.get("/api/gyani/lessons")
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

@router.get("/api/gyani/lesson")
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

@router.get("/api/gyani/verifications")
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

@router.get("/api/gyani/habits")
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

@router.get("/api/gyani/alignment")
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

@router.get("/api/gyani/report-card")
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