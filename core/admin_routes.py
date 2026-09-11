"""core/admin_routes.py -- shared knowledge-database backup/restore routes.

Registered identically by both web_cloud_run.py and slack_cloud_run.py (each a
separate Cloud Run entry point, each with its own container disk that doesn't
survive a restart) -- previously each hand-copied the same two routes. One
version here, registered onto whichever FastAPI app owns it.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from knowledge.store import _db_path

_log = logging.getLogger(__name__)


def register_knowledge_db_admin_routes(app: FastAPI) -> None:
    """Add GET/POST /admin/knowledge-db (manual backup/restore of the local
    SQLite cache) to the given FastAPI app."""

    @app.get("/admin/knowledge-db")
    async def download_knowledge_db() -> Response:
        path = _db_path()
        if not path.exists():
            return JSONResponse({"error": "No knowledge database file exists yet on this instance."}, status_code=404)
        return FileResponse(path, media_type="application/octet-stream", filename="vibe_octo_knowledge.db")

    @app.post("/admin/knowledge-db")
    async def upload_knowledge_db(request: Request) -> dict:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        body = await request.body()
        path.write_bytes(body)
        _log.info("Knowledge database replaced from an uploaded copy (%d bytes).", len(body))
        return {"status": "ok", "bytes_written": len(body)}
