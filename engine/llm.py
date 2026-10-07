"""Provider seam for the engine — one LLM call, no HTTP framing leaking out.

The engine is synchronous (called from FastAPI routes AND from cron scripts /
Hermes batch pipeline).  Use httpx for a sync, blocking call that works
equally well in both contexts; nothing here touches FastAPI or aiohttp.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
GYANI_MODEL = "deepseek/deepseek-v4-pro"
_MAX_TOKENS = 1024
_TIMEOUT_S = 60.0
_RETRIES = 3
_RETRY_DELAY_S = 1.0


class LLMError(RuntimeError):
    """Raised when a call to the model provider fails or returns no text."""


def openrouter_key() -> str | None:
    """OpenRouter key — Gyani's profile .env first, then the environment."""
    env_path = Path.home() / ".hermes" / "profiles" / "gyani" / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("OPENROUTER_API_KEY")


def complete(
    system: str,
    user: str,
    *,
    model: str = GYANI_MODEL,
    max_tokens: int = _MAX_TOKENS,
    temperature: float | None = None,
    api_key: str | None = None,
) -> str:
    """One chat completion.  Returns the model's text or raises LLMError.

    ``api_key`` may be injected for tests; defaults to ``openrouter_key()``.
    """
    key = api_key or openrouter_key()
    if not key:
        raise LLMError("no OpenRouter API key configured")

    body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
    }
    if temperature is not None:
        body["temperature"] = temperature

    last_err: LLMError | None = None
    for attempt in range(_RETRIES):
        try:
            resp = httpx.post(
                OPENROUTER_URL,
                json=body,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                timeout=_TIMEOUT_S,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            if not content or not content.strip():
                raise LLMError("provider returned empty content")
            return content
        except LLMError as e:
            # Empty content / malformed body is often transient (provider flake)
            # — retry with a short backoff before giving up.
            last_err = e
            if attempt < _RETRIES - 1:
                time.sleep(_RETRY_DELAY_S * (attempt + 1))
                continue
        except httpx.HTTPError as e:
            last_err = LLMError(f"provider HTTP error: {e}")
            if attempt < _RETRIES - 1:
                time.sleep(_RETRY_DELAY_S * (attempt + 1))
                continue
        except (KeyError, IndexError, ValueError) as e:
            last_err = LLMError(f"malformed provider response: {e}")
            if attempt < _RETRIES - 1:
                time.sleep(_RETRY_DELAY_S * (attempt + 1))
                continue
    raise last_err if last_err else LLMError("provider failed")
