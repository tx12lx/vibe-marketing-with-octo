"""web_cloud_run.py -- Cloud Run entry point for the browser web app.

Runs api/web_app.py's FastAPI app directly on $PORT. The knowledge layer's
schema is synced once, synchronously, before the app starts serving --
this container's local disk does not survive a restart, so it starts with
the same knowledge gap the Slack Cloud Run host does (see slack_cloud_run.py).

Reachable by the public over HTTPS, but only after Identity-Aware Proxy
authenticates the caller as one of the people granted
roles/iap.httpsResourceAccessor on this service. IAP sits in front of Cloud
Run's own ingress, so nothing here needs to enforce that itself.

/admin/knowledge-db exists for the same reason as the Slack host's: to back
up or restore the knowledge database by hand, since this container's local
disk isn't persistent. It's exposed on the same app IAP already protects.

Run locally for testing:  python web_cloud_run.py
Deployed via:              gcloud run deploy (see deploy notes in the repo)
"""
from __future__ import annotations

import logging
import os

import uvicorn
from fastapi import Request, Response
from fastapi.responses import FileResponse, JSONResponse

from knowledge.store import _db_path
from knowledge.sync_schema import main as sync_schema_main
from api.web_app import app

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


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


if __name__ == "__main__":
    try:
        _log.info("Syncing the knowledge layer's schema before serving requests...")
        sync_schema_main()
    except Exception as exc:  # noqa: BLE001 -- a failed sync shouldn't stop the app from starting
        _log.warning("Schema sync at startup failed (app will still start): %s", exc)

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
