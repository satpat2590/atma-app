"""Jev (TypeSafe System One) — calibrated-decision client for the exam engine.

Jev does NOT generate text.  You send a ``state`` (text/JSON) plus typed
``questions``, and it returns calibrated probabilities: a ``choice`` (1-of-N
+ full PMF + confidence), a ``score`` (rubric position + distribution + confidence),
or a ``noul`` (P(yes) 0..1).  ~$0.042/M input tokens (output free), ~300–850ms
through OpenRouter from NYC — cheap and fast enough to run on *every* exam answer,
with the reasoning model (deepseek) as the escalation path.

In this engine Jev is the answer **classifier**: a typed ``choice`` over
{pass, probe, fail} + an independent ``noul`` "genuine understanding" probe
replaces the fragile "ask a text LLM for JSON, then brace-parse it" step in
``exam.judge_answer``.  The point of the second probe is anti-cheat: a terse,
textbook-correct answer reads "pass" on the choice but low on "genuine", so we
downgrade to ``probe`` (explain it differently) instead of advancing.

FAIL-OPEN: this module NEVER raises.  On any failure (disabled, no key,
transport error, malformed payload) ``decide`` returns ``None`` and the caller
falls back to the deepseek JSON judge byte-identically.  ``judge_answer_verdict``
returns ``None`` *also* on low confidence — Jev only auto-acts when it is sure;
an ambiguous answer escalates to the reasoning model.  Jev is advisory to the
exam's ``judge`` state machine, which is untouched and always the sole arbiter
of strike/level/unlock.

Only the OpenRouter decisions endpoint is wired today (Gyani's profile carries
an OpenRouter key, no native TypeSafe key).  A native-primary + OpenRouter-
fallback chain mirrors Edoras's ``JevClient`` and is a small diff if a
``TYPESAFE_API_KEY`` is ever added.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from engine import llm  # reuse the shared OpenRouter key resolution

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
JEV_MODEL = "~typesafe/jev-latest"
_DEFAULT_TIMEOUT_S = 15.0

# Gyani-specific env overrides (mirror Edoras's EDORAS_JEV_* prefix).
_ENV_PREFIX = "GYANI_JEV_"

# Choice-confidence floor for trusting Jev's verdict.  At or above this the
# distribution is concentrated enough to act on without escalation.  Below it
# (or disabled/down) the deepseek JSON judge handles the answer.
CONFIDENCE_FLOOR = 0.8


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def jev_enabled() -> bool:
    """True unless disabled by flag (enabled by default when a key exists)."""
    return _env_bool(_ENV_PREFIX + "ENABLED", True)


def jev_key() -> str | None:
    """Jev API key — GYANI_JEV_API_KEY override, else the shared OpenRouter key."""
    return os.environ.get(_ENV_PREFIX + "API_KEY") or llm.openrouter_key()


def _confidence(answer: dict[str, Any]) -> float:
    """Normalized 0..1 confidence; 0.0 on missing/malformed."""
    raw = answer.get("confidence")
    if raw is None:
        return 0.0
    try:
        c = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if c < 0.0 or c > 1.0:
        return 0.0
    return c


def decide(
    state: Any,
    questions: dict[str, Any],
    *,
    api_key: str | None = None,
    timeout: float | None = None,
) -> dict[str, Any] | None:
    """Evaluate ``questions`` against ``state``; return the ``answers`` dict.

    Returns ``None`` (never raises) when disabled, no key, or the call fails
    for any reason.  ``api_key`` may be injected for tests.
    """
    if not jev_enabled():
        return None
    key = api_key or jev_key()
    if not key:
        return None

    body: dict = {"model": JEV_MODEL, "state": state, "questions": questions}
    t = timeout or _DEFAULT_TIMEOUT_S

    try:
        # Two attempts: the first decision call can be cold (~800ms+).
        for attempt in range(2):
            try:
                resp = httpx.post(
                    DECISIONS_URL,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    timeout=t,
                )
                if resp.status_code != 200:
                    return None
                data = resp.json()
                answers = data.get("answers") if isinstance(data, dict) else None
                if isinstance(answers, dict) and answers:
                    return answers
                return None
            except (httpx.HTTPError, ValueError):
                if attempt == 0:
                    time.sleep(0.5)
                    continue
                return None
    except Exception:  # noqa: BLE001 — fail-open, never propagate
        return None
    return None


# ── Answer-verdict classifier ────────────────────────────────────────────
def _verdict_questions() -> dict[str, Any]:
    """The two parallel judgments that classify a single exam answer."""
    return {
        "verdict": {
            "type": "choice",
            "instructions": (
                "You are grading a single exam answer. Judge whether it is "
                "correct for the level described in the state."
            ),
            "criteria": {
                "pass": "substantially correct for this level",
                "probe": (
                    "correct but suspiciously fluent/memorized, or needs to be "
                    "rephrased to confirm genuine understanding"
                ),
                "fail": "wrong or off-topic",
            },
        },
        "genuine": {
            "type": "noul",
            "instructions": (
                "Does this answer show the student's OWN understanding rather "
                "than a memorized or canned response?"
            ),
        },
    }


def _verdict_state(level: int, question: str, answer: str) -> str:
    return (
        f"Level {level} of 4. Question: {question}\nStudent answer: {answer}"
    )


def judge_answer_verdict(
    level: int, question: str, answer: str, *, api_key: str | None = None
) -> dict[str, Any] | None:
    """Classify one exam answer with Jev; return a verdict dict or None.

    Returns ``{"judgment": "pass"|"probe"|"fail", "note": str, "source": "jev",
    "p_pass": float}`` — or ``None`` to signal "escalate to the reasoning model"
    (disabled, failed, or confidence below the floor).

    The ``genuine`` noul is the anti-cheat cross-check: a confident "pass" that
    reads as non-genuine is downgraded to "probe" (rephrase it) rather than
    counted as a clean advance.
    """
    answers = decide(
        _verdict_state(level, question, answer),
        _verdict_questions(),
        api_key=api_key,
    )
    if not answers:
        return None

    v = answers.get("verdict")
    if not isinstance(v, dict):
        return None
    choice = v.get("choice")
    if choice not in ("pass", "probe", "fail"):
        return None

    conf = _confidence(v)
    if conf < CONFIDENCE_FLOOR:
        return None  # ambiguous — escalate

    judgment = choice
    genuine = None
    g = answers.get("genuine")
    if isinstance(g, dict):
        try:
            genuine = float(g.get("noul"))
        except (TypeError, ValueError):
            genuine = None

    # Anti-cheat: confident "pass" but the model thinks it's canned → probe.
    if judgment == "pass" and genuine is not None and genuine < 0.5:
        judgment = "probe"
        note = _synthesize_downgrade_note(v, conf, genuine)
    else:
        note = _synthesize_note(judgment, v, conf, genuine)
    p_pass = 0.0
    probs = v.get("probabilities")
    if isinstance(probs, dict):
        p = probs.get("pass")
        if p is not None:
            try:
                p_pass = float(p)
            except (TypeError, ValueError):
                p_pass = 0.0
    return {
        "judgment": judgment,
        "note": note,
        "source": "jev",
        "p_pass": p_pass,
    }


def _synthesize_note(
    judgment: str, answer: dict[str, Any], confidence: float, genuine: float | None
) -> str:
    """Jev returns no prose — synthesize an auditable note from the numbers."""
    prob = answer.get("probabilities", {}) if isinstance(answer.get("probabilities"), dict) else {}
    p_choice = prob.get(judgment)
    parts = [f"jev:{judgment}"]
    if isinstance(p_choice, (int, float)):
        parts.append(f"P={p_choice:.2f}")
    parts.append(f"conf={confidence:.2f}")
    if genuine is not None:
        parts.append(f"genuine={genuine:.2f}")
    return " ".join(parts)


def _synthesize_downgrade_note(
    answer: dict[str, Any], confidence: float, genuine: float | None
) -> str:
    """Note for the anti-cheat downgrade: the answer reads correct but canned.

    The real signal is P(pass) high + genuine low — not the probe probability,
    which is near zero because Jev never *picked* probe; we forced it.
    """
    prob = answer.get("probabilities", {}) if isinstance(answer.get("probabilities"), dict) else {}
    p_pass = prob.get("pass")
    parts = ["jev:probe", "(downgraded: correct-sounding but not genuine)"]
    if isinstance(p_pass, (int, float)):
        parts.append(f"P(pass)={p_pass:.2f}")
    parts.append(f"genuine={genuine:.2f}" if genuine is not None else "genuine=?")
    return " ".join(parts)