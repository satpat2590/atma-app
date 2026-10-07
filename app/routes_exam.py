"""Stateful exam session routes — the interactive exam loop.

This is the surface that makes Gyani a *professor*, not a lecturer.  It ties
the engine's exam lifecycle to HTTP: start an exam, answer one question at a
time, get judged, advance through the four levels, and on pass/fail the ledger
and task state are finalized.

The permanent record is always the ``verifications`` ledger.  The in-memory
``_SESSIONS`` dict only caches the live level/strike counters between requests —
on a cache miss (process restart, browser refresh) the session is reconstructed
from the persisted transcript, so an in_progress exam is never orphaned.

Endpoints:
    POST /api/gyani/exam/start   {task_id} | {topic}  → opens exam, asks L1
    POST /api/gyani/exam/answer  {verification_id, answer} → judge, advance
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from engine import exam as e
from engine import llm
from engine.db import connect

router = APIRouter()

# verification_id → cached live session state (reconstructible on miss)
_SESSIONS: dict[int, dict] = {}


async def _json_body(request: Request) -> dict:
    """Parse the JSON body, returning a clean 400 on malformed input."""
    try:
        return await request.json()
    except Exception:
        return {"_malformed": True}


def _session(vid: int) -> dict | None:
    """Return the cached session, reconstructing from the ledger if absent."""
    s = _SESSIONS.get(vid)
    if s is not None:
        return s
    exam = e.reconstruct_exam(vid)
    if exam is None:
        return None
    s = {"exam": exam, "questions": [], "topic": "", "level": exam.level, "current": ""}
    _SESSIONS[vid] = s
    return s


def _terminal_state(vid: int) -> dict | None:
    """If an exam is already finished, describe its real state (for a friendly
    answer instead of a bare 404)."""
    with connect() as con:
        cur = con.cursor()
        cur.execute("SELECT state, quality, max_level_reached FROM verifications WHERE id = %s", (vid,))
        row = cur.fetchone()
    if not row or row[0] == "in_progress":
        return None
    return {"state": row[0], "quality": row[1], "level_reached": row[2]}


@router.post("/api/gyani/exam/start")
async def exam_start(request: Request):
    body = await _json_body(request)
    if body.get("_malformed"):
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)
    task_id = body.get("task_id")
    topic = (body.get("topic") or "").strip()

    if not task_id and not topic:
        return JSONResponse({"error": "task_id or topic required"}, status_code=400)

    tag_id = None
    if task_id:
        with connect() as con:
            cur = con.cursor()
            cur.execute("SELECT title FROM tasks WHERE id = %s", (task_id,))
            row = cur.fetchone()
            if not row:
                return JSONResponse({"error": f"task {task_id} not found"}, status_code=404)
            topic = topic or row[0]
            cur.execute("SELECT tag_id FROM task_tags WHERE task_id = %s LIMIT 1", (task_id,))
            trow = cur.fetchone()
            tag_id = trow[0] if trow else None

    try:
        questions = e.generate_questions(topic)
    except llm.LLMError as err:
        return JSONResponse({"error": str(err)}, status_code=502)

    vid = e.open_exam(task_id if task_id else None, tag_id=tag_id, questions=questions)
    if vid is None:
        return JSONResponse({"error": "failed to open exam"}, status_code=500)

    first = (questions[0].get("text") or questions[0].get("q") or "") if questions else ""
    _SESSIONS[vid] = {
        "exam": e.Exam(verification_id=vid, task_id=task_id or 0, tag_id=tag_id),
        "questions": questions,
        "topic": topic,
        "level": 1,
        "current": first,
    }

    return {
        "verification_id": vid,
        "topic": topic,
        "level": 1,
        "question": first,
        "total_levels": 4,
    }


@router.post("/api/gyani/exam/answer")
async def exam_answer(request: Request):
    body = await _json_body(request)
    if body.get("_malformed"):
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)
    vid = body.get("verification_id")
    answer = (body.get("answer") or "").strip()
    if vid is not None:
        try:
            vid = int(vid)
        except (TypeError, ValueError):
            return JSONResponse({"error": "verification_id must be an integer"}, status_code=400)

    if not answer:
        return JSONResponse({"error": "answer required"}, status_code=400)

    if vid is None:
        return JSONResponse({"error": "verification_id required"}, status_code=400)

    session = _session(vid)
    if session is None:
        # Friendlier than a bare 404: if the exam is finished, say so.
        term = _terminal_state(vid)
        if term:
            return {"action": "already_finished", **term}
        return JSONResponse({"error": "no active exam for this id (exams are one-shot; start a new one)"}, status_code=404)

    exam: e.Exam = session["exam"]
    level = session["level"]
    question = session["current"]

    try:
        verdict = e.judge_answer(level, question, answer)
    except llm.LLMError as err:
        return JSONResponse({"error": str(err)}, status_code=502)

    exchange = e.Exchange(
        level=level, question=question, answer=answer,
        judgment=verdict["judgment"], note=verdict["note"],
        source=verdict.get("source", "llm"),
    )
    action = e.judge(exam, exchange)
    e.append_transcript(vid, exchange)
    session["level"] = exam.level

    if action in ("pass", "fail"):
        quality = e.quality_for(exam)
        state = "passed" if action == "pass" else "failed"
        e.close_exam(vid, state=state, quality=quality,
                     max_level_reached=exam.level, notes=verdict["note"])
        if action == "pass" and exam.task_id:
            e.complete_task_on_pass(exam.task_id)
        _SESSIONS.pop(vid, None)
        resp: dict = {
            "action": action, "state": state, "quality": quality,
            "note": verdict["note"], "level_reached": exam.level,
        }
        if action == "pass" and exam.tag_id:
            resp["unlocked"] = e.unlocked_children(exam.tag_id)
        return resp

    if action == "probe":
        return {
            "action": "probe", "level": level, "question":
                "I noticed your answer was a bit too pat — can you explain that again "
                "in your own words, differently?",
            "note": verdict["note"],
        }

    # "next" or "retry": ask the next question at the current (post-judge) level.
    session["level"] = exam.level
    qmap = session["questions"]
    idx = exam.level - 1
    if 0 <= idx < len(qmap):
        session["current"] = qmap[idx].get("text") or qmap[idx].get("q") or ""
    else:
        session["current"] = "Tell me more — what's the underlying principle?"
    return {
        "action": action, "level": exam.level, "question": session["current"],
        "note": verdict["note"],
    }