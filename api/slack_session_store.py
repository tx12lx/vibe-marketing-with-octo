"""
api/slack_session_store.py -- Per-channel conversation state for the Slack interface.

Thin subclass of the generic SessionStore/SessionState, keyed by Slack channel ID
instead of Google Chat space name. Adds last_sender tracking so the audit log can
record who approved a result.

Upgrade path: swap the in-memory dict for a SQLite-backed store when the team grows
past ~10 concurrent users or when multi-process deployment is needed (same note as
the base session_store.py).
"""
from __future__ import annotations

from api.session_store import SessionState, SessionStore


class SlackSessionState(SessionState):
    """SessionState for a single Slack channel.

    last_sender (and reviewed_this_result) now live on the base SessionState
    itself -- this subclass no longer needs to redeclare them.
    """

    __slots__ = ()


class SlackSessionStore(SessionStore):
    """Thread-safe in-memory store for all active Slack channel sessions."""

    def get(self, channel_id: str) -> SlackSessionState:  # type: ignore[override]
        if channel_id not in self._sessions:
            self._sessions[channel_id] = SlackSessionState(channel_id)
        return self._sessions[channel_id]  # type: ignore[return-value]
