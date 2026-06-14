"""
Drachenboot Hamburg — Foto-Upload Backend
==========================================
FastAPI · Python 3.10+
"""

import os
import uuid
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import aiofiles
from PIL import Image

# ── CONFIG ────────────────────────────────────────────────────────────────────

UPLOAD_DIR   = Path(os.getenv("UPLOAD_DIR", "uploads"))
DB_PATH      = Path(os.getenv("DB_PATH", "uploads.db"))
MAX_FILE_MB  = int(os.getenv("MAX_FILE_MB", "20"))
MAX_BYTES    = MAX_FILE_MB * 1024 * 1024
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
ALLOWED_EXT  = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
MAX_UPLOADS_PER_HOUR = 5

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── DATABASE ──────────────────────────────────────────────────────────────────

def init_db() -> None:
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS uploads (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_id  TEXT    NOT NULL UNIQUE,
                teamname   TEXT    NOT NULL,
                firma      TEXT,
                filename   TEXT    NOT NULL,
                filepath   TEXT    NOT NULL,
                mime_type  TEXT,
                filesize   INTEGER,
                ip_address TEXT,
                created_at TEXT    NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_created ON uploads(created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_ip ON uploads(ip_address, created_at)")

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

# ── APP ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Drachenboot Hamburg · Upload API",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup():
    init_db()
    log.info("Datenbank initialisiert · Upload-Dir: %s", UPLOAD_DIR.resolve())

# ── VALIDATION ────────────────────────────────────────────────────────────────

def validate_image_file(file: UploadFile, data: bytes) -> None:
    if file.content_type not in ALLOWED_MIME:
        raise HTTPException(status_code=415, detail="Dateityp nicht erlaubt. Erlaubt: JPG, PNG, WEBP, HEIC.")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_EXT:
        raise HTTPException(status_code=415, detail="Ungültige Dateiendung.")
    if file.content_type not in {"image/heic", "image/heif"}:
        try:
            from io import BytesIO
            img = Image.open(BytesIO(data))
            img.verify()
        except Exception:
            raise HTTPException(status_code=422, detail="Die Datei ist kein gültiges Bild.")

def check_rate_limit(ip: str) -> None:
    with get_db() as db:
        since = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
        row = db.execute(
            "SELECT COUNT(*) as cnt FROM uploads WHERE ip_address = ? AND created_at >= ?",
            (ip, since)
        ).fetchone()
        if row["cnt"] >= MAX_UPLOADS_PER_HOUR:
            raise HTTPException(status_code=429, detail="Zu viele Uploads. Bitte in einer Stunde erneut versuchen.")

def safe_filename(original: str, upload_id: str) -> str:
    suffix = Path(original).suffix.lower() or ".jpg"
    return f"{upload_id}{suffix}"

# ── ROUTES ───────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "drachenboot-upload"}


@app.post("/api/upload")
async def upload_photo(
    request:  Request,
    teamname: Annotated[str, Form(min_length=1, max_length=100)],
    firma:    Annotated[str | None, Form(max_length=100)] = None,
    photo:    UploadFile = File(...),
) -> JSONResponse:
    client_ip = request.client.host if request.client else "unknown"
    check_rate_limit(client_ip)

    data = await photo.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"Datei zu groß (max. {MAX_FILE_MB} MB).")
    if len(data) == 0:
        raise HTTPException(status_code=422, detail="Leere Datei.")

    validate_image_file(photo, data)

    upload_id = str(uuid.uuid4())
    filename  = safe_filename(photo.filename or "foto.jpg", upload_id)
    filepath  = UPLOAD_DIR / filename

    async with aiofiles.open(filepath, "wb") as f:
        await f.write(data)

    now = datetime.now(timezone.utc).isoformat()
    with get_db() as db:
        db.execute(
            """INSERT INTO uploads
               (upload_id, teamname, firma, filename, filepath, mime_type, filesize, ip_address, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (upload_id, teamname.strip(), firma.strip() if firma else None,
             filename, str(filepath), photo.content_type, len(data), client_ip, now)
        )

    log.info("Upload OK · Team: %s · Firma: %s · Datei: %s · %s KB",
             teamname, firma or "–", filename, len(data) // 1024)

    return JSONResponse(content={"upload_id": upload_id, "message": "Foto erfolgreich gespeichert. Danke!"})


@app.get("/api/photos")
async def get_photos(limit: int = 50, offset: int = 0):
    """Öffentliche Galerie-API — gibt Teamname, Firma, Datum und Bild-URL zurück."""
    with get_db() as db:
        rows = db.execute(
            """SELECT upload_id, teamname, firma, filename, created_at
               FROM uploads ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (limit, offset)
        ).fetchall()
        total = db.execute("SELECT COUNT(*) as n FROM uploads").fetchone()["n"]

    items = []
    for r in rows:
        # Datum formatieren: "14. Jun 2026"
        try:
            dt = datetime.fromisoformat(r["created_at"])
            date_str = dt.strftime("%-d. %b %Y")
        except Exception:
            date_str = r["created_at"][:10]

        items.append({
            "upload_id": r["upload_id"],
            "teamname":  r["teamname"],
            "firma":     r["firma"],
            "image_url": f"/uploads/{r['filename']}",
            "date":      date_str,
        })

    return {"total": total, "items": items}


@app.get("/api/admin/uploads")
async def list_uploads(limit: int = 50, offset: int = 0):
    with get_db() as db:
        rows = db.execute(
            "SELECT upload_id, teamname, firma, filename, filesize, created_at "
            "FROM uploads ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        total = db.execute("SELECT COUNT(*) as n FROM uploads").fetchone()["n"]
    return {"total": total, "items": [dict(r) for r in rows]}


# ── STATISCHE DATEIEN ─────────────────────────────────────────────────────────
# Uploads-Ordner öffentlich servieren (für Galerie)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
# Frontend-Dateien (index.html, gallery.html)
app.mount("/", StaticFiles(directory=".", html=True), name="static")


# ── HAUPTPROGRAMM ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
