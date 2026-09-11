"""scripts/backup_knowledge_to_github.py -- weekly durability backup.

The VM running Vibe OCTO cannot reach GitHub itself: its network only allows
outbound traffic to Google's own APIs, so every write attempt from the app's
own runtime to api.github.com times out silently (confirmed 2026-09-11, see
knowledge/git_store.py's docstring and the service logs). That's a network
boundary neither the app owner nor Claude can grant themselves, so it isn't
being routed around.

This script is the workaround: it runs from a machine with normal internet
access (this one, on a schedule -- see the Windows Task Scheduler job
"VibeOctoWeeklyBackup"), pulls the VM's current knowledge database down over
the same gcloud/IAP connection already used to deploy and inspect the VM, and
pushes each collection to GitHub directly from here using knowledge/git_store's
own write_collection() -- the same durable-storage code the app itself would
use if it could reach GitHub, so there is exactly one implementation of "what
a GitHub-backed knowledge write looks like," not two.

Never fails silently: every run's outcome (success or failure, per collection)
is printed and appended to backup_log.txt next to this script. The whole
reason this script exists is that a silent GitHub-sync failure was already the
problem once; this must not become a second instance of the same mistake.
"""
from __future__ import annotations

import datetime
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _SCRIPT_DIR.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

# The token lives in a plain file outside this git repo (never in an env var or
# anything checked in) -- see the setup note in this repo's README/CLAUDE.md.
# Loaded before importing knowledge.git_store so its os.getenv("GITHUB_TOKEN")
# reads see it, without needing any system-level environment configuration.
_TOKEN_FILE = Path.home() / ".vibe_octo_secrets" / "github_token.txt"
if not os.getenv("GITHUB_TOKEN") and _TOKEN_FILE.exists():
    os.environ["GITHUB_TOKEN"] = _TOKEN_FILE.read_text(encoding="utf-8").strip()

from knowledge.git_store import COLLECTIONS, is_configured, write_collection  # noqa: E402

_VM_NAME = "bq-test-vm2"
_ZONE = "northamerica-northeast1-a"
_PROJECT = "cdo-hsm-adobe-fda-np-9fbb44"
_REMOTE_DB_PATH = "/home/tian_xia_telus_com/vibe-octo/knowledge/data/vibe_octo_knowledge.db"
_LOG_PATH = _SCRIPT_DIR / "backup_log.txt"


def _log(message: str) -> None:
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {message}"
    print(line)
    with _LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _pull_db(local_path: Path) -> None:
    cmd = (
        f'gcloud compute scp "{_VM_NAME}:{_REMOTE_DB_PATH}" "{local_path}" '
        f'--zone={_ZONE} --project={_PROJECT} --tunnel-through-iap'
    )
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, shell=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout)[:800])


def main() -> int:
    _log("=== Weekly knowledge backup starting ===")

    if not is_configured():
        _log("ABORTED: GITHUB_TOKEN is not set in this environment.")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        local_db = Path(tmp) / "vibe_octo_knowledge.db"
        try:
            _pull_db(local_db)
        except Exception as exc:
            _log(f"FAILED to pull the database from the VM: {exc}")
            return 1

        conn = sqlite3.connect(str(local_db))
        conn.row_factory = sqlite3.Row
        failures: list[str] = []
        for collection in COLLECTIONS:
            try:
                rows = [dict(r) for r in conn.execute(f"SELECT * FROM {collection}").fetchall()]
            except sqlite3.OperationalError as exc:
                _log(f"SKIPPED {collection}: table not present ({exc})")
                continue
            try:
                write_collection(
                    collection, rows,
                    message=f"Weekly automated backup: {len(rows)} {collection} row(s)",
                )
                _log(f"OK: synced {len(rows)} row(s) in {collection}")
            except Exception as exc:
                failures.append(collection)
                _log(f"FAILED to sync {collection}: {exc}")
        conn.close()

    if failures:
        _log(f"=== Backup completed WITH FAILURES: {failures} ===")
        return 1
    _log("=== Backup completed successfully ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
