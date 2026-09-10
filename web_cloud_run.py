"""web_cloud_run.py -- Cloud Run entry point for the browser web app.

Runs api/web_app.py's FastAPI app directly on $PORT. The knowledge layer's
schema sync (and, if Slack credentials are configured, the Slack bot itself)
both start in the background from api/web_app.py's own startup event, so
this file just needs to open the port -- a real deploy showed schema sync can
take longer than Cloud Run's startup-probe timeout on a cold container, which
meant blocking on it here before uvicorn ever started serving could leave the
container unable to start at all. This container's local disk does not
survive a restart, so it starts with the same knowledge gap the Slack Cloud
Run host does (see slack_cloud_run.py) until sync catches back up.

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
  table.rules {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
  table.rules td, table.rules th {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #eee; font-size: 0.9rem; vertical-align: top; }}
  table.rules button {{ padding: 4px 10px; font-size: 0.85rem; margin-right: 4px; }}
  .btn-approve {{ background: #dff6dd; border: 1px solid #8bc48b; }}
  .btn-reject {{ background: #fde2e2; border: 1px solid #d98a8a; }}
  .muted {{ color: #777; font-size: 0.9rem; }}
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

<div class="box">
  <h3>Rules awaiting a second reviewer</h3>
  <p class="muted">A rule scoped to "pattern" or "universal" governs every future user's results, so it
     sits here inactive until someone other than whoever submitted it approves it. A rule scoped to one
     campaign isn't listed here -- it already applies immediately, contained to that campaign.</p>
  <div id="pending-rules">Loading...</div>
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

function escapeHtml(s) {{
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({{
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }})[c]);
}}

async function loadPendingRules() {{
  const box = document.getElementById('pending-rules');
  try {{
    const resp = await fetch('/admin/pending-rules');
    const data = await resp.json();
    const rules = data.rules || [];
    if (!rules.length) {{
      box.innerHTML = '<p class="muted">Nothing waiting on review right now.</p>';
      return;
    }}
    const rows = rules.map(r => `
      <tr id="rule-row-${{r.id}}">
        <td>${{escapeHtml(r.rule_text)}}<br><span class="muted">scope: ${{escapeHtml(r.scope)}}</span></td>
        <td>${{escapeHtml(r.added_by)}}</td>
        <td>${{escapeHtml(r.added_at)}}</td>
        <td>
          <button class="btn-approve" onclick="reviewRule(${{r.id}}, 'approve')">Approve</button>
          <button class="btn-reject" onclick="reviewRule(${{r.id}}, 'reject')">Reject</button>
        </td>
      </tr>`).join('');
    box.innerHTML = `<table class="rules">
      <thead><tr><th>Rule</th><th>Submitted by</th><th>Submitted at</th><th></th></tr></thead>
      <tbody>${{rows}}</tbody>
    </table>`;
  }} catch (err) {{
    box.innerHTML = '<p class="muted">Could not load pending rules: ' + escapeHtml(String(err)) + '</p>';
  }}
}}

async function reviewRule(ruleId, action) {{
  const row = document.getElementById('rule-row-' + ruleId);
  try {{
    const resp = await fetch('/admin/pending-rules/' + action, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ rule_id: ruleId }}),
    }});
    const data = await resp.json();
    if (resp.ok) {{
      if (row) row.remove();
    }} else {{
      // Most commonly a maker-checker rejection -- the signed-in reviewer is the
      // same person who submitted the rule, so it can't be approved from here.
      alert(data.message || ('Could not ' + action + ' this rule.'));
    }}
  }} catch (err) {{
    alert('Request failed: ' + err);
  }}
}}

loadPendingRules();
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
    # Schema sync now runs in the background from api/web_app.py's own startup
    # event (see _sync_schema_thread there) rather than blocking here before
    # uvicorn ever opens the port -- a real Cloud Run deploy of this file
    # showed schema sync can take longer than Cloud Run's startup-probe
    # timeout on a cold container, which meant the container never started at
    # all. Serving immediately and syncing in the background (the same
    # pattern slack_cloud_run.py already used successfully) fixes that.
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
