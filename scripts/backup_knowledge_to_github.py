"""scripts/backup_knowledge_to_github.py -- weekly durability backup + code
drift check.

The VM running Vibe OCTO cannot reach GitHub itself: its network only allows
outbound traffic to Google's own APIs, so every write attempt from the app's
own runtime to api.github.com times out silently (confirmed 2026-09-11, see
knowledge/git_store.py's docstring and the service logs). That's a network
boundary neither the app owner nor Claude can grant themselves, so it isn't
being routed around.

This script is the workaround: it runs from a machine with normal internet
access (this one, on a schedule -- see the Windows Task Scheduler job
"VibeOctoWeeklyBackup", configured to catch up and run as soon as this laptop
is next on if the exact weekly moment is missed), pulls the VM's current
knowledge database down over the same gcloud/IAP connection already used to
deploy and inspect the VM, and pushes each collection to GitHub directly from
here using knowledge/git_store's own write_collection() -- the same
durable-storage code the app itself would use if it could reach GitHub, so
there is exactly one implementation of "what a GitHub-backed knowledge write
looks like," not two.

This is deliberately a once-a-week backup, not a live mirror -- that's an
accepted trade-off (see the plan this was built from), so the bar for this
script isn't speed, it's that it never fails quietly:

- Every run's outcome (success or problem, per collection) is printed and
  appended to backup_log.txt next to this script, same as always.
- A single-line current status is written to backup_status.txt (overwritten
  each run), so "did the last backup actually succeed, and when" is a one-line
  read instead of scrolling through a growing log.
- Any problem also triggers an on-screen Windows notification immediately --
  not something anyone has to go looking for. Nothing is sent on a normal,
  fully successful run, so the notification only ever means "look at this."

It also now checks a second thing every run: whether the app's own code, as
it's actually running on the VM, still matches what's saved on GitHub. This
reuses the exact same VM connection already opened for the knowledge backup,
so it's one weekly routine covering both things that are supposed to match
GitHub -- the learned knowledge and the code -- not two separate systems.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import xml.sax.saxutils as saxutils
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
_REMOTE_APP_DIR = "/home/tian_xia_telus_com/vibe-octo"
_REMOTE_DB_PATH = f"{_REMOTE_APP_DIR}/knowledge/data/vibe_octo_knowledge.db"
_LOG_PATH = _SCRIPT_DIR / "backup_log.txt"
_STATUS_PATH = _SCRIPT_DIR / "backup_status.txt"

# Which files on the VM are expected to match GitHub -- deliberately just the
# app's own first-party code (the same packages pyproject.toml's own
# [tool.setuptools.packages.find] declares), not the whole repo. Excludes:
#   - anything under */_relay/ or teammate_gateway/ (each is its own separate
#     Cloud Run service, never deployed to this VM at all)
#   - web_cloud_run.py (a *different* Cloud Run deployment of the web app,
#     also never on this VM)
#   - update_personal_project.py (a laptop-only setup helper)
#   - knowledge/data/ (the live runtime database this same script already
#     backs up above -- comparing it here would be comparing a moving target)
_CODE_SCOPE_DIRS = ("core/", "agents/", "api/", "knowledge/")
_CODE_SCOPE_ROOT_FILES = {"pydantic_schemas.py", "vibe_orchestrator.py"}


def _log(message: str) -> None:
    line = f"{datetime.datetime.now().isoformat(timespec='seconds')} {message}"
    print(line)
    with _LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _oneline(text: str) -> str:
    """Collapses any error message (some of which, like gcloud's own auth
    errors, span several lines with blank lines and follow-up instructions)
    down to one line, so the status file keeps its "one line to read" promise
    no matter what the underlying problem looks like."""
    return " ".join(text.split())


def _write_status(ok: bool, summary: str) -> None:
    line = f"{'OK' if ok else 'FAILED'} {datetime.datetime.now().isoformat(timespec='seconds')} -- {_oneline(summary)}\n"
    _STATUS_PATH.write_text(line, encoding="utf-8")


def _notify(title: str, message: str) -> None:
    """Best-effort only -- a broken notification must never break the backup
    run itself, since the whole point is surfacing the real problem, not
    adding a second one. Uses Windows' own built-in toast API (no extra
    package to install) via a short PowerShell call."""
    title_esc = saxutils.escape(title)
    message_esc = saxutils.escape(message)
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null\n"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] > $null\n"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument\n"
        "$xml.LoadXml(@'\n"
        f"<toast><visual><binding template=\"ToastGeneric\">"
        f"<text>{title_esc}</text><text>{message_esc}</text>"
        "</binding></visual></toast>\n"
        "'@)\n"
        "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml\n"
        '[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Vibe OCTO Backup").Show($toast)\n'
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=15)
    except Exception:
        pass


def _pull_db(local_path: Path) -> None:
    cmd = (
        f'gcloud compute scp "{_VM_NAME}:{_REMOTE_DB_PATH}" "{local_path}" '
        f'--zone={_ZONE} --project={_PROJECT} --tunnel-through-iap'
    )
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, shell=True)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout)[:800])


def _code_scope_paths() -> list[str]:
    """Every file GitHub currently has that's supposed to also be on the VM,
    derived from git itself (so this list can never quietly drift from what's
    actually tracked) rather than a hand-maintained list."""
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "origin/master"],
        cwd=_ROOT_DIR, capture_output=True, text=True, timeout=30, check=True,
    )
    tracked = result.stdout.splitlines()
    return sorted(
        path for path in tracked
        if path in _CODE_SCOPE_ROOT_FILES
        or (path.startswith(_CODE_SCOPE_DIRS) and not path.startswith("knowledge/data/"))
    )


def _github_contents(paths: list[str]) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    for path in paths:
        result = subprocess.run(
            ["git", "show", f"origin/master:{path}"],
            cwd=_ROOT_DIR, capture_output=True, timeout=30,
        )
        if result.returncode == 0:
            contents[path] = result.stdout
    return contents


def _vm_hashes(paths: list[str]) -> dict[str, str]:
    quoted = " ".join(f"'{p}'" for p in paths)
    cmd = (
        f'gcloud compute ssh {_VM_NAME} --zone={_ZONE} --project={_PROJECT} '
        f'--tunnel-through-iap --command="cd {_REMOTE_APP_DIR} && sha256sum {quoted} 2>&1"'
    )
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=90, shell=True)
    hashes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and len(parts[0]) == 64:
            digest, path = parts
            hashes[path] = digest
    if not hashes and paths:
        # Nothing parsed at all -- almost certainly the VM was never actually
        # reached (a dropped connection, an expired gcloud login, etc.), not
        # "every single file happens to be missing." Surface the real
        # problem instead of a wall of misleading per-file mismatches.
        raise RuntimeError((result.stderr or result.stdout or "no response from the VM")[:500])
    return hashes


def _normalize_line_endings(content: bytes) -> bytes:
    return content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _classify_mismatch(path: str, github_content: bytes) -> str:
    """A raw byte mismatch can mean two very different things: the code
    itself is genuinely different, or the two copies are the same code saved
    with Windows-style (CRLF) vs Linux-style (LF) line endings -- harmless,
    but still worth reporting precisely rather than lumping it in with a real
    content difference and causing needless alarm."""
    cmd = (
        f'gcloud compute ssh {_VM_NAME} --zone={_ZONE} --project={_PROJECT} '
        f'--tunnel-through-iap --command="cat \'{_REMOTE_APP_DIR}/{path}\'"'
    )
    result = subprocess.run(cmd, capture_output=True, timeout=30, shell=True)
    if _normalize_line_endings(github_content) == _normalize_line_endings(result.stdout):
        return f"{path}: only differs by line-ending style (Windows vs Linux), not actual content"
    return f"{path}: VM copy has genuinely different content from GitHub"


def _check_code_drift() -> tuple[bool, list[str]]:
    """Compares the VM's actual deployed code against GitHub's current
    origin/master. Returns (everything matches, list of plain-English
    problems) -- never raises, so one broken check never hides the other."""
    try:
        subprocess.run(
            ["git", "fetch", "origin", "master"],
            cwd=_ROOT_DIR, capture_output=True, timeout=60, check=True,
        )
    except Exception as exc:
        return False, [f"could not check GitHub's latest code: {exc}"]

    try:
        paths = _code_scope_paths()
    except Exception as exc:
        return False, [f"could not list GitHub's tracked files: {exc}"]

    github_contents = _github_contents(paths)
    github_hashes = {p: hashlib.sha256(c).hexdigest() for p, c in github_contents.items()}
    try:
        vm_hashes = _vm_hashes(paths)
    except Exception as exc:
        return False, [f"could not read the VM's code: {exc}"]

    problems: list[str] = []
    for path in paths:
        gh = github_hashes.get(path)
        vm = vm_hashes.get(path)
        if gh is None:
            problems.append(f"{path}: could not read from GitHub")
        elif vm is None:
            problems.append(f"{path}: missing on the VM")
        elif gh != vm:
            problems.append(_classify_mismatch(path, github_contents[path]))
    return (len(problems) == 0), problems


def main() -> int:
    _log("=== Weekly knowledge backup + code check starting ===")
    problems: list[str] = []

    if not is_configured():
        _log("ABORTED knowledge backup: GITHUB_TOKEN is not set in this environment.")
        problems.append("GITHUB_TOKEN is not set, so nothing could be backed up")
    else:
        with tempfile.TemporaryDirectory() as tmp:
            local_db = Path(tmp) / "vibe_octo_knowledge.db"
            try:
                _pull_db(local_db)
            except Exception as exc:
                _log(f"FAILED to pull the database from the VM: {exc}")
                problems.append(f"could not reach the VM to back up its knowledge: {exc}")
            else:
                conn = sqlite3.connect(str(local_db))
                conn.row_factory = sqlite3.Row
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
                        problems.append(f"{collection}: failed to save to GitHub ({exc})")
                        _log(f"FAILED to sync {collection}: {exc}")
                conn.close()

    code_ok, code_problems = _check_code_drift()
    if code_ok:
        _log("OK: the VM's code matches GitHub.")
    else:
        for p in code_problems:
            _log(f"CODE MISMATCH: {p}")
        problems.extend(f"code out of sync -- {p}" for p in code_problems)

    all_ok = not problems
    summary = "knowledge backup and code check both passed" if all_ok else "; ".join(_oneline(p) for p in problems)
    _write_status(all_ok, summary)

    if not all_ok:
        _log(f"=== Weekly run completed WITH PROBLEMS: {summary} ===")
        _notify("Vibe OCTO backup needs attention", summary[:250])
        return 1

    _log("=== Weekly run completed successfully (knowledge backup + code check) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
