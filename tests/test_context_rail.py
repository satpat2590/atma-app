"""Tests for the context-source rail — the personalization layer.

Proves the fail-open contract: a source that raises must degrade to being
skipped, never blow up the render, and never block the exam/lesson loop.
"""

from engine.context import ContextSource, render


def _boom():
    raise RuntimeError("source exploded")


def _ok():
    return "10%"


def test_render_never_raises_on_broken_source():
    sources = [
        ContextSource("ok", "OK", _ok),
        ContextSource("boom", "BOOM", _boom),
    ]
    out = render(sources)
    assert "OK: 10%" in out
    assert "BOOM" not in out  # broken source silently skipped


def test_render_empty_when_all_none():
    assert render([ContextSource("n", "N", lambda: None)]) == ""


def test_render_skips_none_values():
    out = render([ContextSource("a", "A", lambda: None), ContextSource("b", "B", _ok)])
    assert out == "B: 10%"
