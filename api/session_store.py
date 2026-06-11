"""
api/session_store.py -- Per-space conversation state for the Google Chat interface.

Keyed by Google Chat space name (e.g. "spaces/AAA123").
Holds the last pipeline result and HITL state between the result card and the
user's button response.

Upgrade path: swap the in-memory dict for a SQLite-backed store when the team
grows past ~10 concurrent users or when multi-process deployment is needed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pydantic_schemas import BriefingOutput, IntentClassification, QuantAuditLog, UniversalJSONSpec


class SessionState:
    """Mutable state for one Google Chat space."""

    __slots__ = (
        "space_id",
        "last_spec",
        "last_audit_log",
        "last_brief",
        "last_intent",
        "last_query",
        "hitl_pending",
        "awaiting_correction",
    )

    def __init__(self, space_id: str) -> None:
        self.space_id: str = space_id
        self.last_spec: Optional["UniversalJSONSpec"] = None
        self.last_audit_log: Optional["QuantAuditLog"] = None
        self.last_brief: Optional["BriefingOutput"] = None
        self.last_intent: Optional["IntentClassification"] = None
        self.last_query: str = ""
        # True after a result card is posted, waiting for a HITL button click.
        self.hitl_pending: bool = False
        # True after the user clicked "Something's wrong"; next message is treated
        # as a free-text correction for FeedbackAgent.
        self.awaiting_correction: bool = False

    def store_result(
        self,
        query: str,
        intent: "IntentClassification",
        spec: Optional["UniversalJSONSpec"],
        log: Optional["QuantAuditLog"],
        brief: Optional["BriefingOutput"],
    ) -> None:
        self.last_query = query
        self.last_intent = intent
        self.last_spec = spec
        self.last_audit_log = log
        self.last_brief = brief
        self.hitl_pending = True
        self.awaiting_correction = False

    def clear_hitl(self) -> None:
        self.hitl_pending = False
        self.awaiting_correction = False


class SessionStore:
    """Thread-safe in-memory store for all active space sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def get(self, space_id: str) -> SessionState:
        if space_id not in self._sessions:
            self._sessions[space_id] = SessionState(space_id)
        return self._sessions[space_id]

    def clear(self, space_id: str) -> None:
        self._sessions.pop(space_id, None)
