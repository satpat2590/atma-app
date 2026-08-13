"""Atma Dashboard — thin wrapper preserving `uvicorn server:app` for systemd.

The application logic lives in the ``app`` package.  This file exists so the
existing systemd unit (`uvicorn server:app --host 127.0.0.1 --port 8601`)
continues to work without any infrastructure changes.
"""

from app.main import app  # noqa: F401 — uvicorn needs this name