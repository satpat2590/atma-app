"""Task Ownership Classification (TOC) — Gyani's triage endpoint.

When Satyam creates a task, this endpoint answers the question that sits at
the heart of the Atma/Gyani design:

    "Can Satyam actually do this task himself, or does it route to an agent?"

It maps a task to a *skill signature* (the tags + proficiency levels it
requires), diffs that against Satyam's ledger proficiency, and produces a gap
vector. The result is a routing recommendation — 'ready' (Satyam can do it),
'learn' (blocked on prerequisites he can close), or 'delegate' (an agent who
already has the skill takes it).

No growth points ever accrue to Satyam for a delegated task: the growth views
already filter `WHERE assigned_to = 1`, so delegation simply points
`assigned_to` at the agent and the task drops out of his ledger naturally.
"""

from app.db import _supabase_url
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
import psycopg2, json, re
from pathlib import Path
import aiohttp

router = APIRouter()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GYANI_MODEL = "deepseek/deepseek-v4-pro"

# Canonical agent catalog — who in Omni can already do what. This is the
# delegation target list the LLM picks from. Paisa = quant/risk (the Galadriel
# example), Argus = trading/PM, Satya = philosophy/research, Veltiosi = knowledge.
AGENT_CATALOG = [
    {"id": 2, "name": "Argus",    "domains": "portfolio management, trading, trade execution, market microstructure, strategy deployment, Edoras operations"},
    {"id": 3, "name": "Veltiosi", "domains": "knowledge management, curation, source verification, documentation, fact-checking"},
    {"id": 4, "name": "Satya",    "domains": "natural philosophy, reasoning, research synthesis, conceptual and philosophical analysis"},
    {"id": 5, "name": "Paisa",    "domains": "quantitative research, risk metrics, statistical derivations, stochastic modeling, machine learning, backtesting"},
]

# Do not route learning tasks to these: Gyani is the examiner, never the worker.
# (Satya is listed above only for completeness of the research domain.)
NON_DELEGATABLE = {"Gyani"}


def _openrouter_key() -> str:
    """Read OpenRouter key from Gyani's profile .env (same one Gyani uses)."""
    env_path = Path.home() / ".hermes" / "profiles" / "gyani" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip()
    import os
    return os.environ.get("OPENROUTER_API_KEY", "")


def _load_tag_tree():
    """Return {name: {id, parent, category}} and {id: name} for the full tree."""
    url = _supabase_url()
    if not url:
        return {}, {}
    con = psycopg2.connect(url, connect_timeout=8)
    cur = con.cursor()
    cur.execute("SELECT id, name, parent_tag_id, category FROM tags ORDER BY id")
    rows = cur.fetchall()
    con.close()
    by_name = {r[1]: {"id": r[0], "parent": r[2], "category": r[3]} for r in rows}
    by_id = {r[0]: r[1] for r in rows}
    return by_name, by_id


def _load_proficiency():
    """Return {tag_id: proficiency} from passed exams in the verifications ledger."""
    url = _supabase_url()
    if not url:
        return {}
    con = psycopg2.connect(url, connect_timeout=8)
    cur = con.cursor()
    cur.execute(
        "SELECT tag_id, ROUND(AVG(quality)::numeric, 2) FROM verifications "
        "WHERE state = 'passed' AND tag_id IS NOT NULL GROUP BY tag_id"
    )
    prof = {r[0]: float(r[1]) for r in cur.fetchall()}
    con.close()
    return prof


def _agent_by_name(name):
    if not name:
        return None
    for a in AGENT_CATALOG:
        if a["name"].lower() == name.strip().lower():
            return a
    return None


def _ensure_tag(con, cur, name, category_hint):
    """Resolve a tag name to an id, creating a root-level tag if unknown.

    The LLM maps tasks to existing tags, but it may legitimately name a tag
    that doesn't exist yet (e.g. a brand-new domain). Auto-create it as a
    root so the skill signature is always persistable. Idempotent on name.
    """
    cur.execute("SELECT id FROM tags WHERE name = %s", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cat = category_hint if category_hint in ("mental", "physical", "financial") else "mental"
    cur.execute(
        "INSERT INTO tags (name, parent_tag_id, category, required_proficiency, created_at) "
        "VALUES (%s, NULL, %s, 3.0, NOW()) RETURNING id",
        (name, cat),
    )
    return cur.fetchone()[0]


async def _llm_signature(api_key, subject_text, tag_tree_lines):
    """Ask the LLM to map a task/goal to a skill signature + suggested agent."""
    prompt = (
        "You are Gyani, an educational professor and triage engine. Given a task "
        "Satyam wants to accomplish, produce a SKILL SIGNATURE: the knowledge "
        "domains (tags) the task requires, each with a required proficiency level "
        "from 1.0 (basic familiarity) to 5.0 (could teach it).\n\n"
        "Available tags (use these EXACT names; do not invent a tag that already "
        "exists, but you MAY name a new tag if the task needs a domain absent from "
        "the list):\n" + tag_tree_lines + "\n\n"
        "Omni agents who can already do work (for the suggested_agent field, pick "
        "the ONE best match, or null if it's genuinely a learning task for Satyam):\n"
        + "\n".join(f"- {a['name']}: {a['domains']}" for a in AGENT_CATALOG) + "\n\n"
        "Rules:\n"
        "- signature: 1-6 entries. Prefer existing tag names. required is 1.0-5.0.\n"
        "- suggested_agent: a catalog name or null. Never 'Gyani' (the examiner).\n"
        "- rationale: one sentence.\n\n"
        "Task: " + subject_text + "\n\n"
        'Respond with ONLY valid JSON:\n'
        '{"signature": [{"tag_name": "...", "required": 3.0}, ...], '
        '"suggested_agent": "Paisa"|null, "rationale": "..."}\n'
    )
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": f"Triage this task: {subject_text}"},
    ]
    async with aiohttp.ClientSession() as session:
        async with session.post(
            OPENROUTER_URL,
            json={"model": GYANI_MODEL, "messages": messages, "max_tokens": 2048},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                return None, f"LLM error {resp.status}: {text[:200]}"
            data = await resp.json()
            raw = data["choices"][0]["message"]["content"]

    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None, f"could not parse JSON: {raw[:300]}"
    try:
        return json.loads(m.group(0)), None
    except json.JSONDecodeError:
        return None, f"invalid JSON: {raw[:300]}"


@router.post("/api/gyani/triage")
async def gyani_triage(request: Request):
    """Classify a task: can Satyam do it, or does it route to an agent?

    Request:
      { task_id: int }                      — triage an existing task, OR
      { goal: "..." }                       — triage a free-text goal
      decision: "learn" | "delegate"        — optional; finalize routing
      agent: "Paisa"                        — required when decision="delegate"

    Response: signature, gap vector, recommendation, and (if a decision was
    given) the applied routing state.
    """
    body = await request.json()
    task_id = body.get("task_id")
    goal = (body.get("goal") or "").strip()
    decision = (body.get("decision") or "").strip().lower()
    agent_name = (body.get("agent") or "").strip()

    if not task_id and not goal:
        return JSONResponse({"error": "task_id or goal required"}, status_code=400)
    if decision and decision not in ("learn", "delegate"):
        return JSONResponse({"error": "decision must be 'learn' or 'delegate'"}, status_code=400)
    if decision == "delegate" and not agent_name:
        return JSONResponse({"error": "agent required when delegating"}, status_code=400)

    api_key = _openrouter_key()
    if not api_key:
        return JSONResponse({"error": "no API key"}, status_code=503)

    url = _supabase_url()
    if not url:
        return JSONResponse({"error": "no supabase"}, status_code=503)

    # ── Resolve the subject ──────────────────────────────────────────────
    con = psycopg2.connect(url, connect_timeout=8)
    cur = con.cursor()

    if task_id:
        cur.execute("SELECT id, title, description, category FROM tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
        if not row:
            con.close()
            return JSONResponse({"error": f"task {task_id} not found"}, status_code=404)
        tid, title, desc, category = row
        subject_text = f"{title}. {desc or ''}".strip()
    else:
        # Free-text goal: materialize it as a task first so triage persists cleanly.
        category = body.get("category", "mental")
        cur.execute(
            "INSERT INTO tasks (title, category, priority, description, assigned_to, created_by, is_active, created_at) "
            "VALUES (%s, %s, 3, %s, 1, 1, TRUE, NOW()) RETURNING id",
            (goal[:200], category, goal[:500]),
        )
        _row = cur.fetchone()
        if not _row:
            con.rollback()
            con.close()
            return JSONResponse({"error": "failed to materialize task"}, status_code=500)
        tid = _row[0]
        title, desc, category = goal[:200], "", category
        subject_text = goal
    con.commit()

    # ── Build LLM context from the live tree + proficiency ──────────────
    tag_by_name, _ = _load_tag_tree()
    tag_tree_lines = "\n".join(
        f"- {name} [{meta['category']}]" for name, meta in sorted(tag_by_name.items())
    )

    parsed, err = await _llm_signature(api_key, subject_text, tag_tree_lines)
    if parsed is None:
        con.close()
        return JSONResponse({"error": err}, status_code=502)

    signature = parsed.get("signature", [])
    suggested_agent = parsed.get("suggested_agent") or None
    rationale = parsed.get("rationale", "")

    proficiency = _load_proficiency()

    # ── Resolve signature to tag ids + compute the gap vector ───────────
    resolved = []
    for entry in signature:
        name = (entry.get("tag_name") or "").strip()
        if not name:
            continue
        required = float(entry.get("required", 3.0))
        tag_id = _ensure_tag(con, cur, name, category)
        current = proficiency.get(tag_id, 0.0)
        gap = max(0.0, round(required - current, 2))
        resolved.append({
            "tag_name": name, "tag_id": tag_id, "required": required,
            "current": current, "gap": gap,
        })
        cur.execute(
            "INSERT INTO task_skill_requirements (task_id, tag_id, required_proficiency) "
            "VALUES (%s, %s, %s) ON CONFLICT (task_id, tag_id) DO UPDATE "
            "SET required_proficiency = EXCLUDED.required_proficiency",
            (tid, tag_id, required),
        )

    gap_tags = [r for r in resolved if r["gap"] > 0]
    has_gap = len(gap_tags) > 0

    # ── Recommendation ───────────────────────────────────────────────────
    agent_meta = _agent_by_name(suggested_agent)
    if not has_gap:
        recommendation = "ready"
    elif agent_meta:
        recommendation = "delegate"
    else:
        recommendation = "learn"

    # ── Apply a decision if one was given ────────────────────────────────
    applied = None
    if decision == "delegate":
        meta = _agent_by_name(agent_name)
        if not meta:
            con.rollback()
            con.close()
            return JSONResponse({"error": f"unknown agent '{agent_name}'"}, status_code=400)
        cur.execute(
            "UPDATE tasks SET assigned_to = %s, owner_type = 'agent', routing_state = 'delegated' WHERE id = %s",
            (meta["id"], tid),
        )
        applied = {"routing_state": "delegated", "assigned_to": meta["name"], "agent_id": meta["id"]}
    elif decision == "learn":
        cur.execute(
            "UPDATE tasks SET routing_state = %s WHERE id = %s",
            ("ready" if not has_gap else "learning_gap", tid),
        )
        applied = {"routing_state": "ready" if not has_gap else "learning_gap"}
    else:
        # No decision yet: record the preliminary state.
        cur.execute(
            "UPDATE tasks SET routing_state = %s WHERE id = %s",
            ("ready" if not has_gap else "triage_pending", tid),
        )

    con.commit()
    con.close()

    return {
        "task_id": tid,
        "title": title,
        "subject": subject_text[:300],
        "signature": resolved,
        "gap_tags": [g["tag_name"] for g in gap_tags],
        "has_gap": has_gap,
        "recommendation": recommendation,
        "suggested_agent": suggested_agent if agent_meta else None,
        "rationale": rationale,
        "applied": applied,
        "note": (
            "Satyam owns this task and may grow his mindmap by completing it."
            if not has_gap
            else "Gap detected. Reply with decision='learn' to build the prerequisite "
                 "path, or decision='delegate' + agent to route it to an agent (no points for Satyam)."
        ),
    }
