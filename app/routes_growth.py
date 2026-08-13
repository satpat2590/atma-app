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

@router.get("/api/growth")
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