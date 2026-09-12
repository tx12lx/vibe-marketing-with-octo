"""knowledge/store.py -- the single SQLite database behind the new knowledge layer.

One connection helper, one schema, and write functions that never silently lose
data: replacing a table's columns first checks the new count against what was
already stored, and refuses (raises) rather than quietly accepting a sync that
looks like it dropped most of what was there before. Business rules and
feedback events are append-only -- a correction never overwrites history, it
supersedes it with a pointer back to what it replaced.

Storage location: one file on disk (KNOWLEDGE_DB_PATH, defaulting to
knowledge/data/vibe_octo_knowledge.db). The same table shapes move into a
BigQuery dataset later without a redesign -- nothing here relies on a
SQLite-only feature.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

_ROOT_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _ROOT_DIR / "knowledge" / "data" / "vibe_octo_knowledge.db"

# A resync is only trusted if the new column count is at least this fraction of
# the previous one (when there was a previous one). This is the concrete guard
# against the old system's silent data-loss bug.
_MIN_RETAINED_FRACTION = 0.5


class IntegrityError(RuntimeError):
    """Raised when a write looks like it would silently lose previously-stored knowledge."""


def _db_path() -> Path:
    override = os.getenv("KNOWLEDGE_DB_PATH")
    return Path(override) if override else _DEFAULT_DB_PATH


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tables (
    project     TEXT NOT NULL,
    dataset     TEXT NOT NULL,
    table_name  TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    synced_at   TEXT NOT NULL,
    PRIMARY KEY (project, dataset, table_name)
);

CREATE TABLE IF NOT EXISTS columns (
    project      TEXT NOT NULL,
    dataset      TEXT NOT NULL,
    table_name   TEXT NOT NULL,
    column_name  TEXT NOT NULL,
    data_type    TEXT NOT NULL,
    mode         TEXT NOT NULL DEFAULT '',
    sensitivity  TEXT NOT NULL DEFAULT 'none',  -- 'hidden' | 'filter_only' | 'none'
    description  TEXT NOT NULL DEFAULT '',
    description_source TEXT NOT NULL DEFAULT '',  -- 'bigquery' | 'ai_generated' | 'human' | ''
    value_notes  TEXT NOT NULL DEFAULT '',  -- human-confirmed notes on what values this column actually holds
    synced_at    TEXT NOT NULL,
    PRIMARY KEY (project, dataset, table_name, column_name)
);

CREATE TABLE IF NOT EXISTS glossary_terms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    term        TEXT NOT NULL,
    definition  TEXT NOT NULL,
    added_by    TEXT NOT NULL DEFAULT 'unknown',
    added_at    TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'seed'  -- 'seed' | 'hitl'
);

CREATE TABLE IF NOT EXISTS business_rules (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_text      TEXT NOT NULL,
    scope          TEXT NOT NULL DEFAULT 'universal',  -- 'table' | 'pattern' | 'universal'
    project        TEXT,  -- set when scope='table': which table this rule applies to
    dataset        TEXT,
    table_name     TEXT,
    added_by       TEXT NOT NULL DEFAULT 'unknown',
    added_at       TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'pending_review' | 'rejected' | 'retired'
    superseded_by  INTEGER,
    approved_by    TEXT,  -- who moved this out of pending_review (must differ from added_by -- enforced in code)
    approved_at    TEXT,
    review_note    TEXT
);

CREATE TABLE IF NOT EXISTS feedback_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type         TEXT NOT NULL,  -- 'confirm' | 'correction'
    raw_text           TEXT NOT NULL DEFAULT '',
    structured_rule_id INTEGER,
    user_identity      TEXT NOT NULL DEFAULT 'unknown',
    created_at         TEXT NOT NULL
);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_BUSINESS_RULES_MIGRATION_COLUMNS = (
    "project", "dataset", "table_name", "approved_by", "approved_at", "review_note",
)


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an already-existing database up to the current shape. Every step
    here is idempotent -- safe to run on every connection, including one
    against a database that's already current (each check no-ops).

    2026-09-11: dropped campaign_code from business_rules/feedback_events and
    the whole campaign_summaries table -- the campaign-tier scaffolding this
    supported was removed as dead weight (see core/ai_client.py's and
    agents/nexus_agent.py's module docstrings). Requires SQLite 3.35+ for
    DROP COLUMN (bundled with Python 3.11+ is well past that)."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(business_rules)")}
    for column in _BUSINESS_RULES_MIGRATION_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE business_rules ADD COLUMN {column} TEXT")

    if "campaign_code" in existing:
        conn.execute("ALTER TABLE business_rules DROP COLUMN campaign_code")

    feedback_columns = {row["name"] for row in conn.execute("PRAGMA table_info(feedback_events)")}
    if "campaign_code" in feedback_columns:
        conn.execute("ALTER TABLE feedback_events DROP COLUMN campaign_code")

    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='campaign_summaries'"
    ).fetchone():
        conn.execute("DROP TABLE campaign_summaries")

    conn.commit()


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    path = db_path or _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.commit()
    _migrate(conn)
    return conn


@contextmanager
def connect(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    conn = get_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tables / columns -- synced from live BigQuery schema
# ---------------------------------------------------------------------------

def get_column_count(conn: sqlite3.Connection, project: str, dataset: str, table_name: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM columns WHERE project=? AND dataset=? AND table_name=?",
        (project, dataset, table_name),
    ).fetchone()
    return row["n"] if row else 0


def get_human_confirmed_columns(conn: sqlite3.Connection, project: str, dataset: str, table_name: str) -> dict:
    """Every column in this table a human has personally confirmed, keyed by column name.

    A schema re-sync must never silently overwrite these -- same principle as
    business rules being append-only rather than blindly replaced.
    """
    rows = conn.execute(
        "SELECT column_name, description, description_source, value_notes FROM columns "
        "WHERE project=? AND dataset=? AND table_name=? AND description_source='human'",
        (project, dataset, table_name),
    ).fetchall()
    return {r["column_name"]: dict(r) for r in rows}


def replace_table_columns(
    conn: sqlite3.Connection,
    project: str,
    dataset: str,
    table_name: str,
    table_description: str,
    columns: list[dict],
) -> None:
    """Atomically replace one table's stored columns, refusing a suspicious drop.

    ``columns`` is a list of dicts with keys: name, data_type, mode,
    sensitivity, description, description_source, value_notes. Raises
    IntegrityError instead of writing when the new column count looks like
    silent data loss compared to what was already stored -- this is the
    concrete fix for the old system's silent-overwrite bug. Any column a human
    has personally confirmed (description_source='human') keeps that
    human-provided description and value_notes across the re-sync, even if
    this fresh fetch would otherwise overwrite it.
    """
    previous_count = get_column_count(conn, project, dataset, table_name)
    new_count = len(columns)

    if previous_count > 0 and new_count < previous_count * _MIN_RETAINED_FRACTION:
        raise IntegrityError(
            f"Refusing to sync {project}.{dataset}.{table_name}: it previously had "
            f"{previous_count} columns stored, but this sync only found {new_count} -- "
            "that looks like a partial or broken schema fetch, not a real change."
        )
    if new_count == 0:
        raise IntegrityError(
            f"Refusing to sync {project}.{dataset}.{table_name}: no columns were found at all."
        )

    human_confirmed = get_human_confirmed_columns(conn, project, dataset, table_name)
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO tables (project, dataset, table_name, description, synced_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(project, dataset, table_name) DO UPDATE SET "
            "description=excluded.description, synced_at=excluded.synced_at",
            (project, dataset, table_name, table_description, now),
        )
        conn.execute(
            "DELETE FROM columns WHERE project=? AND dataset=? AND table_name=?",
            (project, dataset, table_name),
        )
        rows = []
        for c in columns:
            confirmed = human_confirmed.get(c["name"])
            if confirmed is not None:
                description = confirmed["description"]
                description_source = "human"
                value_notes = confirmed["value_notes"]
            else:
                description = c.get("description", "")
                description_source = c.get("description_source", "")
                value_notes = c.get("value_notes", "")
            rows.append((
                project, dataset, table_name,
                c["name"], c["data_type"], c.get("mode", ""),
                c.get("sensitivity", "none"), description, description_source, value_notes, now,
            ))
        conn.executemany(
            "INSERT INTO columns "
            "(project, dataset, table_name, column_name, data_type, mode, sensitivity, "
            "description, description_source, value_notes, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def confirm_column(
    conn: sqlite3.Connection,
    project: str,
    dataset: str,
    table_name: str,
    column_name: str,
    description: str,
    value_notes: str = "",
) -> None:
    """Record a human-confirmed answer for one column. Marked description_source='human'
    so a future schema re-sync (replace_table_columns) never overwrites it."""
    with conn:
        conn.execute(
            "UPDATE columns SET description=?, description_source='human', value_notes=?, synced_at=? "
            "WHERE project=? AND dataset=? AND table_name=? AND column_name=?",
            (description, value_notes, _now(), project, dataset, table_name, column_name),
        )


def get_table_schema_rows(conn: sqlite3.Connection, table_names: Optional[list[str]] = None) -> list[sqlite3.Row]:
    """Every stored table with its columns as one row per column, joined."""
    if table_names:
        placeholders = ",".join("?" for _ in table_names)
        query = (
            "SELECT t.project, t.dataset, t.table_name, t.description AS table_description, "
            "c.column_name, c.data_type, c.mode, c.sensitivity, c.description AS column_description, "
            "c.description_source, c.value_notes "
            "FROM tables t JOIN columns c "
            "ON t.project=c.project AND t.dataset=c.dataset AND t.table_name=c.table_name "
            f"WHERE t.table_name IN ({placeholders}) "
            "ORDER BY t.table_name, c.column_name"
        )
        return conn.execute(query, table_names).fetchall()
    return conn.execute(
        "SELECT t.project, t.dataset, t.table_name, t.description AS table_description, "
        "c.column_name, c.data_type, c.mode, c.sensitivity, c.description AS column_description, "
        "c.description_source, c.value_notes "
        "FROM tables t JOIN columns c "
        "ON t.project=c.project AND t.dataset=c.dataset AND t.table_name=c.table_name "
        "ORDER BY t.table_name, c.column_name"
    ).fetchall()


# ---------------------------------------------------------------------------
# Glossary
# ---------------------------------------------------------------------------

def add_glossary_term(conn: sqlite3.Connection, term: str, definition: str, added_by: str = "unknown", source: str = "seed") -> None:
    with conn:
        conn.execute(
            "INSERT INTO glossary_terms (term, definition, added_by, added_at, source) VALUES (?, ?, ?, ?, ?)",
            (term, definition, added_by, _now(), source),
        )


def get_glossary_terms(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM glossary_terms ORDER BY term").fetchall()


# ---------------------------------------------------------------------------
# Business rules -- append-only, corrections supersede rather than overwrite
# ---------------------------------------------------------------------------

def add_business_rule(
    conn: sqlite3.Connection,
    rule_text: str,
    scope: str = "universal",
    project: Optional[str] = None,
    dataset: Optional[str] = None,
    table_name: Optional[str] = None,
    added_by: str = "unknown",
    supersedes_id: Optional[int] = None,
    status: str = "active",
) -> int:
    """scope='table' rules are contained to one table (project/dataset/table_name) so a
    rule written for one table's conventions (e.g. its default sizing filters) can never
    leak into a different table's query just because both rules are 'active'.

    status defaults to 'active'. Callers pass status='pending_review' for a
    rule whose scope requires a second reviewer's approval -- see
    knowledge/context.py's _SCOPES_REQUIRING_REVIEW -- so it sits inert
    (get_active_rules() only ever returns status='active') until a second
    person calls approve_rule().
    """
    with conn:
        cur = conn.execute(
            "INSERT INTO business_rules "
            "(rule_text, scope, project, dataset, table_name, added_by, added_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (rule_text, scope, project, dataset, table_name, added_by, _now(), status),
        )
        new_id = cur.lastrowid
        if supersedes_id is not None:
            conn.execute(
                "UPDATE business_rules SET status='retired', superseded_by=? WHERE id=?",
                (new_id, supersedes_id),
            )
        return new_id


class MakerCheckerViolation(RuntimeError):
    """Raised when the same identity that submitted a rule tries to approve it."""


def get_pending_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every rule staged for a second reviewer's approval, oldest first."""
    return conn.execute(
        "SELECT * FROM business_rules WHERE status='pending_review' ORDER BY added_at"
    ).fetchall()


def get_rule(conn: sqlite3.Connection, rule_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM business_rules WHERE id=?", (rule_id,)).fetchone()


def approve_rule(
    conn: sqlite3.Connection,
    rule_id: int,
    approved_by: str,
    note: str = "",
) -> None:
    """Move a pending_review rule to active. Refuses (raises) when the approver
    is the same identity that submitted it -- maker-checker enforced in code,
    not just left as a convention someone could forget to follow."""
    row = get_rule(conn, rule_id)
    if row is None:
        raise ValueError(f"No business rule with id {rule_id}.")
    if row["status"] != "pending_review":
        raise ValueError(f"Rule {rule_id} is not pending review (status={row['status']!r}).")
    if (row["added_by"] or "").strip().lower() == (approved_by or "").strip().lower():
        raise MakerCheckerViolation(
            f"Rule {rule_id} was submitted by {row['added_by']!r} -- it must be approved "
            "by someone else, not the same person who submitted it."
        )
    with conn:
        conn.execute(
            "UPDATE business_rules SET status='active', approved_by=?, approved_at=?, review_note=? "
            "WHERE id=?",
            (approved_by, _now(), note, rule_id),
        )


def reject_rule(
    conn: sqlite3.Connection,
    rule_id: int,
    approved_by: str,
    note: str = "",
) -> None:
    """Move a pending_review rule to rejected -- it never becomes active."""
    row = get_rule(conn, rule_id)
    if row is None:
        raise ValueError(f"No business rule with id {rule_id}.")
    if row["status"] != "pending_review":
        raise ValueError(f"Rule {rule_id} is not pending review (status={row['status']!r}).")
    with conn:
        conn.execute(
            "UPDATE business_rules SET status='rejected', approved_by=?, approved_at=?, review_note=? "
            "WHERE id=?",
            (approved_by, _now(), note, rule_id),
        )


def get_all_business_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every business rule regardless of status -- for durable backup (see
    knowledge/git_store.py), not for grounding a prompt (use get_active_rules for that)."""
    return conn.execute("SELECT * FROM business_rules ORDER BY id").fetchall()


def get_active_rules(
    conn: sqlite3.Connection,
    scope: Optional[str] = None,
    table_name: Optional[str] = None,
) -> list[sqlite3.Row]:
    """table_name filters to that table's own scope='table' rules, plus every rule that
    isn't table-scoped at all (universal/pattern) -- a table-scoped rule for a different
    table is never returned."""
    query = "SELECT * FROM business_rules WHERE status='active'"
    params: list = []
    if scope is not None:
        query += " AND scope=?"
        params.append(scope)
    if table_name is not None:
        query += " AND (table_name=? OR table_name IS NULL)"
        params.append(table_name)
    query += " ORDER BY added_at"
    return conn.execute(query, params).fetchall()


# ---------------------------------------------------------------------------
# Feedback events -- permanent, append-only record of every confirm/correction
# ---------------------------------------------------------------------------

def record_feedback_event(
    conn: sqlite3.Connection,
    event_type: str,
    raw_text: str = "",
    structured_rule_id: Optional[int] = None,
    user_identity: str = "unknown",
) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO feedback_events "
            "(event_type, raw_text, structured_rule_id, user_identity, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_type, raw_text, structured_rule_id, user_identity, _now()),
        )
        return cur.lastrowid


def get_feedback_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM feedback_events ORDER BY created_at").fetchall()


# ---------------------------------------------------------------------------
# Hydration from durable backup -- knowledge/git_store.py holds the durable
# copy of everything in this section (business rules, glossary, feedback); a
# fresh container calls these once at startup to rebuild its local cache from
# it. Safe to call repeatedly (INSERT OR REPLACE keyed on id) since a fresh
# container's tables start empty.
# ---------------------------------------------------------------------------

def hydrate_business_rules(conn: sqlite3.Connection, rows: list[dict]) -> None:
    with conn:
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO business_rules "
                "(id, rule_text, scope, project, dataset, table_name, "
                "added_by, added_at, status, superseded_by, approved_by, approved_at, review_note) "
                "VALUES (:id, :rule_text, :scope, :project, :dataset, :table_name, "
                ":added_by, :added_at, :status, :superseded_by, :approved_by, :approved_at, :review_note)",
                r,
            )


def hydrate_glossary_terms(conn: sqlite3.Connection, rows: list[dict]) -> None:
    with conn:
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO glossary_terms (id, term, definition, added_by, added_at, source) "
                "VALUES (:id, :term, :definition, :added_by, :added_at, :source)",
                r,
            )


def hydrate_feedback_events(conn: sqlite3.Connection, rows: list[dict]) -> None:
    with conn:
        for r in rows:
            conn.execute(
                "INSERT OR REPLACE INTO feedback_events "
                "(id, event_type, raw_text, structured_rule_id, user_identity, created_at) "
                "VALUES (:id, :event_type, :raw_text, :structured_rule_id, :user_identity, :created_at)",
                r,
            )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def check_health(db_path: Optional[Path] = None) -> tuple[str, str]:
    """Real health check for core/resilience.py's startup check -- can the
    database be opened, and does it have the tables and data it should."""
    try:
        with connect(db_path) as conn:
            table_count = conn.execute("SELECT COUNT(*) AS n FROM tables").fetchone()["n"]
            column_count = conn.execute("SELECT COUNT(*) AS n FROM columns").fetchone()["n"]
        if table_count == 0:
            return "WARN", "The knowledge layer's database is set up but has no synced tables yet -- run the schema sync."
        return "OK", f"Knowledge layer OK -- {table_count} table(s), {column_count} column(s) synced."
    except Exception as exc:  # noqa: BLE001 -- surfacing any failure as a plain-English health check result
        return "FAIL", f"The knowledge layer's database could not be opened: {exc}"
