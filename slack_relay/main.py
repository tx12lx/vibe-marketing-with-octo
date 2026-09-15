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
tunnel_manager.py), stream the VM's plain-English progress narration back
into the same placeholder message as it arrives, and post the final answer
to the same Slack thread with buttons for reviewing or correcting it. The
VM's own audit trail (api/web_app.py's _caller_identity()) attributes the
request to the real Slack user's TELUS email when Slack will give it to us
(requires the users:read.email bot scope); otherwise it falls back to a
Slack-specific identity string so at least something identifiable is
recorded.

Once a thread has started (via that first @mention), every later reply in
the same thread is handled too -- no re-mention needed. This requires the
Slack app to actually be told about those plain messages, which needs its
own scope/subscription pair granted in the Slack app's own settings (not
something this code can do for itself): "channels:history" (or
"groups:history" for a private channel) as a bot token scope, plus the
matching "message.channels" ("message.groups") bot event subscription.
Without both, Slack simply never delivers those messages here at all, and a
reply with no @mention gets no response -- a Slack-side gap, not a bug in
this file. A message that DOES mention the bot arrives twice (Slack sends
both a "message" and an "app_mention" event for it) -- handle_message()
recognizes and skips those, since handle_mention() already processes them,
to avoid answering twice.

Human-in-the-loop review and corrections (previously out of scope) are now
wired up here too: buttons ("Show me how you got this", "Something looks
wrong") call the same /hitl, /correction, and /confirm-correction endpoints
the web UI uses, over Slack's Socket Mode -- Slack delivers button clicks
(block_actions) over the same outbound connection this service already holds
open for events, so none of this needs an inbound HTTP endpoint or a new GCP
permission either. This does require "Interactivity & Shortcuts" to be turned
on (with Socket Mode) in the Slack app's own configuration -- a one-time,
workspace-side setting change, not something this code can do for itself.

"Show me how you got this" always shows the real underlying database query
alongside the plain-English breakdown, in one combined step -- not a separate
optional extra -- matching api/web_app.py's own review_sql response shape.
Since this tool's whole job (today) is writing and running real database
queries, and everyone using it for sizing already reads SQL, a result only
ever counts as reviewed once the actual query has been shown, not just a
plain-English gloss of it.

Nothing is saved to the tool's permanent knowledge without the person
explicitly confirming it first -- the same confirm-before-save step
api/web_app.py already enforces for the web UI.
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
from typing import Callable

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

# How many of the most recent progress lines stay visible in the "working on
# it" message at once -- keeps the message from turning into a wall of text
# on a long-running request (progressive disclosure, matching the web UI).
_MAX_VISIBLE_PROGRESS_LINES = 4

app = App(token=_BOT_TOKEN, token_verification_enabled=False)
# This bot's own Slack user id -- resolved once at startup, so a later message
# that mentions the bot can be recognized and skipped in handle_message()
# (see that function's docstring for why: Slack delivers such a message as
# BOTH a "message" and an "app_mention" event, and handle_mention() already
# answers it).
_BOT_USER_ID: str = app.client.auth_test()["user_id"]

# ---------------------------------------------------------------------------
# Ephemeral, per-thread UI-only state.
#
# This does not duplicate the VM's own session/conversation memory (that
# lives entirely in api/session_store.py, keyed by the same session_id) -- it
# only tracks what this relay itself needs to know to route the next Slack
# event correctly. Scoped strictly to one session_id (one Slack thread) and
# never read across threads.
#
# A session_id appears here from the moment its thread's first @mention is
# handled, and stays here for the life of the thread (not just while a
# correction is pending) -- that presence is what tells handle_message()
# "this is a thread I'm already part of, not unrelated channel chatter."
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()
_thread_sessions: dict[str, dict] = {}  # session_id -> {"caller_email": str, "awaiting_correction": bool}


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


def _parse_session_id(session_id: str) -> tuple[str, str]:
    """session_id is always "slack:{channel}:{thread_ts}" (see handle_mention) --
    split it back apart rather than trusting Slack's action payload shape,
    since the value we put on the button is the one source of truth here."""
    _, channel, thread_ts = session_id.split(":", 2)
    return channel, thread_ts


def _vm_headers(caller_email: str) -> dict:
    return {
        "Cookie": f"{_VM_AUTH_COOKIE_NAME}={_VM_AUTH_COOKIE_VALUE}",
        "x-goog-authenticated-user-email": f"accounts.google.com:{caller_email}",
        "Content-Type": "application/json",
    }


def _vm_post(path: str, session_id: str, caller_email: str, **body_extra) -> dict:
    """POST one of the VM's plain-JSON HITL/correction endpoints (/hitl,
    /correction, /confirm-correction) -- unlike /query these aren't
    streamed, they just return one JSON object."""
    body = {"session_id": session_id, **body_extra}
    with httpx.Client(timeout=180.0) as client:
        resp = client.post(f"{_VM_BASE_URL}{path}", json=body, headers=_vm_headers(caller_email))
        resp.raise_for_status()
        return resp.json()


def _format_result(data: dict) -> str:
    """Turns one of api/web_app.py's _process_query_sync() result dicts into
    plain text -- used as the visible answer for non-'result' types, and as
    the required fallback/notification text alongside every Block Kit message."""
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
        # medium/cadence are only ever present when the consultant actually stated one --
        # no fake "unspecified"/"ad-hoc" placeholder is sent, so most results have neither.
        meta = ", ".join(v for v in (data.get("medium"), data.get("cadence")) if v)
        count_str = f"{count:,}" if isinstance(count, (int, float)) else "an unknown number of"
        lines = [f"*{label}*: {count_str} people" + (f" ({meta})" if meta else "")]
        if data.get("optimization_note"):
            lines.append(f"_Note: {data['optimization_note']}_")
        if data.get("confidence") is not None:
            lines.append(f"Confidence: {data['confidence']}%")
        return "\n".join(lines)

    return data.get("message") or "Done, but I didn't get a readable result back."


def _actions_block(session_id: str, buttons: list[tuple[str, str]]) -> dict:
    """buttons is a list of (action_id, label) pairs; the button's value is
    always the session_id, so an action handler can find its way back to the
    right Slack thread and the right VM session without any extra lookup."""
    return {
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": label},
                "action_id": action_id,
                "value": session_id,
            }
            for action_id, label in buttons
        ],
    }


def _result_blocks(answer_text: str, session_id: str) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": answer_text}},
        _actions_block(session_id, [
            ("hitl_review", "Show me how you got this"),
            ("hitl_no", "Something looks wrong"),
        ]),
    ]


def _review_blocks(sql: str, waterfall: list[dict], session_id: str) -> list[dict]:
    """The real query and the plain-English breakdown together, in one required
    step -- matches api/web_app.py's own review_sql response shape. A result only
    ever counts as reviewed once this has been shown, so the query is never
    optional or hidden behind a second click here."""
    sql_body = sql[:2900] if sql else "(no query was recorded for this result)"
    lines = []
    for layer in waterfall:
        step = layer.get("step", "")
        count = layer.get("count")
        count_str = f"{count:,}" if isinstance(count, (int, float)) else "unknown"
        lines.append(f"• *{step}* — {count_str}")
    wf_text = "\n".join(lines) if lines else "_No breakdown is available for this result._"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Here's the underlying database query:*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{sql_body}```"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Here's how I got to that number:*\n{wf_text}"}},
        _actions_block(session_id, [
            ("hitl_yes", "Confirmed, looks correct"),
            ("hitl_no", "Still something wrong"),
        ]),
    ]


def _correction_confirm_blocks(interpretation: str, session_id: str) -> list[dict]:
    text = f"*Here's what I understood:*\n{interpretation}" if interpretation else "I've noted your feedback -- want me to remember this going forward?"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        _actions_block(session_id, [
            ("confirm_yes", "Yes, that's right"),
            ("confirm_no", "No, let me re-explain"),
        ]),
    ]


def _run_query(session_id: str, text: str, caller_email: str, on_progress: Callable[[str], None]) -> dict:
    """Streams /query's SSE events, forwarding each 'progress' line to
    on_progress as it arrives (a broken/slow callback must never break this),
    and returns the 'final' event's data dict once the stream ends."""
    final_data: dict = {"type": "error", "message": "No response came back from the tool."}
    with httpx.Client(timeout=180.0) as client:
        with client.stream(
            "POST", f"{_VM_BASE_URL}/query",
            json={"session_id": session_id, "text": text},
            headers=_vm_headers(caller_email),
        ) as resp:
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                event = json.loads(line[len("data: "):])
                etype = event.get("type")
                if etype == "progress":
                    try:
                        on_progress(event.get("message", ""))
                    except Exception:
                        _log.exception("Progress callback failed; continuing without it.")
                elif etype == "final":
                    final_data = event.get("data", final_data)
    return final_data


def _route_thread_message(channel: str, thread_ts: str, session_id: str, text: str, caller_email: str, say, client) -> None:
    """Shared entry point for both a fresh @mention and a plain follow-up
    reply in a thread this bot already knows about -- decides query vs.
    correction from THIS thread's own remembered state, so the same message
    is handled identically no matter which Slack event carried it in."""
    with _state_lock:
        session = _thread_sessions.setdefault(session_id, {"caller_email": caller_email, "awaiting_correction": False})
        session["caller_email"] = caller_email  # a different teammate may pick up the same thread later
        awaiting = session["awaiting_correction"]

    if not text:
        # Kept as a fixed string deliberately: this relay has no direct path to the AI
        # model (it only ever calls the VM's /query, /hitl, /correction endpoints over
        # the tunnel -- see module docstring), and there's no request content yet to
        # compose anything about.
        say(text="Hi! Ask me anything and I'll figure out who can help.", thread_ts=thread_ts)
        return

    if awaiting:
        _handle_correction_turn(thread_ts, session_id, text, say)
    else:
        _handle_query_turn(channel, thread_ts, session_id, text, caller_email, say, client)


def _handle_query_turn(channel: str, thread_ts: str, session_id: str, text: str, caller_email: str, say, client) -> None:
    ack = say(text="Working on it...", thread_ts=thread_ts)

    progress_lines: list[str] = []

    def on_progress(message: str) -> None:
        if not message:
            return
        progress_lines.append(message)
        shown = progress_lines[-_MAX_VISIBLE_PROGRESS_LINES:]
        try:
            client.chat_update(channel=channel, ts=ack["ts"], text="\n".join(shown))
        except Exception:
            _log.exception("Progress update to Slack failed; continuing without it.")

    try:
        final_data = _run_query(session_id, text, caller_email, on_progress)
    except Exception as exc:
        _diag("QUERY TO VM FAILED", error=repr(exc))
        try:
            client.chat_update(
                channel=channel, ts=ack["ts"],
                text="I ran into an issue reaching the tool. Please try again in a moment.",
            )
        except Exception:
            _log.exception("Could not report the query failure back to Slack.")
        return

    answer_text = _format_result(final_data)
    if final_data.get("type") == "result":
        client.chat_update(channel=channel, ts=ack["ts"], text=answer_text, blocks=_result_blocks(answer_text, session_id))
    else:
        client.chat_update(channel=channel, ts=ack["ts"], text=answer_text)


def _handle_correction_turn(thread_ts: str, session_id: str, text: str, say) -> None:
    """The free-text reply to "Something looks wrong" / "let me re-explain" --
    same /correction call handle_message() used to make directly, now shared
    so a correction can arrive via a plain reply OR an @mention either way."""
    with _state_lock:
        session = _thread_sessions.get(session_id, {})
        caller_email = session.get("caller_email", "")
    try:
        data = _vm_post("/correction", session_id, caller_email, text=text)
    except Exception:
        _log.exception("correction call to VM failed.")
        say(text="I had trouble processing that. Could you try again?", thread_ts=thread_ts)
        return

    result_type = data.get("type")
    if result_type == "correction_clarifying":
        # Still needs a free-text reply -- leave awaiting_correction as is.
        say(text=data.get("question", ""), thread_ts=thread_ts)
        return

    with _state_lock:
        session = _thread_sessions.get(session_id)
        if session is not None:
            session["awaiting_correction"] = False

    if result_type == "correction_interpreted":
        blocks = _correction_confirm_blocks(data.get("interpretation", ""), session_id)
        say(text=data.get("message", "Here is what I understood:"), thread_ts=thread_ts, blocks=blocks)
    else:
        say(text=data.get("message", "Got it."), thread_ts=thread_ts)


@app.event("app_mention")
def handle_mention(event: dict, say, client) -> None:
    text = _MENTION_RE.sub("", event.get("text", "")).strip()
    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event["ts"]
    session_id = f"slack:{channel}:{thread_ts}"
    caller_email = _caller_email(client, event["user"])
    _route_thread_message(channel, thread_ts, session_id, text, caller_email, say, client)


# ---------------------------------------------------------------------------
# HITL / correction actions -- Slack delivers these as "block_actions" over
# the same Socket Mode connection used for events (see module docstring).
# Every handler acks immediately (Slack requires a response within 3s),
# resolves session_id back to (channel, thread_ts), and posts its response
# as a new reply in that same thread rather than editing prior messages, so
# the original answer and the review trail both stay visible.
# ---------------------------------------------------------------------------

@app.action("hitl_review")
def handle_hitl_review(ack, body, client) -> None:
    ack()
    session_id = body["actions"][0]["value"]
    channel, thread_ts = _parse_session_id(session_id)
    caller_email = _caller_email(client, body["user"]["id"])
    try:
        data = _vm_post("/hitl", session_id, caller_email, action="review")
    except Exception:
        _log.exception("hitl_review call to VM failed.")
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="I couldn't pull up the details right now. Please try again in a moment.")
        return

    if data.get("type") != "review_sql":
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=data.get("message") or "No detail is available for this result.")
        return

    blocks = _review_blocks(data.get("sql", ""), data.get("waterfall", []), session_id)
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="Here's how I got to that number.", blocks=blocks)


@app.action("hitl_no")
def handle_hitl_no(ack, body, client) -> None:
    ack()
    session_id = body["actions"][0]["value"]
    channel, thread_ts = _parse_session_id(session_id)
    caller_email = _caller_email(client, body["user"]["id"])
    try:
        data = _vm_post("/hitl", session_id, caller_email, action="no")
    except Exception:
        _log.exception("hitl_no call to VM failed.")
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="I couldn't process that right now. Please try again in a moment.")
        return

    with _state_lock:
        session = _thread_sessions.setdefault(session_id, {"caller_email": caller_email, "awaiting_correction": False})
        session["caller_email"] = caller_email
        session["awaiting_correction"] = True
    message = data.get("message") or "No problem! Please describe what looks wrong in your own words -- no need to be technical."
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=message)


@app.action("hitl_yes")
def handle_hitl_yes(ack, body, client) -> None:
    ack()
    session_id = body["actions"][0]["value"]
    channel, thread_ts = _parse_session_id(session_id)
    caller_email = _caller_email(client, body["user"]["id"])
    try:
        data = _vm_post("/hitl", session_id, caller_email, action="yes")
    except Exception:
        _log.exception("hitl_yes call to VM failed.")
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="I couldn't save that confirmation right now. Please try again in a moment.")
        return

    if data.get("type") == "review_required":
        # Same evidence-first gate the web UI enforces server-side: a sizing
        # result can't be confirmed sight-unseen, even via a direct button
        # click that skipped the review step.
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": data.get("message", "")}},
            _actions_block(session_id, [
                ("hitl_review", "Show me how you got this"),
                ("hitl_no", "Something looks wrong"),
            ]),
        ]
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=data.get("message", ""), blocks=blocks)
        return

    detail = data.get("detail", "")
    message = data.get("message", "Thanks, noted!")
    text = f"{message}\n_{detail}_" if detail else message
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


@app.action("confirm_yes")
def handle_confirm_yes(ack, body, client) -> None:
    ack()
    session_id = body["actions"][0]["value"]
    channel, thread_ts = _parse_session_id(session_id)
    caller_email = _caller_email(client, body["user"]["id"])
    try:
        data = _vm_post("/confirm-correction", session_id, caller_email)
    except Exception:
        _log.exception("confirm_yes call to VM failed.")
        client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="I couldn't save that just now. Please try again in a moment.")
        return

    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=data.get("message", "Got it, thanks!"))

    # Nothing is written to the knowledge layer until this confirmation, and
    # the corrected answer is retried in this same turn so the person doesn't
    # have to ask their original question again -- mirrors
    # api/web_app.py's _save_confirmed_correction_sync().
    retry = data.get("retry_result")
    if retry:
        retry_text = _format_result(retry)
        if retry.get("type") == "result":
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=retry_text, blocks=_result_blocks(retry_text, session_id))
        else:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=retry_text)


@app.action("confirm_no")
def handle_confirm_no(ack, body, client) -> None:
    ack()
    session_id = body["actions"][0]["value"]
    channel, thread_ts = _parse_session_id(session_id)
    caller_email = _caller_email(client, body["user"]["id"])
    with _state_lock:
        session = _thread_sessions.setdefault(session_id, {"caller_email": caller_email, "awaiting_correction": False})
        session["caller_email"] = caller_email
        session["awaiting_correction"] = True
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="No problem -- go ahead and describe it again, in your own words.")


@app.event("message")
def handle_message(event: dict, say, client) -> None:
    """Handles every plain (no-mention) reply in a thread this bot already
    knows about, so a conversation can keep going without re-mentioning it --
    the same routing handle_mention() uses (query vs. correction, decided by
    this thread's own remembered state), just reached from a different event.

    Skips: the bot's own messages/edits/deletes (bot_id/subtype set); any
    message outside a thread (a thread is what "known" means here); a thread
    this bot was never actually part of (not in _thread_sessions -- ordinary
    channel chatter, left alone); and a message that mentions this bot, since
    Slack delivers that one as an app_mention event too and handle_mention()
    already answers it -- handling it here as well would answer it twice."""
    if event.get("bot_id") or event.get("subtype"):
        return
    if f"<@{_BOT_USER_ID}>" in event.get("text", ""):
        return

    thread_ts = event.get("thread_ts")
    if not thread_ts:
        return

    channel = event["channel"]
    session_id = f"slack:{channel}:{thread_ts}"
    with _state_lock:
        known = session_id in _thread_sessions
    if not known:
        return

    text = event.get("text", "").strip()
    if not text:
        return

    caller_email = _caller_email(client, event.get("user", ""))
    _route_thread_message(channel, thread_ts, session_id, text, caller_email, say, client)


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
