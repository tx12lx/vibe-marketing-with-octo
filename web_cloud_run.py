"""web_cloud_run.py -- Cloud Run entry point for the browser web app.

Runs api/web_app.py's FastAPI app directly on $PORT. The knowledge layer's
schema sync starts in the background from api/web_app.py's own startup
event, so this file just needs to open the port -- a real deploy showed
schema sync can take longer than Cloud Run's startup-probe timeout on a cold
container, which meant blocking on it here before uvicorn ever started
serving could leave the container unable to start at all. This container's
local disk does not survive a restart, so knowledge synced from BigQuery
starts fresh each time until sync catches back up.

Reachable by the public over HTTPS, but only after Identity-Aware Proxy
authenticates the caller as one of the people granted
roles/iap.httpsResourceAccessor on this service. IAP sits in front of Cloud
Run's own ingress, so nothing here needs to enforce that itself.

/admin/knowledge-db exists to back up or restore the knowledge database by
hand, since this container's local disk isn't persistent. It's exposed on
the same app IAP already protects.

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

from core.admin_routes import register_admin_status_page, register_knowledge_db_admin_routes
import api.web_app as web_app
from api.web_app import app

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

register_knowledge_db_admin_routes(app)
register_admin_status_page(app, get_schema_sync_status=lambda: web_app.schema_sync_status)


if __name__ == "__main__":
    # Schema sync now runs in the background from api/web_app.py's own startup
    # event (see _sync_schema_thread there) rather than blocking here before
    # uvicorn ever opens the port -- a real Cloud Run deploy of this file
    # showed schema sync can take longer than Cloud Run's startup-probe
    # timeout on a cold container, which meant the container never started at
    # all. Serving immediately and syncing in the background fixes that.
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
