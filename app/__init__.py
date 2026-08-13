"""Atma Dashboard — modular FastAPI backend.

Split from the original 1070-line server.py monolith for maintainability.
Each concern lives in its own route module; app.main assembles them.
"""

# Package marker