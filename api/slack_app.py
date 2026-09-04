"""api/slack_app.py -- Slack front end (temporary, laptop-only proof-of-concept).

Runs in Socket Mode: an outbound-only connection to Slack, no public URL or
inbound port needed -- this is why Slack works from a laptop when the VM
(restricted to Google-owned network destinations only) cannot reach it at all.

Calls into the exact same core pipeline api/web_app.py already uses
(process_core_request), and reuses api/web_app.py's own HITL/correction
persistence functions directly rather than reimplementing them -- one clear
front door for business logic and permanent storage; only the presentation
layer (this file + api/slack_formatter.py) is Slack-specific.

Scope for this pass: sizing_request and general_question intents only --
briefing is still None in build_runtime() today (same gap the web UI has),
so brief/campaign-execution intents aren't functional yet, independent of Slack.
The "stuck, need a clarifying detail" case is shown as a one-shot explanation
here (no retry loop yet, unlike the web UI) -- ask a fresh question instead.

Run:  python -m api.slack_app
"""
from __future__ import annotations

import logging
import os
import re
import threading

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import api.web_app as web_app
from api import slack_formatter as fmt
from api.slack_session_store import SlackSessionState, SlackSessionStore
from vibe_orchestrator import RequestResult, build_runtime, generate_stuck_explanation, process_core_request

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

load_dotenv()

_runtime = None  # set in main(), also mirrored onto web_app._runtime so its reused functions work standalone
_session_store = SlackSessionStore()

app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.getenv("SLACK_SIGNING_SECRET"),
    token_verification_enabled=False,
)


def _post(client, channel: str, thread_ts: str, text: str, blocks=None) -> None:
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text, blocks=blocks)


# ---------------------------------------------------------------------------
# Incoming messages
# ---------------------------------------------------------------------------

@app.event("app_mention")
def handle_mention(event: dict, client) -> None:
    channel: str = event["channel"]
    thread_ts: str = event.get("thread_ts") or event["ts"]
    user: str = event.get("user", "unknown")
    text: str = re.sub(r"<@[A-Z0-9]+>", "", event.get("text", "")).strip()

    session: SlackSessionState = _session_store.get(channel)
    session.last_sender = user

    if session.awaiting_correction:
        session.awaiting_correction = False
        threading.Thread(target=_handle_correction_text, args=(text, channel, thread_ts, session), daemon=True).start()
        return

    threading.Thread(target=_handle_query, args=(text, channel, thread_ts, session), daemon=True).start()


def _handle_query(text: str, channel: str, thread_ts: str, session: SlackSessionState) -> None:
    client = app.client
    _post(client, channel, thread_ts, "Got it! Let me look into that for you...")

    result: RequestResult = process_core_request(
        text, _runtime,
        session_id=channel,
        session_corrections=session.active_corrections or None,
    )

    if result.error:
        _post(
            client, channel, thread_ts, "I ran into an issue.",
            blocks=fmt.format_error("I ran into an issue and couldn't complete your request.", result.error[:300]),
        )
        return

    if result.log is not None or result.brief_output is not None:
        session.store_result(
            query=text, intent=result.intent, spec=result.spec, log=result.log, brief=result.brief_output,
        )

    intent_type = result.intent.intent_type if result.intent else "general_question"

    if intent_type == "general_question" and result.log is None and result.brief_output is None:
        _post(
            client, channel, thread_ts, "Answered from the knowledge base.",
            blocks=fmt.format_general_answer("I've answered from the knowledge base. Is there anything else I can help with?"),
        )
        return

    if result.log is None and result.brief_output is None:
        session.last_query = text
        explanation = generate_stuck_explanation(_runtime.nexus, text, result.intent, result.spec, result.brief_output)
        _post(client, channel, thread_ts, "I need a bit more information.", blocks=fmt.format_error(explanation))
        return

    if result.log is not None and result.brief_output is not None:
        blocks = fmt.format_combined_result(result.log, result.brief_output)
    elif result.brief_output is not None:
        blocks = fmt.format_brief_result(result.brief_output)
    else:
        blocks = fmt.format_sizing_result(result.log)

    _post(client, channel, thread_ts, f"Result for: {text}", blocks=blocks)


def _handle_correction_text(text: str, channel: str, thread_ts: str, session: SlackSessionState) -> None:
    client = app.client
    result = web_app._process_correction_sync(text, session.last_spec, session.last_audit_log, session.last_query, session)

    if result["type"] == "correction_clarifying":
        session.awaiting_correction = True  # the next @mention is the answer to this question
        _post(client, channel, thread_ts, result["question"], blocks=fmt.format_clarifying_question(result["question"]))
        return

    if result["type"] == "correction_error":
        session.awaiting_correction = True  # let them try rephrasing
        _post(client, channel, thread_ts, result["message"])
        return

    # correction_interpreted -- show what was understood, wait for a button click (not free text)
    _post(
        client, channel, thread_ts, "Here's what I understood from your feedback:",
        blocks=fmt.format_correction_interpretation(result.get("interpretation", "")),
    )


# ---------------------------------------------------------------------------
# HITL buttons
# ---------------------------------------------------------------------------

@app.action(fmt.HITL_ACTION_YES)
def handle_hitl_yes(ack, body, client) -> None:
    ack()
    channel = body["channel"]["id"]
    thread_ts = body["message"].get("thread_ts") or body["message"]["ts"]
    session = _session_store.get(channel)
    result = web_app._handle_hitl_yes_sync(session, channel)
    session.clear_hitl()
    _post(client, channel, thread_ts, result["message"])


@app.action(fmt.HITL_ACTION_REVIEW)
def handle_hitl_review(ack, body, client) -> None:
    ack()
    channel = body["channel"]["id"]
    thread_ts = body["message"].get("thread_ts") or body["message"]["ts"]
    session = _session_store.get(channel)
    result = web_app._build_review_response(session)

    if result["type"] == "review_sql":
        blocks = fmt.format_sql_detail(session.last_audit_log)
    elif result["type"] == "review_sources":
        blocks = fmt.format_sources_detail(session.last_brief)
    else:
        blocks = fmt.format_error(result.get("message", "No detail is available for this result."))
    _post(client, channel, thread_ts, "Here's how it was built.", blocks=blocks)


@app.action(fmt.HITL_ACTION_NO)
def handle_hitl_no(ack, body, client) -> None:
    ack()
    channel = body["channel"]["id"]
    thread_ts = body["message"].get("thread_ts") or body["message"]["ts"]
    session = _session_store.get(channel)
    session.awaiting_correction = True
    _post(client, channel, thread_ts, "No problem! Please describe what looks wrong in your own words -- no need to be technical.")


# ---------------------------------------------------------------------------
# Correction confirm/clarify buttons (the two-phase flow the web UI already has)
# ---------------------------------------------------------------------------

@app.action(fmt.CORRECTION_ACTION_CONFIRM)
def handle_correction_confirm(ack, body, client) -> None:
    ack()
    channel = body["channel"]["id"]
    thread_ts = body["message"].get("thread_ts") or body["message"]["ts"]
    session = _session_store.get(channel)
    result = web_app._save_confirmed_correction_sync(session, session.pending_correction)
    _post(client, channel, thread_ts, result["message"])


@app.action(fmt.CORRECTION_ACTION_CLARIFY)
def handle_correction_clarify(ack, body, client) -> None:
    ack()
    channel = body["channel"]["id"]
    thread_ts = body["message"].get("thread_ts") or body["message"]["ts"]
    session = _session_store.get(channel)
    session.awaiting_correction = True
    _post(client, channel, thread_ts, "No problem -- please describe it again, and I'll re-read it carefully.")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def main() -> None:
    global _runtime
    _log.info("Vibe OCTO Slack bot starting -- initializing runtime...")
    _runtime = build_runtime()
    web_app._runtime = _runtime  # so the reused api.web_app functions have a runtime to act on
    _log.info("Runtime ready. Starting Socket Mode connection to Slack...")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()


if __name__ == "__main__":
    main()
