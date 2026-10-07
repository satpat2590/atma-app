"""The exam lifecycle — teach → prove → unlock.  The soul of the system.

This is the piece that has never actually run (the ``verifications`` ledger is
empty).  It implements the exam protocol end-to-end as code:

   OPEN    — explicit trigger only ("ready" / "test me").  Never assumed.
   GENERATE— fresh questions at 4 levels, never the ones printed in the lesson.
   DIALOGUE— one question at a time; each exchange judged; transcript appended.
   CLOSE   — pass (complete task → children unlock) or fail (penalty) or
              abandoned (24h silence, no grade).

The grading is a *pure state machine* so it can be unit-tested with no DB and
no LLM (see tests/).  Persistence and AI sit at the edges, injected where it
matters.

Unlock is DERIVED, not stored: tag proficiency = AVG(quality) of passed exams
with that tag_id.  A pass raises proficiency, which unlocks children whose
``required_proficiency`` threshold the parent now meets (the "ring effect").
No explicit unlock write exists — the graph is always recomputed from the
ledger, which is what keeps it honest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from engine import llm
from engine import jev as jev_module
from engine.db import connect

# ── Pure grading state machine ──────────────────────────────────────────
LEVELS = (1, 2, 3, 4)  # identification → correction → generation → synthesis
MAX_LEVEL = 4
PASS_MIN_LEVEL = 4          # must reach synthesis to pass
FAIL_AFTER_STRIKES = 2      # two failures at the SAME level fail the exam
MAX_QUALITY = 5


@dataclass
class Exchange:
    level: int
    question: str
    answer: str
    judgment: str          # "pass" | "fail" | "probe"
    note: str = ""         # what held / what cracked (for the transcript)
    source: str = "llm"    # "jev" when Jev classified, else "llm" (the deepseek judge)


@dataclass
class Exam:
    verification_id: int | None = None
    task_id: int = 0
    tag_id: int | None = None
    level: int = 1
    strikes: int = 0
    transcript: list[Exchange] = field(default_factory=list)


def judge(exam: Exam, exchange: Exchange) -> str:
    """Advance the machine for one judged exchange; returns next action.

    Returns one of:
      "next"  — ask the next question at the NEXT level
      "retry" — a failure, but not two; ask again at the SAME level
      "probe" — fluency suspect; ask them to explain it differently (same level)
      "pass"  — reached synthesis at the top level
      "fail"  — two failures at the same level

    Two failures must land at the SAME level (the protocol's anti-cheat), so a
    fail never advances the level — it yields "retry" until the strike count
    is exhausted at that level.
    """
    exam.transcript.append(exchange)
    if exchange.judgment == "fail":
        exam.strikes += 1
        if exam.strikes >= FAIL_AFTER_STRIKES:
            return "fail"
        return "retry"

    # A pass or probe resets the strike count at this level.
    exam.strikes = 0
    if exchange.judgment == "probe":
        return "probe"

    # exchange.judgment == "pass"
    if exam.level >= MAX_LEVEL:
        return "pass"
    exam.level += 1
    return "next"


def quality_for(exam: Exam) -> int:
    """Map a finished exam to a 1–5 quality score (mechanical, not discretionary)."""
    passed = sum(1 for e in exam.transcript if e.judgment == "pass")
    total = len(exam.transcript)
    if total == 0:
        return 1
    ratio = passed / total
    if ratio >= 0.9:
        return 5
    if ratio >= 0.75:
        return 4
    if ratio >= 0.6:
        return 3
    if ratio >= 0.4:
        return 2
    return 1


# ── Question generation (AI edge) ───────────────────────────────────────
def generate_questions(topic: str, *, api_key: str | None = None) -> list[dict]:
    """Fresh Socratic questions at 4 levels for a task.  Never the printed ones."""
    system = (
        "You are Gyani, a Socratic examiner. Generate FOUR questions to verify "
        "deep understanding of a single concept, one per level:\n"
        "L1 identification — recognize/recall the concept in isolation.\n"
        "L2 correction — spot and fix a wrong application of it.\n"
        "L3 generation — produce a correct novel example or solution.\n"
        "L4 synthesis — connect it to another domain or reason from first principles.\n\n"
        "These must be NEW questions, not a restatement of any lesson text. "
        "Make each independently answerable without the others. "
        "Respond with ONLY valid JSON: {\"questions\": [{\"level\":1,\"text\":\"...\"}, ...]}"
    )
    text = llm.complete(system, f"Topic: {topic}", api_key=api_key, max_tokens=4096)
    # Tolerate a markdown fence or preamble, AND mid-response truncation.
    start = text.find("{")
    end = text.rfind("}")
    try:
        if start == -1 or end == -1 or end <= start:
            raise llm.LLMError(f"could not parse question JSON from model output: {text[:200]!r}")
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        # Truncation recovery: the model often cuts JSON mid-response (the
        # atma-knowledge-graph skill warns of this) — retry once with an
        # explicit "complete the JSON" hint at a higher token budget.
        hint = "Your previous response was truncated. Respond with COMPLETE, valid JSON only."
        text = llm.complete(system, f"Topic: {topic}\n\n{hint}", api_key=api_key, max_tokens=4096)
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise llm.LLMError(f"could not parse question JSON after retry: {text[:200]!r}")
        data = json.loads(text[start : end + 1])
    questions = data.get("questions", [])
    if not questions:
        raise llm.LLMError("model returned empty question set")
    return questions[:MAX_LEVEL]


def judge_answer(level: int, question: str, answer: str, *, api_key: str | None = None) -> dict:
    """Judge one answer at a given level.  Returns the parsed verdict dict.

    Fast path: Jev (TypeSafe System One) classifies the answer as a typed
    `choice` over {pass, probe, fail} + a `noul` "genuine understanding" probe.
    When Jev returns a CONFIDENT verdict it is used directly — no reasoning LLM
    call, no JSON parsing.  On disabled / outage / low-confidence, it falls
    back to the deepseek JSON judge byte-identically (fail-open).

    The returned dict carries `source` = "jev" | "llm" so the transcript can
    A/B which grader produced each judgment.
    """
    # Fast path — Jev is advisory but never mutates exam state; its failure
    # must degrade to the deterministic fallback, not propagate.
    try:
        verdict = jev_module.judge_answer_verdict(level, question, answer)
    except Exception:  # noqa: BLE001 — fail-open: Jev can never break grading
        verdict = None
    if verdict is not None:
        return verdict

    # Deepseek fallback (the pre-Jev path, unchanged).
    system = (
        "You are Gyani, a Socratic examiner. Judge a single exam answer.\n\n"
        "The student is being tested at LEVEL "
        f"{level} of 4 (1=identification, 2=correction, 3=generation, 4=synthesis).\n"
        "Rules:\n"
        '  judgment = "pass" if the answer is substantially correct for this level.\n'
        '  judgment = "probe" if it is suspiciously fluent/memorized OR you need to\n'
        '    verify they can rephrase it (ask them to explain differently).\n'
        '  judgment = "fail" if it is wrong or off-topic.\n'
        "Be strict but fair; a wrong answer at level 1 is a fail, not a probe.\n\n"
        'Respond with ONLY valid JSON: {"judgment": "pass|probe|fail", "note": "<1 short sentence on what held or cracked>"}'
    )
    text = llm.complete(system, f"Question: {question}\n\nStudent answer: {answer}", api_key=api_key)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise llm.LLMError(f"could not parse judgment JSON: {text[:200]!r}")
    try:
        verdict = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise llm.LLMError(f"malformed judgment JSON: {e}") from e
    judgment = verdict.get("judgment", "fail")
    if judgment not in ("pass", "probe", "fail"):
        judgment = "fail"
    return {"judgment": judgment, "note": verdict.get("note", ""), "source": "llm"}


# ── Persistence (DB edge) ──────────────────────────────────────────────
def open_exam(task_id, *, tag_id: int | None = None, lesson_path: str | None = None,
              questions: list[dict] | None = None, created_by: str = "gyani") -> int | None:
    """Create an in_progress verification row.  Returns the verification id.

    ``task_id`` may be None for a free-topic exam (no backing task).
    """
    now = datetime.now(timezone.utc)
    with connect() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO verifications (task_id, tag_id, lesson_path, state, started_at, "
            "questions, created_by) VALUES (%s, %s, %s, 'in_progress', %s, %s, %s) "
            "RETURNING id",
            (task_id, tag_id, lesson_path, now, json.dumps(questions or []), created_by),
        )
        row = cur.fetchone()
        vid = row[0] if row else None
        con.commit()
        return vid


def append_transcript(verification_id: int, exchange: Exchange) -> None:
    """Append one judged exchange to the running exam's transcript JSONB."""
    with connect() as con:
        cur = con.cursor()
        cur.execute("SELECT transcript FROM verifications WHERE id = %s", (verification_id,))
        row = cur.fetchone()
        transcript = (row[0] if row and row[0] else [])
        if isinstance(transcript, str):
            transcript = json.loads(transcript or "[]")
        transcript.append({
            "level": exchange.level,
            "q": exchange.question,
            "a": exchange.answer,
            "judgment": exchange.judgment,
            "note": exchange.note,
            "source": exchange.source,
        })
        cur.execute(
            "UPDATE verifications SET transcript = %s WHERE id = %s",
            (json.dumps(transcript), verification_id),
        )
        con.commit()


def close_exam(verification_id: int, *, state: str, quality: int | None = None,
               max_level_reached: int | None = None, notes: str = "") -> dict:
    """Finalize a verification row.  ``state`` is 'passed' | 'failed' | 'abandoned'."""
    if state == "passed" and max_level_reached is None:
        max_level_reached = MAX_LEVEL
    now = datetime.now(timezone.utc)
    task_id = None
    with connect() as con:
        cur = con.cursor()
        cur.execute(
            "UPDATE verifications SET state = %s, quality = %s, max_level_reached = %s, "
            "proficiency_notes = %s, completed_at = %s WHERE id = %s RETURNING task_id",
            (state, quality, max_level_reached, notes, now, verification_id),
        )
        row = cur.fetchone()
        if row:
            task_id = row[0]
        con.commit()
    return {"verification_id": verification_id, "state": state, "task_id": task_id}


def tag_proficiency(tag_id: int) -> float:
    """AVG(quality) of passed exams for a tag.  Drives the unlock ring effect."""
    with connect() as con:
        cur = con.cursor()
        cur.execute(
            "SELECT AVG(quality) FROM verifications "
            "WHERE tag_id = %s AND state = 'passed'",
            (tag_id,),
        )
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0


def unlocked_children(parent_tag_id: int) -> list[dict[str, Any]]:
    """Children of a tag whose parent proficiency now meets their threshold."""
    prof = tag_proficiency(parent_tag_id)
    with connect() as con:
        cur = con.cursor()
        cur.execute(
            "SELECT id, name, required_proficiency FROM tags "
            "WHERE parent_tag_id = %s AND required_proficiency <= %s ORDER BY id",
            (parent_tag_id, prof),
        )
        return [
            {"id": r[0], "name": r[1], "required_proficiency": r[2]}
            for r in cur.fetchall()
        ]


def complete_task_on_pass(task_id: int) -> None:
    """On a pass, mark the task complete (children unlock via derived proficiency)."""
    with connect() as con:
        cur = con.cursor()
        cur.execute("UPDATE tasks SET is_active = FALSE WHERE id = %s", (task_id,))
        con.commit()


def reconstruct_exam(verification_id: int) -> Exam | None:
    """Rebuild an Exam's live state from a persisted transcript.

    The transcript is the source of truth: replaying its exchanges through the
    pure ``judge`` machine deterministically restores level + strikes.  Lets a
    process restart or browser refresh resume an in_progress exam instead of
    orphaning it.
    """
    with connect() as con:
        cur = con.cursor()
        cur.execute(
            "SELECT task_id, tag_id, state, transcript, questions FROM verifications WHERE id = %s",
            (verification_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        task_id, tag_id, state, transcript, _questions = row
        if state != "in_progress":
            return None
    if isinstance(transcript, str):
        transcript = json.loads(transcript or "[]")

    exam = Exam(verification_id=verification_id, task_id=task_id or 0, tag_id=tag_id)
    for entry in transcript:
        ex = Exchange(
            level=entry.get("level", 1),
            question=entry.get("q", ""),
            answer=entry.get("a", ""),
            judgment=entry.get("judgment", "pass"),
            note=entry.get("note", ""),
        )
        judge(exam, ex)
    return exam
