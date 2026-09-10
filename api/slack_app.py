"""api/slack_app.py -- Slack front end (temporary, laptop-only proof-of-concept).

Runs in Socket Mode: an outbound-only connection to Slack, no public URL or
inbound port needed -- this is why Slack works from a laptop when the VM
(restricted to Google-owned network destinations only) cannot reach it at all.

Calls into the exact same core pipeline api/web_app.py already uses
(process_core_request), and reuses api/web_app.py's own HITL/correction
persistence functions directly rather than reimplementing them -- one clear
front door for business logic and permanent storage; only the presentation
layer (this file + api/slack_formatter.py) is Slack-specific.

Only two intents exist: sizing_request and general_question -- see
agents/nexus_agent.py's module docstring for why the earlier
brief/campaign-execution machinery was removed rather than kept as dead code.
The "stuck, need a clarifying detail" case is shown as a one-shot explanation
here (no retry loop yet, unlike the web UI) -- ask a fresh question instead.

Run:  python -m api.slack_app
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Optional

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

import api.web_app as web_app
from api import slack_formatter as fmt
from api.slack_session_store import SlackSessionState, SlackSessionStore
from vibe_orchestrator import RequestResult, VibeRuntime, build_runtime, generate_stuck_explanation, process_core_request

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
        threading.Thread(target=_safe_handle_correction_text, args=(text, channel, thread_ts, session), daemon=True).start()
        return

    threading.Thread(target=_safe_handle_query, args=(text, channel, thread_ts, session), daemon=True).start()


def _safe_handle_query(text: str, channel: str, thread_ts: str, session: SlackSessionState) -> None:
    """Wraps _handle_query so a bug never leaves the user staring at 'Got it!'
    forever with no further reply -- any unhandled exception is reported back,
    not swallowed by a dying background thread."""
    try:
        _handle_query(text, channel, thread_ts, session)
    except Exception as exc:  # noqa: BLE001 -- last-resort catch so the thread always reports back
        _log.exception("Unhandled error while processing a query: %s", exc)
        try:
            _post(app.client, channel, thread_ts, "I ran into an unexpected problem.", blocks=fmt.format_error("I ran into an unexpected problem.", str(exc)[:300]))
        except Exception:
            pass  # if even posting the error fails, there's nothing more we can do


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

    if result.log is not None:
        session.store_result(query=text, intent=result.intent, log=result.log)

    intent_type = result.intent.intent_type if result.intent else "general_question"

    if intent_type == "general_question":
        answer = result.answer_text or "I've answered from the knowledge base. Is there anything else I can help with?"
        _post(client, channel, thread_ts, "Answered from the knowledge base.", blocks=fmt.format_general_answer(answer))
        return

    if result.log is None:
        session.last_query = text
        explanation = generate_stuck_explanation(_runtime.nexus, text, result.intent)
        _post(client, channel, thread_ts, "I need a bit more information.", blocks=fmt.format_error(explanation))
        return

    blocks = fmt.format_sizing_result(result.log)
    _post(client, channel, thread_ts, f"Result for: {text}", blocks=blocks)


def _safe_handle_correction_text(text: str, channel: str, thread_ts: str, session: SlackSessionState) -> None:
    try:
        _handle_correction_text(text, channel, thread_ts, session)
    except Exception as exc:  # noqa: BLE001 -- last-resort catch so the thread always reports back
        _log.exception("Unhandled error while processing a correction: %s", exc)
        try:
            _post(app.client, channel, thread_ts, "I ran into an unexpected problem.", blocks=fmt.format_error("I ran into an unexpected problem.", str(exc)[:300]))
        except Exception:
            pass


def _handle_correction_text(text: str, channel: str, thread_ts: str, session: SlackSessionState) -> None:
    client = app.client
    result = web_app._process_correction_sync(text, session.last_audit_log, session.last_query, session)

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
    session.reviewed_this_result = True
    result = web_app._build_review_response(session)

    if result["type"] == "review_sql":
        blocks = fmt.format_sql_detail(session.last_audit_log)
    else:
        blocks = fmt.format_error(result.get("message", "No detail is available for this result."))
    _post(client, channel, thread_ts, "Here's how it was built.", blocks=blocks)


@app.action(fmt.HITL_ACTION_NO)
def handle_hitl_no(ack, body, client) -> None:
    ack()
    channel = body["channel"]["id"]
    thread_ts = body["message"].get("thread_ts") or body["message"]["ts"]
    session = _session_store.get(channel)
    outcome = web_app.HITL_REVIEW_NO if session.reviewed_this_result else web_app.HITL_NO
    web_app._log_hitl_resolution(session, channel, outcome)
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

def main(runtime: Optional[VibeRuntime] = None) -> None:
    """Start the Slack bot. If a runtime is already built (the combined web+Slack
    entrypoint builds one for the web app's own startup and hands it here), reuse
    it instead of building a second, independent one -- two separate runtimes in
    the same process would mean two separate KnowledgeContext setups racing to
    set web_app._runtime, which is exactly the split-knowledge problem this
    combined entrypoint exists to eliminate. Standalone use (no argument) keeps
    working exactly as before."""
    global _runtime
    if runtime is not None:
        _runtime = runtime
        _log.info("Vibe OCTO Slack bot starting -- reusing the already-built runtime...")
    else:
        _log.info("Vibe OCTO Slack bot starting -- initializing runtime...")
        _runtime = build_runtime()
    web_app._runtime = _runtime  # so the reused api.web_app functions have a runtime to act on
    _log.info("Runtime ready. Starting Socket Mode connection to Slack...")
    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.start()


if __name__ == "__main__":
    main()
