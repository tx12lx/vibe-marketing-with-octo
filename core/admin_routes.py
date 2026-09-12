"""core/admin_routes.py -- shared knowledge-database admin routes.

Registered identically by api/web_app.py and web_cloud_run.py -- previously
each hand-copied the same routes/HTML. One version here, registered onto
whichever FastAPI app owns it.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from knowledge import git_store
from knowledge.store import _db_path, connect

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


def _durability_summary() -> str:
    """One honest line on whether learned knowledge is actually reaching GitHub right
    now. Previously a GitHub sync failure only ever appeared in a server log nobody
    was watching -- a correction could look saved (the local cache always updates
    immediately) while its durable copy silently never arrived. This is read fresh on
    every /admin load, not cached, so it can never go stale the way that silent
    failure did.

    Local persistence note: on a VM (persistent disk), the local SQLite cache
    already survives a service restart or reboot by itself -- GitHub sync only
    protects against losing the disk/instance itself, and gives a timestamped
    audit trail of every learned rule as a real commit. On Cloud Run (ephemeral
    container disk), GitHub sync is what makes anything survive a restart at all.
    """
    if not git_store.is_configured():
        return (
            "Durability: GITHUB_TOKEN is not set -- learned knowledge is stored only "
            "on this instance's local disk, with no external backup."
        )
    if not git_store.last_sync_status:
        return "Durability: GitHub token is configured; no writes attempted yet this run."
    failed = {k: v for k, v in git_store.last_sync_status.items() if v != "ok"}
    if failed:
        names = ", ".join(failed.keys())
        return (
            f"Durability: WARNING -- the last GitHub sync failed for {names}. Learned "
            f"knowledge is still saved locally on this instance, but is NOT reaching "
            f"GitHub right now. Detail: {failed}"
        )
    return "Durability: all learned knowledge is syncing to GitHub successfully."


def _status_summary(get_schema_sync_status: Optional[Callable[[], dict]] = None) -> str:
    try:
        with connect() as conn:
            tables = conn.execute("SELECT COUNT(*) AS n FROM tables").fetchone()["n"]
            columns = conn.execute("SELECT COUNT(*) AS n FROM columns").fetchone()["n"]
            confirmed = conn.execute(
                "SELECT COUNT(*) AS n FROM columns WHERE description_source='human'"
            ).fetchone()["n"]
            rules = conn.execute("SELECT COUNT(*) AS n FROM business_rules WHERE status='active'").fetchone()["n"]
            pending_rules = conn.execute(
                "SELECT COUNT(*) AS n FROM business_rules WHERE status='pending_review'"
            ).fetchone()["n"]
            glossary = conn.execute("SELECT COUNT(*) AS n FROM glossary_terms").fetchone()["n"]
        summary = (
            f"{tables} table(s), {columns} column(s) synced ({confirmed} human-confirmed), "
            f"{rules} active business rule(s) ({pending_rules} awaiting review), {glossary} glossary term(s)."
        )
        if get_schema_sync_status is not None:
            sync = get_schema_sync_status()
            if sync.get("state") == "failed":
                summary += f" SCHEMA SYNC FAILED: {sync.get('error')}"
            elif sync.get("state") == "running":
                summary += " (schema sync still running...)"
        return summary + " " + _durability_summary()
    except Exception as exc:  # noqa: BLE001 -- shown to a human on an admin page, not swallowed silently
        return f"Could not read the knowledge database: {exc}"


_ADMIN_PAGE_TEMPLATE = """<!doctype html>
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
<div class="status">{status}</div>

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
  <p class="muted">The second-reviewer gate is currently off for every rule scope (2026-09-11
     decision), so every confirmed correction applies immediately and nothing should appear here.
     If this list is ever non-empty again, it's because the gate has been turned back on for some
     scope -- see knowledge/context.py's _SCOPES_REQUIRING_REVIEW.</p>
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


def register_admin_status_page(
    app: FastAPI, get_schema_sync_status: Optional[Callable[[], dict]] = None,
) -> None:
    """Add GET /admin -- the human-facing status page (durability, schema sync,
    backup/restore, pending-rule review). get_schema_sync_status is an optional
    callable returning the caller's own schema-sync-state dict (api/web_app.py's
    module-level schema_sync_status); omitted where a caller doesn't track one."""

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_page() -> HTMLResponse:
        html = _ADMIN_PAGE_TEMPLATE.format(status=_status_summary(get_schema_sync_status))
        return HTMLResponse(content=html)
