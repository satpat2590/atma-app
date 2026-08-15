"""Atma Dashboard — FastAPI application assembly.

Imports all route modules and mounts static files.  The ``app`` object is
the single FastAPI instance that uvicorn serves.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

APP_DIR = Path(__file__).parent
STATIC = APP_DIR.parent / "static"

app = FastAPI(title="Atma Dashboard — atma.omnisatya.cc")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Route modules ────────────────────────────────────────────
from app.routes_system import router as system_router
from app.routes_growth import router as growth_router
from app.routes_gyani import router as gyani_router
from app.routes_tutor import router as tutor_router
from app.routes_submissions import router as submissions_router
from app.routes_triage import router as triage_router

app.include_router(system_router)
app.include_router(growth_router)
app.include_router(gyani_router)
app.include_router(tutor_router)
app.include_router(submissions_router)
app.include_router(triage_router)

app.mount("/static", StaticFiles(directory=STATIC), name="static")

# ── app.mount for mindmap static already handled by routes_system ──