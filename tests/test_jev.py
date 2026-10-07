"""Tests for engine/jev.py — the Jev answer classifier.

Covers the fail-open contract (no key / disabled / HTTP error / malformed /
low-confidence → None), the confident fast path, and the anti-cheat downgrade
(confident "pass" + low "genuine" → "probe").  No live network: httpx is mocked.

The invariant under test is the integration rule: Jev is ADVISORY — it must
return None (→ escalate to the deepseek judge) on anything ambiguous, and its
failure must never leak into the exam's grading state machine.
"""

from unittest import mock

import pytest

from engine import jev


# ── decide(): the transport/fail-open layer ──────────────────────────────
class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    def json(self):
        return self._payload

    @property
    def status_code(self):
        return self._status


def test_decide_returns_none_when_disabled():
    with mock.patch.object(jev, "jev_enabled", return_value=False):
        assert jev.decide("s", {"q": {}}) is None


def test_decide_returns_none_without_key():
    with mock.patch.object(jev, "jev_enabled", return_value=True), \
         mock.patch.object(jev, "jev_key", return_value=None):
        assert jev.decide("s", {"q": {}}) is None


def test_decide_returns_answers_dict_on_success():
    payload = {"answers": {"x": {"type": "noul", "noul": 0.9}}}
    with mock.patch.object(jev, "jev_enabled", return_value=True), \
         mock.patch.object(jev, "jev_key", return_value="k"), \
         mock.patch.object(jev.httpx, "post", return_value=_Resp(payload)):
        assert jev.decide("s", {"q": {}}) == {"x": {"type": "noul", "noul": 0.9}}


def test_decide_returns_none_on_http_error():
    with mock.patch.object(jev, "jev_enabled", return_value=True), \
         mock.patch.object(jev, "jev_key", return_value="k"), \
         mock.patch.object(jev.httpx, "post", return_value=_Resp({}, status=500)), \
         mock.patch.object(jev.time, "sleep", return_value=None):
        assert jev.decide("s", {"q": {}}) is None


def test_decide_returns_none_on_transport_error():
    with mock.patch.object(jev, "jev_enabled", return_value=True), \
         mock.patch.object(jev, "jev_key", return_value="k"), \
         mock.patch.object(jev.httpx, "post", side_effect=jev.httpx.ConnectError("boom")), \
         mock.patch.object(jev.time, "sleep", return_value=None):
        assert jev.decide("s", {"q": {}}) is None


def test_decide_returns_none_on_malformed_body():
    with mock.patch.object(jev, "jev_enabled", return_value=True), \
         mock.patch.object(jev, "jev_key", return_value="k"), \
         mock.patch.object(jev.httpx, "post", return_value=_Resp({"answers": []})):
        assert jev.decide("s", {"q": {}}) is None  # answers must be a dict


# ── judge_answer_verdict(): the classifier + anti-cheat ──────────────────
def _answers(choice, conf, genuine=None, prob_pass=None):
    a = {"verdict": {"type": "choice", "choice": choice, "confidence": conf,
                     "probabilities": {"pass": prob_pass or 0.0}}}
    if genuine is not None:
        a["genuine"] = {"type": "noul", "noul": genuine}
    return a


def test_confident_pass_returns_jev_verdict():
    with mock.patch.object(jev, "decide", return_value=_answers("pass", 0.96, 0.8, 0.98)):
        v = jev.judge_answer_verdict(1, "q", "correct answer")
        assert v is not None
        assert v["judgment"] == "pass"
        assert v["source"] == "jev"
        assert v["p_pass"] == 0.98


def test_confident_fail_returns_jev_verdict():
    with mock.patch.object(jev, "decide", return_value=_answers("fail", 0.97)):
        v = jev.judge_answer_verdict(1, "q", "garbage")
        assert v is not None
        assert v["judgment"] == "fail"
        assert v["source"] == "jev"


def test_low_confidence_escalates():
    # Highly-concentrated is not the norm here: an ambiguous answer → None →
    # the caller escalates to the deepseek judge.
    with mock.patch.object(jev, "decide", return_value=_answers("pass", 0.6, 0.8, 0.7)):
        assert jev.judge_answer_verdict(1, "q", "maybe") is None


def test_pass_with_low_genuine_downgrades_to_probe():
    # Anti-cheat: correct but canned → force a rephrase, do not clean-pass.
    with mock.patch.object(jev, "decide", return_value=_answers("pass", 0.96, 0.3, 0.95)):
        v = jev.judge_answer_verdict(1, "q", "textbook verbatim")
        assert v is not None
        assert v["judgment"] == "probe"


def test_decide_none_escalates():
    with mock.patch.object(jev, "decide", return_value=None):
        assert jev.judge_answer_verdict(1, "q", "a") is None


def test_missing_verdict_escalates():
    with mock.patch.object(jev, "decide", return_value={"genuine": {"noul": 0.9}}):
        assert jev.judge_answer_verdict(1, "q", "a") is None


# ── judge_answer() integration: Jev-first + deepseek fallback ────────────
def test_judge_answer_uses_jev_when_confident():
    from engine import exam
    with mock.patch.object(exam.jev_module, "judge_answer_verdict",
                           return_value={"judgment": "pass", "note": "jev:pass", "source": "jev"}):
        assert exam.judge_answer(2, "q", "a") == {"judgment": "pass", "note": "jev:pass", "source": "jev"}


def test_judge_answer_falls_back_to_llm_when_jev_escalates():
    from engine import exam
    # Jev returns None (ambiguous) → deepseek path runs.  Stub llm.complete to
    # return a clean JSON verdict.
    with mock.patch.object(exam.jev_module, "judge_answer_verdict", return_value=None), \
         mock.patch.object(exam.llm, "complete",
                           return_value='{"judgment": "fail", "note": "off-topic"}'):
        v = exam.judge_answer(1, "q", "a")
        assert v["judgment"] == "fail"
        assert v["note"] == "off-topic"
        assert v["source"] == "llm"


def test_judge_answer_falls_back_to_llm_when_jev_raises():
    from engine import exam
    def boom(*a, **k):
        raise RuntimeError("jev exploded")
    with mock.patch.object(exam.jev_module, "judge_answer_verdict", side_effect=boom), \
         mock.patch.object(exam.llm, "complete",
                           return_value='{"judgment": "pass", "note": "ok"}'):
        v = exam.judge_answer(1, "q", "a")
        assert v["judgment"] == "pass"
        assert v["source"] == "llm"