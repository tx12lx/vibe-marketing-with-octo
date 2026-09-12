"""slack_relay/main.py -- lets teammates use the tool from Slack, with no web
link, no sign-in page, and no new GCP permission of any kind.

Why this is different from teammate_gateway: that service has to accept
inbound browser traffic, which is why it needs an administrator to grant it
`roles/run.invoker` before Cloud Run will let anyone reach it at all -- see
teammate_gateway/main.py's docstring. This service never accepts inbound
traffic from anyone. Slack's "Socket Mode" has the bot open an outbound
WebSocket connection to Slack and receive events over that connection --
Slack never calls in. So this container only ever makes outbound calls (to
Slack, and to the VM over the same IAP tunnel tunnel_manager.py already
knows how to hold open), and needs no ingress configuration and no invoker
grant at all.

What it does: when someone @mentions the bot in Slack, forward their message
to the VM's existing /query endpoint (over the background IAP tunnel, using
the app owner's already-permitted personal credential -- see
tunnel_manager.py), wait for the final answer, and post it back to the same
Slack thread. The VM's own audit trail (api/web_app.py's _caller_identity())
attributes the request to the real Slack user's TELUS email when Slack will
give it to us (requires the users:read.email bot scope); otherwise it falls
back to a Slack-specific identity string so at least something identifiable
is recorded.

Deliberately out of scope for this first version: Slack's interactive
buttons (the human-in-the-loop confirm/review/correct flow the web UI has).
Buttons need Slack to call back into an HTTP endpoint you control -- which
would reintroduce exactly the inbound-traffic requirement this design avoids.
A plain @mention in, plain-text answer out loop is the tradeoff for staying
permission-free.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import tunnel_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_log = logging.getLogger(__name__)

# .strip() guards against a trailing newline sneaking into the secret value --
# e.g. piping a string into `gcloud secrets create ... --data-file=-` from
# PowerShell appends one, which otherwise breaks every Authorization header
# built from it with an "Illegal header value" error.
_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"].strip()
_APP_TOKEN = os.environ["SLACK_APP_TOKEN"].strip()

# Mirrors api/web_app.py's _expected_auth_cookie_value() exactly -- same
# cookie name, same HMAC construction -- so the value computed here is valid
# there. See teammate_gateway/main.py for the identical pattern.
_VM_AUTH_COOKIE_NAME = "octo_auth"
_VM_WEB_APP_SECRET = os.environ["VM_WEB_APP_SECRET"].strip().encode()
_VM_AUTH_COOKIE_VALUE = hmac.new(_VM_WEB_APP_SECRET, b"vibe-octo-authenticated", hashlib.sha256).hexdigest()

_VM_BASE_URL = f"http://localhost:{tunnel_manager.TUNNEL_LOCAL_PORT}"
_MENTION_RE = re.compile(r"<@[A-Z0-9]+>\s*")

app = App(token=_BOT_TOKEN, token_verification_enabled=False)


def _caller_email(client, slack_user_id: str) -> str:
    """Best-effort real email for attribution; falls back to a Slack-specific
    identity string if the users:read.email scope isn't granted."""
    try:
        info = client.users_info(user=slack_user_id)
        email = (info.get("user", {}).get("profile", {}) or {}).get("email", "")
        if email:
            return email
    except Exception:
        _log.exception("Could not resolve Slack user %s to an email.", slack_user_id)
    return f"slack:{slack_user_id}"


def _format_result(data: dict) -> str:
    """Turns one of api/web_app.py's _process_query_sync() result dicts into
    a plain-text Slack message. Mirrors the cases api/templates/chat.html
    renders for the web UI, minus anything that needs interactive buttons."""
    result_type = data.get("type", "")

    if result_type == "error":
        detail = data.get("detail", "")
        base = data.get("message", "I ran into an issue and couldn't complete your request.")
        return f"{base}\n> {detail}" if detail else base

    if result_type == "general_answer":
        return data.get("message", "")

    if result_type == "stuck":
        return data.get("explanation", "I wasn't able to size that request -- could you add more detail?")

    if result_type == "result":
        label = data.get("audience_label") or "your audience"
        count = data.get("audience_count")
        medium = data.get("medium", "")
        cadence = data.get("cadence", "")
        count_str = f"{count:,}" if isinstance(count, (int, float)) else "an unknown number of"
        lines = [f"*{label}*: {count_str} people" + (f" ({medium}, {cadence})" if medium or cadence else "")]
        if data.get("optimization_note"):
            lines.append(f"_Note: {data['optimization_note']}_")
        if data.get("confidence") is not None:
            lines.append(f"Confidence: {data['confidence']}%")
        return "\n".join(lines)

    return data.get("message") or "Done, but I didn't get a readable result back."


def _run_query(session_id: str, text: str, caller_email: str) -> str:
    headers = {
        "Cookie": f"{_VM_AUTH_COOKIE_NAME}={_VM_AUTH_COOKIE_VALUE}",
        "x-goog-authenticated-user-email": f"accounts.google.com:{caller_email}",
        "Content-Type": "application/json",
    }
    final_data: dict = {"type": "error", "message": "No response came back from the tool."}
    with httpx.Client(timeout=180.0) as client:
        with client.stream(
            "POST", f"{_VM_BASE_URL}/query",
            json={"session_id": session_id, "text": text},
            headers=headers,
        ) as resp:
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[len("data: "):])
                if event.get("type") == "final":
                    final_data = event.get("data", final_data)
    return _format_result(final_data)


@app.event("app_mention")
def handle_mention(event: dict, say, client) -> None:
    text = _MENTION_RE.sub("", event.get("text", "")).strip()
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    if not text:
        say(text="Ask me something about audience sizing or a campaign, and I'll take a look.", thread_ts=thread_ts)
        return

    ack = say(text="Working on it...", thread_ts=thread_ts)
    caller_email = _caller_email(client, event["user"])
    session_id = f"slack:{channel}:{thread_ts}"

    try:
        answer = _run_query(session_id, text, caller_email)
    except Exception as exc:
        _diag("QUERY TO VM FAILED", error=repr(exc))
        answer = "I ran into an issue reaching the tool. Please try again in a moment."

    client.chat_update(channel=channel, ts=ack["ts"], text=answer)


@app.event("message")
def ignore_other_messages(event: dict) -> None:
    """Bolt requires every subscribed event type to have a handler; direct
    messages and channel chatter that aren't @mentions are intentionally
    ignored so the bot only responds when addressed."""


_diag_state: dict = {"stage": "not started"}


class _HealthCheckHandler(BaseHTTPRequestHandler):
    """Answers Cloud Run's own startup/liveness probe, which hits the
    container directly and is unrelated to public reachability -- this
    service still accepts no inbound traffic from outside Google's
    infrastructure, and needs no invoker grant. Temporarily also reports
    live diagnostic state as plain text, since Cloud Logging visibility for
    this container could not be confirmed working."""

    def do_GET(self) -> None:  # noqa: N802 -- required name from BaseHTTPRequestHandler
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(json.dumps(_diag_state, default=str).encode())

    def log_message(self, *args) -> None:  # noqa: D102 -- silence per-request access logs
        pass


def _start_health_server() -> None:
    port = int(os.environ.get("PORT", 8080))
    threading.Thread(target=HTTPServer(("0.0.0.0", port), _HealthCheckHandler).serve_forever, daemon=True).start()


_DIAG_CHANNEL = os.environ.get("DIAG_CHANNEL", "")


def _diag(stage: str, **extra) -> None:
    """Temporary startup checkpoint reporter. Records state for the health
    endpoint to report back (queryable even though Cloud Logging visibility
    for this container could not be confirmed working), and best-effort
    posts to Slack too, capturing Slack's own response instead of assuming
    success. Safe to remove once the relay is confirmed working normally."""
    _diag_state["stage"] = stage
    _diag_state.update(extra)
    if not _DIAG_CHANNEL:
        return
    try:
        resp = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {_BOT_TOKEN}"},
            json={"channel": _DIAG_CHANNEL, "text": f"[diag] {stage} {extra}"},
            timeout=10,
        )
        _diag_state["last_slack_post_status"] = resp.status_code
        _diag_state["last_slack_post_body"] = resp.text[:300]
    except Exception as exc:
        _diag_state["last_slack_post_error"] = repr(exc)


if __name__ == "__main__":
    _diag("container process started", bot_token_present=bool(_BOT_TOKEN), app_token_present=bool(_APP_TOKEN))
    _start_health_server()
    _diag("health server bound")
    tunnel_manager.start_background_tunnel(on_event=_diag)
    _diag("background IAP tunnel thread launched")
    _diag("about to open Socket Mode connection")
    try:
        SocketModeHandler(app, _APP_TOKEN).start()
    except Exception as exc:
        _diag("SOCKET MODE FAILED", error=repr(exc))
        raise
