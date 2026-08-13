"""Database helpers — Supabase URL resolution + connection context manager."""

import os
from contextlib import contextmanager

import psycopg2

_SUPABASE_URL = None


def _supabase_url():
    """Atma Supabase URL. Prefer env var; fall back to shell rc for dev shells."""
    global _SUPABASE_URL
    if _SUPABASE_URL is not None:
        return _SUPABASE_URL
    env_url = os.environ.get("ATMA_DATABASE_URL")
    if env_url:
        _SUPABASE_URL = env_url
        return _SUPABASE_URL
    for rc in (".zshrc", ".bashrc"):
        p = os.path.expanduser("~/" + rc)
        if not os.path.exists(p):
            continue
        for line in open(p):
            if "DATABASE_URL" in line and "supabase" in line:
                _SUPABASE_URL = line.split("=", 1)[1].strip().strip("\"'")
                return _SUPABASE_URL
    return None


@contextmanager
def get_db():
    """Yield a psycopg2 connection.  Caller MUST consume inside a `with` block."""
    url = _supabase_url()
    if not url:
        raise RuntimeError("no supabase url")
    con = psycopg2.connect(url, connect_timeout=8)
    try:
        yield con
    finally:
        con.close()