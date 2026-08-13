from app.db import get_db, _supabase_url
from app.helpers import _parse_lesson_title, _score_color, build_lesson_index
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, FileResponse
import json, os, re, sqlite3, subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

router = APIRouter()
OBSIDIAN_CURRICULUM = Path.home() / "Obsidian" / "Curriculum"
OBSIDIAN_DAILY = Path.home() / "Obsidian" / "15-Daily-Notes"
WHOOP_DB = Path.home() / "whoop-sync" / "health.db"
EDORAS_REPO = Path.home() / "edoras"
ALIGNMENT_DIR = OBSIDIAN_CURRICULUM / "alignment-reports"
GRAPH_JSON = Path(__file__).parent.parent / "static" / "graph.json"
STATIC = Path(__file__).parent.parent / "static"

# mindmap_data (570-575) — keep this one, remove the duplicate at 946-950
@router.get("/api/mindmap")
def mindmap_data():
    """Serve pre-generated graph.json."""
    if GRAPH_JSON.exists():
        return FileResponse(GRAPH_JSON)
    return JSONResponse({"error": "graph not generated"}, status_code=503)

# health (1049-1056)
@router.get("/api/health")
def health():
    """Service health check."""
    return {
        "status": "ok",
        "service": "atma-dashboard",
        "version": "1.0.0",
    }

# index (1061-1063)
@router.get("/")
def index():
    return FileResponse(STATIC / "index.html")

# mindmap_page (1066-1068)
@router.get("/mindmap")
def mindmap_page():
    return FileResponse(STATIC / "mindmap.html")