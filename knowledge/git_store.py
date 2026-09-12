"""knowledge/git_store.py -- durable, version-controlled persistence for the
knowledge layer's human-contributed knowledge (business rules, glossary
terms, feedback events), backed by the GitHub repo itself.

Why GitHub instead of a cloud database: the app's Cloud Run identity has
exactly two permissions -- call Google's AI models, and read (never write)
the campaign BigQuery data. Every cloud storage option that could hold this
knowledge durably (a new BigQuery dataset, a storage bucket, a database)
needs a new IAM grant nobody currently available can make. GitHub sits
entirely outside that permission system: a personal access token scoped to
just this repo is enough, and it gives a genuine audit trail for free --
every learned rule is a real, timestamped commit.

Table/column schema knowledge is NOT stored here -- it's cheaply re-derived
from BigQuery's own (read-only) schema metadata on every startup (see
knowledge/sync_schema.py), so it never needed durable storage in the first
place.

Storage shape: one JSON file per collection under knowledge_data/ in this
repo (business_rules.json, glossary_terms.json, feedback_events.json) -- one
atomic commit per write, not a directory of many small per-record files.
Each file's content is exactly the list of rows knowledge/store.py's SQLite
tables would hold, so the local cache can load them directly with no
transformation.

Writes are queued and applied by a single background worker thread (see
queue_sync()) so a slow GitHub API call never makes a user-facing request
wait -- the local SQLite cache (knowledge/store.py) is always updated first
and is what every read goes through; GitHub is the durability layer behind
it, not the read path.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import queue
import threading
from typing import Optional

import requests

_log = logging.getLogger(__name__)

_API_BASE = "https://api.github.com"
_DEFAULT_REPO = "tx12lx/vibe-marketing-with-octo"
_DATA_DIR = "knowledge_data"
_REQUEST_TIMEOUT_SECONDS = 15

COLLECTIONS = ("business_rules", "glossary_terms", "feedback_events")


def _repo() -> str:
    return os.getenv("GITHUB_KNOWLEDGE_REPO", _DEFAULT_REPO)


def _branch() -> str:
    return os.getenv("GITHUB_KNOWLEDGE_BRANCH", "master")


def is_configured() -> bool:
    """False when no token is set -- callers should degrade gracefully (local-only,
    same as before this module existed) rather than raise, e.g. for local dev."""
    return bool(os.getenv("GITHUB_TOKEN", ""))


def _headers() -> dict:
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is not set -- call is_configured() before using git_store.")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _path_for(collection: str) -> str:
    if collection not in COLLECTIONS:
        raise ValueError(f"Unknown knowledge collection: {collection!r}")
    return f"{_DATA_DIR}/{collection}.json"


def _contents_url(collection: str) -> str:
    return f"{_API_BASE}/repos/{_repo()}/contents/{_path_for(collection)}"


def read_collection(collection: str) -> list[dict]:
    """Fetch one collection's current rows from GitHub. Returns [] if the file
    doesn't exist yet (nothing has ever been written) or the token isn't set."""
    if not is_configured():
        return []
    resp = requests.get(
        _contents_url(collection), headers=_headers(),
        params={"ref": _branch()}, timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    content_b64 = resp.json().get("content", "")
    raw = base64.b64decode(content_b64).decode("utf-8") if content_b64 else ""
    return json.loads(raw) if raw.strip() else []


def read_all() -> dict[str, list[dict]]:
    """Every collection's current rows, for hydrating a fresh container's local cache."""
    return {name: read_collection(name) for name in COLLECTIONS}


def _get_sha(collection: str) -> Optional[str]:
    resp = requests.get(
        _contents_url(collection), headers=_headers(),
        params={"ref": _branch()}, timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()["sha"]


def write_collection(collection: str, rows: list[dict], message: str) -> None:
    """Overwrite one collection's file with the full current row list, as one commit.

    Rows are plain dicts (e.g. dict(sqlite3.Row) from knowledge/store.py) -- this
    mirrors the SQLite table's current content, it doesn't transform it. Synchronous;
    callers that don't want a request to wait on this should go through queue_sync()
    instead.
    """
    content = json.dumps(rows, indent=2, sort_keys=True, default=str) + "\n"
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    body = {"message": message, "content": encoded, "branch": _branch()}
    sha = _get_sha(collection)
    if sha is not None:
        body["sha"] = sha
    resp = requests.put(_contents_url(collection), headers=_headers(), json=body, timeout=_REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Background write queue -- so a confirmed rule or "looks good" click never
# makes the user wait on a GitHub API round-trip.
# ---------------------------------------------------------------------------

_queue: "queue.Queue[tuple[str, list[dict], str]]" = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()

#: last outcome per collection, for /admin visibility -- see api/web_app.py's
#: status line. Deliberately simple (in-memory, not persisted) since it only
#: needs to answer "did the last sync work" for whoever is looking at /admin.
last_sync_status: dict[str, str] = {}


def _worker() -> None:
    while True:
        collection, rows, message = _queue.get()
        try:
            write_collection(collection, rows, message)
            last_sync_status[collection] = "ok"
        except Exception as exc:  # noqa: BLE001 -- a failed background commit must never crash the process
            _log.exception("Failed to commit %s to GitHub", collection)
            last_sync_status[collection] = f"failed: {exc}"
        finally:
            _queue.task_done()


def _ensure_worker() -> None:
    global _worker_started
    with _worker_lock:
        if not _worker_started:
            threading.Thread(target=_worker, daemon=True, name="git-store-writer").start()
            _worker_started = True


def queue_sync(collection: str, rows: list[dict], message: str) -> None:
    """Queue a background commit of one collection's full current content.

    No-op (logged, not raised) when GITHUB_TOKEN isn't set, so local dev and
    any environment without the token keeps working exactly as before this
    module existed -- durability is additive, never a hard requirement to
    serve a request.
    """
    if not is_configured():
        return
    _ensure_worker()
    _queue.put((collection, rows, message))
