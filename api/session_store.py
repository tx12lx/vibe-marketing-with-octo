"""
api/session_store.py -- Per-conversation state shared by the web chat and the
Slack bot (see api/slack_session_store.py for the Slack-specific subclass).

Keyed by a session id (a browser session id for the web UI, a Slack channel
id for Slack). Holds the last pipeline result and HITL state between the
result card and the user's button response.

Upgrade path: swap the in-memory dict for a SQLite-backed store when the team
grows past ~10 concurrent users or when multi-process deployment is needed.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pydantic_schemas import IntentClassification, QuantAuditLog


class SessionState:
    """Mutable state for one conversation."""

    __slots__ = (
        "space_id",
        "last_audit_log",
        "last_intent",
        "last_query",
        "last_sender",
        "hitl_pending",
        "reviewed_this_result",
        "awaiting_correction",
        "active_corrections",
        "pending_correction",
    )

    def __init__(self, space_id: str) -> None:
        self.space_id: str = space_id
        # Real identity of whoever is behind this session -- set from the
        # IAP-authenticated header on every request (see api/web_app.py's
        # _caller_identity()). Defaults to "unknown" rather than a placeholder
        # like "web" so a HITL confirmation or correction is never silently
        # attributed to the same fake identity for every different person.
        self.last_sender: str = "unknown"
        self.last_audit_log: Optional["QuantAuditLog"] = None
        self.last_intent: Optional["IntentClassification"] = None
        self.last_query: str = ""
        # True after a result card is posted, waiting for a HITL button click.
        self.hitl_pending: bool = False
        # True once the person has actually looked at the SQL behind the
        # current pending result (clicked "review"). For high-impact intents
        # (sizing_request) the server refuses a "yes" until this is true, so
        # approving without ever looking at the evidence isn't possible via
        # the API either, not just discouraged by the UI -- see
        # api/web_app.py's /hitl endpoint.
        self.reviewed_this_result: bool = False
        # True after the user clicked "Something's wrong"; next message is treated
        # as a free-text correction for FeedbackAgent.
        self.awaiting_correction: bool = False
        # Confirmed corrections from this session, injected into every subsequent query.
        self.active_corrections: list = []
        # Correction awaiting explicit user confirmation. Stored here between the
        # interpretation display and the "Yes, exactly right" click. Dict with keys:
        #   "text": str, "interpretation": str, "rules": list[BusinessRule]
        self.pending_correction: dict = {}

    def store_result(
        self,
        query: str,
        intent: "IntentClassification",
        log: Optional["QuantAuditLog"],
    ) -> None:
        self.last_query = query
        self.last_intent = intent
        self.last_audit_log = log
        self.hitl_pending = True
        self.reviewed_this_result = False
        self.awaiting_correction = False

    def clear_hitl(self) -> None:
        self.hitl_pending = False
        self.reviewed_this_result = False
        self.awaiting_correction = False


class SessionStore:
    """Thread-safe in-memory store for all active sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def get(self, space_id: str) -> SessionState:
        if space_id not in self._sessions:
            self._sessions[space_id] = SessionState(space_id)
        return self._sessions[space_id]

    def clear(self, space_id: str) -> None:
        self._sessions.pop(space_id, None)
