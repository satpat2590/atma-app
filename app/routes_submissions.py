from app.db import get_db, _supabase_url
from app.helpers import _parse_lesson_title, _score_color, build_lesson_index
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, FileResponse
import json, os, re, sqlite3, subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

router = APIRouter()
GWS_SCRIPT = (Path.home() / ".hermes" / "profiles" / "gyani" / "skills" /
              "productivity" / "google-workspace" / "scripts" / "google_api.py")
GYANI_HOME = Path.home() / ".hermes" / "profiles" / "gyani"
SUBMISSIONS_FOLDER = "Gyani-Submissions"


def _gws(*args, timeout=30):
    """Run the Google Workspace CLI with Gyani's credentials."""
    if not GWS_SCRIPT.exists():
        return None
    env = dict(os.environ, HERMES_HOME=str(GYANI_HOME))
    try:
        r = subprocess.run(["python3", str(GWS_SCRIPT), *args],
                           capture_output=True, text=True,
                           timeout=timeout, env=env)
        if r.returncode != 0:
            return None
        return json.loads(r.stdout) if r.stdout.strip() else None
    except Exception:
        return None

@router.get("/api/gyani/submissions")
def gyani_submissions():
    """Artifact submissions in the Gyani-Submissions Drive folder."""
    files = _gws("drive", "search", SUBMISSIONS_FOLDER, "--max", "1")
    folder_id = None
    if isinstance(files, list):
        for f in files:
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                folder_id = f.get("id")
                break
    listing = _gws("drive", "search", f"'{folder_id}' in parents and trashed=false",
                   "--raw-query", "--max", "25") if folder_id else None

    items = []
    for f in (listing or []):
        if f.get("mimeType") == "application/vnd.google-apps.folder":
            continue
        mime = f.get("mimeType", "")
        kind = ("image" if mime.startswith("image/")
                else "doc" if "document" in mime or "wordprocessing" in mime
                else "pdf" if "pdf" in mime
                else "sheet" if "spreadsheet" in mime
                else "file")
        items.append({
            "file_id": f.get("id"),
            "name": f.get("name"),
            "kind": kind,
            "mime": mime,
            "web_link": f.get("webViewLink"),
            "modified": f.get("modifiedTime"),
        })

    # Cross-reference the ledger for review status
    reviewed = {}
    url = _supabase_url()
    if url:
        try:
            con = psycopg2.connect(url, connect_timeout=8)
            cur = con.cursor()
            cur.execute(
                """SELECT task_id, submissions FROM verifications
                   WHERE submissions IS NOT NULL"""
            )
            for task_id, subs in cur.fetchall():
                for s in (subs or []):
                    if s.get("file_id"):
                        reviewed[s["file_id"]] = {
                            "task_id": task_id,
                            "assessment": s.get("assessment"),
                        }
            con.close()
        except Exception:
            pass

    for it in items:
        r = reviewed.get(it["file_id"])
        it["reviewed"] = bool(r and r.get("assessment"))
        it["task_id"] = r.get("task_id") if r else None
        it["assessment"] = r.get("assessment") if r else None

    return {
        "folder": SUBMISSIONS_FOLDER,
        "folder_id": folder_id,
        "folder_link": (f"https://drive.google.com/drive/folders/{folder_id}"
                        if folder_id else None),
        "submissions": items,
        "unreviewed": sum(1 for i in items if not i["reviewed"]),
    }