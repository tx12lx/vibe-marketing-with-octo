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

/admin is a small page for the same purpose, reachable from a browser -- IAP
only accepts an actual signed-in person, never a script, so this is the one
way left to push an updated knowledge database to this specific service
without a custom OAuth client and the GCP permissions that would take to set
up. Protected by nothing here directly -- IAP in front of the whole service
is what gates it, same as everything else this app serves.

Run locally for testing:  python web_cloud_run.py
Deployed via:              gcloud run deploy (see deploy notes in the repo)
"""
from __future__ import annotations

import logging
import os

import uvicorn
from fastapi import Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from knowledge.store import _db_path, connect
from knowledge.sync_schema import main as sync_schema_main
from api.web_app import app

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _status_summary() -> str:
    try:
        with connect() as conn:
            tables = conn.execute("SELECT COUNT(*) AS n FROM tables").fetchone()["n"]
            columns = conn.execute("SELECT COUNT(*) AS n FROM columns").fetchone()["n"]
            confirmed = conn.execute(
                "SELECT COUNT(*) AS n FROM columns WHERE description_source='human'"
            ).fetchone()["n"]
            rules = conn.execute("SELECT COUNT(*) AS n FROM business_rules WHERE status='active'").fetchone()["n"]
            glossary = conn.execute("SELECT COUNT(*) AS n FROM glossary_terms").fetchone()["n"]
        return (
            f"{tables} table(s), {columns} column(s) synced ({confirmed} human-confirmed), "
            f"{rules} active business rule(s), {glossary} glossary term(s)."
        )
    except Exception as exc:  # noqa: BLE001 -- shown to a human on an admin page, not swallowed silently
        return f"Could not read the knowledge database: {exc}"


@app.get("/admin", response_class=HTMLResponse)
async def admin_page() -> HTMLResponse:
    html = f"""<!doctype html>
<title>Vibe OCTO -- knowledge database admin</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 16px; color: #222; }}
  h1 {{ font-size: 1.3rem; }}
  .status {{ background: #f4f4f4; border-radius: 6px; padding: 12px 16px; margin: 16px 0; }}
  .box {{ border: 1px solid #ddd; border-radius: 6px; padding: 16px; margin: 16px 0; }}
  button {{ padding: 8px 16px; cursor: pointer; }}
  #result {{ margin-top: 12px; font-weight: 600; }}
</style>
<h1>Knowledge database admin</h1>
<p>This instance's own copy of the knowledge database, currently:</p>
<div class="status">{_status_summary()}</div>

<div class="box">
  <h3>Download a backup</h3>
  <p>Save this instance's current database to your computer before replacing it.</p>
  <a href="/admin/knowledge-db"><button type="button">Download backup</button></a>
</div>

<div class="box">
  <h3>Replace with an updated copy</h3>
  <p>Choose a <code>vibe_octo_knowledge.db</code> file (e.g. from your laptop) to replace this
     instance's database with it.</p>
  <input type="file" id="dbfile" accept=".db">
  <button type="button" onclick="uploadDb()">Upload and replace</button>
  <div id="result"></div>
</div>

<script>
async function uploadDb() {{
  const input = document.getElementById('dbfile');
  const result = document.getElementById('result');
  if (!input.files.length) {{
    result.textContent = 'Choose a file first.';
    return;
  }}
  if (!confirm('This replaces this instance\\'s knowledge database right now. Continue?')) return;
  result.textContent = 'Uploading...';
  try {{
    const resp = await fetch('/admin/knowledge-db', {{ method: 'POST', body: input.files[0] }});
    const data = await resp.json();
    if (resp.ok) {{
      result.textContent = 'Done -- ' + data.bytes_written + ' bytes written. Reloading status...';
      setTimeout(() => location.reload(), 1200);
    }} else {{
      result.textContent = 'Failed: ' + (data.error || resp.status);
    }}
  }} catch (err) {{
    result.textContent = 'Failed: ' + err;
  }}
}}
</script>
"""
    return HTMLResponse(content=html)


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
