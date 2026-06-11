"""
api/progress_sender.py -- Post and update status messages in a Google Chat space.

Used when the API server is configured with a Chat service account and can make
outbound API calls.  In the default sync-response mode (no service account),
the server responds directly to the webhook and this class is not invoked.

Usage:
    sender = ProgressSender(chat_service, "spaces/AAA123")
    sender.send("Analyzing your request...")          # creates a new message
    sender.update("Building your audience query...")  # updates that same message
    sender.done()                                     # resets for next request
"""
from __future__ import annotations

import logging
from typing import Optional

_log = logging.getLogger(__name__)


class ProgressSender:
    """Post and in-place-update a single status message in a Google Chat space."""

    def __init__(self, service, space_name: str) -> None:
        self._service = service
        self._space_name = space_name
        self._message_name: Optional[str] = None

    def send(self, text: str) -> None:
        """Create a new text message in the space and remember its resource name."""
        try:
            msg = (
                self._service.spaces()
                .messages()
                .create(parent=self._space_name, body={"text": text})
                .execute()
            )
            self._message_name = msg.get("name")
        except Exception as exc:
            _log.warning("ProgressSender.send failed: %s", exc)

    def update(self, text: str) -> None:
        """Update the previously created message in place.

        Falls back to send() if no prior message exists.
        """
        if not self._message_name:
            self.send(text)
            return
        try:
            self._service.spaces().messages().update(
                name=self._message_name,
                updateMask="text",
                body={"text": text},
            ).execute()
        except Exception as exc:
            _log.warning("ProgressSender.update failed: %s", exc)

    def done(self) -> None:
        """Reset so the next send() creates a fresh message."""
        self._message_name = None
