"""Tests for the LLM seam's retry/robustness behaviour.

Mock the httpx transport so no network is involved; verify empty-content and
HTTP-error retries and the final LLMError propagation.
"""

from unittest import mock

import pytest

from engine import llm


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise llm.httpx.HTTPStatusError("err", request=mock.MagicMock(), response=mock.MagicMock())

    def json(self):
        return self._payload


def _resp(content):
    return _FakeResp({"choices": [{"message": {"content": content}}]})


def test_retries_then_succeeds_on_empty_content():
    # first two calls empty, third succeeds
    calls = [None, None, "OK"]

    def fake_post(*a, **k):
        c = calls.pop(0)
        if c is None:
            return _FakeResp({"choices": [{"message": {"content": None}}]})
        return _resp(c)

    with mock.patch.object(llm.httpx, "post", side_effect=fake_post), \
         mock.patch.object(llm, "openrouter_key", return_value="k"):
        assert llm.complete("s", "u") == "OK"


def test_gives_up_after_retries():
    def fake_post(*a, **k):
        return _FakeResp({"choices": [{"message": {"content": None}}]})

    with mock.patch.object(llm.httpx, "post", side_effect=fake_post), \
         mock.patch.object(llm, "openrouter_key", return_value="k"), \
         mock.patch.object(llm.time, "sleep", return_value=None):
        with pytest.raises(llm.LLMError):
            llm.complete("s", "u")


def test_empty_string_treated_as_empty():
    def fake_post(*a, **k):
        return _resp("   ")

    with mock.patch.object(llm.httpx, "post", side_effect=fake_post), \
         mock.patch.object(llm, "openrouter_key", return_value="k"), \
         mock.patch.object(llm.time, "sleep", return_value=None):
        with pytest.raises(llm.LLMError):
            llm.complete("s", "u")
