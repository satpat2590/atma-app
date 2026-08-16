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

import aiohttp
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

@router.post("/api/gyani/tutor")
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

@router.post("/api/gyani/curate")
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
                    "INSERT INTO tasks (title, category, priority, description, assigned_to, created_by, is_active, owner_type, routing_state, created_at) "
                    "VALUES (%s, 'mental', %s, %s, 1, 1, TRUE, 'satyam', 'ready', NOW()) RETURNING id",
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