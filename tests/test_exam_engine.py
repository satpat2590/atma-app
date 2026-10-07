"""Unit tests for the exam grading state machine — no DB, no LLM.

The ``judge`` / ``quality_for`` logic is pure; these tests prove the
mechanical rules in the protocol: strike count, probe, level progression,
pass at synthesis, and quality mapping.
"""

import pytest

from engine.exam import Exam, Exchange, judge, quality_for


def _exchange(level, judgment, question="q", answer="a"):
    return Exchange(level=level, question=question, answer=answer, judgment=judgment)


def test_clean_pass_advances_and_completes():
    exam = Exam(task_id=1)
    actions = []
    for level in (1, 2, 3, 4):
        actions.append(judge(exam, _exchange(level, "pass")))
    assert actions == ["next", "next", "next", "pass"]
    assert exam.level == 4


def test_two_failures_at_same_level_fails():
    exam = Exam(task_id=1, level=2)
    assert judge(exam, _exchange(2, "fail")) == "retry"   # first strike, stays
    assert exam.strikes == 1
    assert exam.level == 2
    assert judge(exam, _exchange(2, "fail")) == "fail"    # second strike -> fail
    assert exam.strikes == 2


def test_fail_then_pass_resets_strikes():
    exam = Exam(task_id=1, level=1)
    assert judge(exam, _exchange(1, "fail")) == "retry"   # stay at level 1
    assert exam.level == 1
    assert judge(exam, _exchange(1, "pass")) == "next"    # strike reset, advance
    assert exam.strikes == 0
    assert exam.level == 2


def test_failure_never_advances_level():
    # Two fails at DIFFERENT levels is impossible by construction: a fail never
    # moves the level, so a second fail is necessarily at the same level.
    exam = Exam(task_id=1, level=3)
    judge(exam, _exchange(3, "fail"))
    assert exam.level == 3
    assert judge(exam, _exchange(3, "fail")) == "fail"


def test_probe_does_not_advance_level_and_resets_strikes():
    exam = Exam(task_id=1, level=1)
    assert judge(exam, _exchange(1, "probe")) == "probe"
    assert exam.level == 1  # level unchanged
    assert exam.strikes == 0


def test_pass_at_max_level_triggers_pass():
    exam = Exam(task_id=1, level=4)
    assert judge(exam, _exchange(4, "pass")) == "pass"


def test_quality_scoring_is_mechanical():
    # all pass -> 5
    exam = Exam(task_id=1)
    for level in (1, 2, 3, 4):
        judge(exam, _exchange(level, "pass"))
    assert quality_for(exam) == 5

    # mostly pass with one fail -> 4
    exam = Exam(task_id=1)
    judge(exam, _exchange(1, "pass"))
    judge(exam, _exchange(2, "pass"))
    judge(exam, _exchange(3, "pass"))
    judge(exam, _exchange(4, "fail"))
    assert quality_for(exam) == 4


def test_quality_empty_transcript_is_minimum():
    assert quality_for(Exam(task_id=1)) == 1
