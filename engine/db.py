"""Supabase connection seam for the engine.

Single source of truth for "how do we reach the Atma Postgres".  Prefer the
ATMA_DATABASE_URL env var; fall back to Gyani's profile .env; last-dollar
fallback to the shell rc (dev shells that don't source .env).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg2

_url_cache: str | None = None


def supabase_url() -> str | None:
    """Resolve the Atma Supabase URL, caching across calls."""
    global _url_cache
    if _url_cache is not None:
        return _url_cache

    env_url = os.environ.get("ATMA_DATABASE_URL")
    if env_url:
        _url_cache = env_url
        return _url_cache

    # Gyani profile .env (same URL the engine's caller — Gyani — uses).
    gyani_env = Path.home() / ".hermes" / "profiles" / "gyani" / ".env"
    if gyani_env.exists():
        for line in gyani_env.read_text().splitlines():
            if line.startswith("ATMA_DATABASE_URL="):
                _url_cache = line.split("=", 1)[1].strip().strip("\"'")
                return _url_cache
            if line.startswith("DATABASE_URL="):
                _url_cache = line.split("=", 1)[1].strip().strip("\"'")
                return _url_cache

    # Dev shell fallback — no .env, but the URL was exported in an rc file.
    for rc in (".zshrc", ".bashrc"):
        p = os.path.expanduser("~/" + rc)
        if not p or not os.path.exists(p):
            continue
        try:
            for line in open(p, encoding="utf-8", errors="ignore"):
                if "DATABASE_URL" in line and "supabase" in line:
                    _url_cache = line.split("=", 1)[1].strip().strip("\"'")
                    return _url_cache
        except OSError:
            continue

    return None


@contextmanager
def connect() -> Iterator[psycopg2.extensions.connection]:
    """Yield a psycopg2 connection.  Must be used inside a ``with`` block."""
    url = supabase_url()
    if not url:
        raise RuntimeError("no Supabase URL resolved (set ATMA_DATABASE_URL)")
    con = psycopg2.connect(url, connect_timeout=8)
    try:
        yield con
    finally:
        con.close()
