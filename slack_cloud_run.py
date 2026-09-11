"""slack_cloud_run.py -- Cloud Run entry point for the Slack front end.

Runs api/slack_app.py's Socket Mode bot in a background thread (its outbound-
only connection to Slack works from Cloud Run's default networking, unlike
the VM's), while the main thread serves a minimal FastAPI app on $PORT --
Cloud Run requires a container to listen on its assigned port to be
considered healthy, even for a service that's really just a background
worker.

Not reachable by the public: this service is deployed without
--allow-unauthenticated, so Cloud Run's own IAM check blocks every request
here (including /admin/*) unless the caller already has the Cloud Run
Invoker role on this specific service -- the same mechanism the relay's
/tunnel endpoint already relies on. No new IAM grants were needed for this,
since the deploying identity already has that right on anything it deploys.

/admin/knowledge-db exists because the knowledge layer's SQLite file lives on
this container's local disk, which Cloud Run does not guarantee survives a
restart. Pull it down to back it up; push a copy back up to restore it --
both explicit, manual, on your own schedule, exactly as agreed.

Run locally for testing:  python slack_cloud_run.py
Deployed via:              gcloud run deploy (see Procfile)
"""
from __future__ import annotations

import logging
import os
import threading

import uvicorn
from fastapi import FastAPI

from core.admin_routes import register_knowledge_db_admin_routes
from knowledge.sync_schema import main as sync_schema_main

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Vibe OCTO -- Slack Cloud Run host")
register_knowledge_db_admin_routes(app)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


def _start_slack_bot() -> None:
    from api.slack_app import main as start_slack_bot  # noqa: PLC0415 -- deferred so a slow import can't block health checks

    try:
        _log.info("Syncing the knowledge layer's schema before starting the bot...")
        sync_schema_main()
    except Exception as exc:  # noqa: BLE001 -- a failed sync shouldn't stop the bot from starting
        _log.warning("Schema sync at startup failed (bot will still start): %s", exc)

    start_slack_bot()


if __name__ == "__main__":
    threading.Thread(target=_start_slack_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
