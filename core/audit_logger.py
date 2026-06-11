"""
core/audit_logger.py -- Structured per-request audit log for compliance tracking.

Appends one JSONL entry per request to logs/audit_YYYY-MM-DD.jsonl.
No PII: SQL is stored as SHA-256 hash only; no query results or row data are written.
logs/ is .gitignore'd — ready for compliance review when legal engagement begins.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_DEFAULT_LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

# Canonical hitl_outcome values — use these constants for consistency
HITL_YES = "yes"
HITL_NO = "no"
HITL_REVIEW_YES = "review_yes"
HITL_REVIEW_NO = "review_no"
HITL_PENDING = None  # API mode: HITL not yet resolved


def _sql_hash(sql: str) -> str:
    return "sha256:" + hashlib.sha256(sql.encode()).hexdigest()


class AuditLogger:
    """Append-only JSONL audit logger.

    One instance per process. Each call to log() appends a single JSON line to
    logs/audit_YYYY-MM-DD.jsonl (rotated daily). File I/O is synchronous and
    suitable for single-process use (write() on a short line is atomic on Linux
    and Windows for the typical log-line sizes here).
    """

    def __init__(self, logs_dir: Path = _DEFAULT_LOGS_DIR) -> None:
        self._logs_dir = Path(logs_dir)
        self._logs_dir.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        *,
        session_id: str,
        user: str,
        intent_type: str,
        campaign_id: Optional[str],
        sql: Optional[str],
        agent_called: Optional[str],
        hitl_outcome: Optional[str],
        duration_ms: int,
    ) -> None:
        """Append one audit entry to today's JSONL log file.

        PII policy: sql is stored as SHA-256 hash only; the raw SQL string
        is never written to disk.
        """
        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        log_path = self._logs_dir / f"audit_{date_str}.jsonl"

        entry: dict = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "session_id": session_id,
            "user": user,
            "intent_type": intent_type,
            "campaign_id": campaign_id,
            "bq_query_hash": _sql_hash(sql) if sql else None,
            "agent_called": agent_called,
            "hitl_outcome": hitl_outcome,
            "duration_ms": duration_ms,
        }

        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def log_hitl_resolution(
        self,
        *,
        session_id: str,
        user: str,
        campaign_id: Optional[str],
        hitl_outcome: str,
    ) -> None:
        """Append a HITL resolution entry (API mode only).

        In terminal mode the HITL outcome is known before log() is called, so
        this is only needed in async API mode where HITL button clicks arrive
        after the pipeline entry has already been written.
        """
        date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
        log_path = self._logs_dir / f"audit_{date_str}.jsonl"

        entry: dict = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "session_id": session_id,
            "user": user,
            "intent_type": "hitl_resolution",
            "campaign_id": campaign_id,
            "bq_query_hash": None,
            "agent_called": None,
            "hitl_outcome": hitl_outcome,
            "duration_ms": 0,
        }

        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
