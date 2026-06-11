"""
api/slack_app.py -- Vibe OCTO Slack interface.

Uses the Slack Bolt framework. Runs in Socket Mode for laptop development
(no public URL or ngrok needed). Switch to HTTP mode for server deployment by
setting SLACK_SOCKET_MODE=false in the environment.

Required environment variables (set in root .env or environment):
  SLACK_BOT_TOKEN      -- Bot OAuth token (starts with xoxb-)
  SLACK_APP_TOKEN      -- App-level token for Socket Mode (starts with xapp-)
  SLACK_SIGNING_SECRET -- Signing secret from Basic Information page

Setup (one-time):
  1.  Go to api.slack.com/apps -- Create New App -- From scratch.
  2.  OAuth & Permissions -- Bot Token Scopes: chat:write, app_mentions:read,
      channels:history.
  3.  Event Subscriptions -- Enable Events -- Subscribe to bot events: app_mention.
  4.  Basic Information -- App-Level Tokens -- Generate token with connections:write
      scope. This is SLACK_APP_TOKEN.
  5.  Basic Information -- App Credentials -- copy Signing Secret.
  6.  Install App -- Install to Workspace -- copy Bot User OAuth Token.
  7.  In your workspace: /invite @VibeOCTO in the target channel.

Run (Socket Mode, laptop):
  python -m api.slack_app

Run (HTTP mode, internal server):
  SLACK_SOCKET_MODE=false python -m api.slack_app

HITL flow:
  1. User @mentions bot -> pipeline runs in background thread with progress updates.
  2. Final result posted with three buttons: Looks good / Show me how it was built /
     Something looks wrong.
  3. Button clicks arrive as action events:
     - hitl_yes     -> save result (HITLAuditLoop._handle_yes), post confirmation
     - hitl_review  -> post SQL or brief sources as a follow-up thread reply
     - hitl_no      -> prompt for correction; session.awaiting_correction = True
  4. If awaiting_correction, the user's next @mention is treated as free-text
     correction for FeedbackAgent rather than a new query.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path so all project imports resolve.
# slack_app.py lives in api/ which is one level below the project root.
# ---------------------------------------------------------------------------
_API_DIR = Path(__file__).resolve().parent
_ROOT = _API_DIR.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env")

from slack_bolt import App  # noqa: E402
from slack_bolt.adapter.socket_mode import SocketModeHandler  # noqa: E402

from vibe_orchestrator import (  # noqa: E402
    VibeRuntime,
    RequestResult,
    build_runtime,
    _ARTIFACTS_DIR,
    _build_dynamic_context,
    _agent_called_for_intent,
    route_by_intent,
    _run_adhoc_feedback,
)
from core.audit_logger import HITL_YES  # noqa: E402
from core.resilience import run_startup_health_check  # noqa: E402
from pydantic_schemas import BriefingOutput, IntentClassification, QuantAuditLog, UniversalJSONSpec  # noqa: E402
from api.slack_session_store import SlackSessionStore  # noqa: E402
from api.slack_formatter import (  # noqa: E402
    format_sizing_result,
    format_brief_result,
    format_combined_result,
    format_sql_detail,
    format_sources_detail,
    format_error,
    HITL_ACTION_YES,
    HITL_ACTION_REVIEW,
    HITL_ACTION_NO,
)

_log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# ---------------------------------------------------------------------------
# Initialize Slack Bolt App
# ---------------------------------------------------------------------------

_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")

app = App(
    token=_BOT_TOKEN,
    signing_secret=_SIGNING_SECRET,
    # Skip auth.test at import time -- validated explicitly in main() so that
    # importing this module for testing does not require real Slack credentials.
    token_verification_enabled=False,
)

# ---------------------------------------------------------------------------
# Shared runtime and session store
# ---------------------------------------------------------------------------

_runtime: Optional[VibeRuntime] = None
_session_store = SlackSessionStore()


# ---------------------------------------------------------------------------
# @mention handler
# ---------------------------------------------------------------------------

@app.event("app_mention")
def handle_mention(event: dict, client) -> None:
    """Fired when someone @mentions the bot in a channel."""
    channel: str = event["channel"]
    # Use thread_ts if this is already a thread reply; otherwise start a new thread
    # by using the message's own ts as the thread root.
    thread_ts: str = event.get("thread_ts") or event["ts"]
    user: str = event.get("user", "unknown")

    # Strip @-mention markup (<@UXXXXXXXX>) to get the clean query text.
    raw_text: str = event.get("text", "")
    message_text = re.sub(r"<@[A-Z0-9]+>", "", raw_text).strip()

    if not message_text:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="I didn't catch that. Could you rephrase your question?",
        )
        return

    if _runtime is None:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="Vibe OCTO is still starting up. Please try again in a moment.",
        )
        return

    session = _session_store.get(channel)
    session.last_sender = user

    # If the user previously clicked "Something looks wrong", treat this message
    # as a free-text correction for FeedbackAgent rather than a new query.
    if session.awaiting_correction:
        session.awaiting_correction = False
        threading.Thread(
            target=_process_correction,
            args=(message_text, channel, thread_ts, session),
            daemon=True,
        ).start()
        return

    # Normal request -- run the full pipeline in a background thread.
    threading.Thread(
        target=_process_query,
        args=(message_text, channel, thread_ts, user, session),
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Button action handlers (must call ack() within 3 seconds)
# ---------------------------------------------------------------------------

@app.action(HITL_ACTION_YES)
def handle_hitl_yes(ack, body, client) -> None:
    ack()
    channel: str = body["channel"]["id"]
    thread_ts: str = body["message"].get("thread_ts") or body["message"]["ts"]
    session = _session_store.get(channel)

    spec = session.last_spec
    log = session.last_audit_log
    brief = session.last_brief
    session.clear_hitl()

    if _runtime is None or spec is None or log is None:
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="Result confirmed. Thanks!",
        )
        return

    try:
        _runtime.hitl._handle_yes(spec, log, brief)
        campaign = spec.campaign_name or "Result"

        if _runtime.audit_logger is not None:
            try:
                _runtime.audit_logger.log_hitl_resolution(
                    session_id=channel,
                    user=session.last_sender,
                    campaign_id=spec.campaign_code,
                    hitl_outcome=HITL_YES,
                )
            except Exception:
                pass

        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=(
                f"*Saved!* The '{campaign}' result has been confirmed as a verified "
                "blueprint. I'll use it as a reference for future requests."
            ),
        )
    except Exception as exc:
        _log.warning("HITL yes failed: %s", exc)
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="Result noted. Thanks for confirming!",
        )


@app.action(HITL_ACTION_REVIEW)
def handle_hitl_review(ack, body, client) -> None:
    ack()
    channel: str = body["channel"]["id"]
    thread_ts: str = body["message"].get("thread_ts") or body["message"]["ts"]
    session = _session_store.get(channel)

    log = session.last_audit_log
    brief = session.last_brief
    intent_type = session.last_intent.intent_type if session.last_intent else ""

    if intent_type in ("brief_generation", "brief_qa") and brief is not None:
        blocks = format_sources_detail(brief)
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="Here is how this brief was built.",
            blocks=blocks,
        )
        return

    if log is not None and log.sql:
        blocks = format_sql_detail(log)
        client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text="Here is the SQL waterfall query that built this audience.",
            blocks=blocks,
        )
        return

    client.chat_postMessage(
        channel=channel, thread_ts=thread_ts,
        text="No detail is available for this result.",
    )


@app.action(HITL_ACTION_NO)
def handle_hitl_no(ack, body, client) -> None:
    ack()
    channel: str = body["channel"]["id"]
    thread_ts: str = body["message"].get("thread_ts") or body["message"]["ts"]
    session = _session_store.get(channel)

    session.awaiting_correction = True
    session.hitl_pending = False

    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=(
            "What should be different? Type it here in the thread and I'll take note "
            "of it for future improvements."
        ),
    )


# ---------------------------------------------------------------------------
# Pipeline execution (runs in background thread)
# ---------------------------------------------------------------------------

def _post(channel: str, thread_ts: str, text: str) -> None:
    """Post a plain text message to a thread. Safe to call from any thread."""
    try:
        app.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)
    except Exception as exc:
        _log.warning("_post failed: %s", exc)


def _process_query(
    message_text: str,
    channel: str,
    thread_ts: str,
    user: str,
    session,
) -> None:
    """Full pipeline execution with progressive Slack updates. Runs in a thread.

    Two-stage split:
      Stage 1: classify_intent() -- fast LLM call (~2s)
      Stage 2: route_by_intent() -- the main work, may include BigQuery (~10-30s)

    A progress message is posted after each stage so the user sees the bot
    thinking through their request step by step.
    """
    rt = _runtime
    if rt is None:
        _post(channel, thread_ts, "Runtime not ready -- please try again in a moment.")
        return

    _start = time.perf_counter()

    try:
        # --- Stage 1a: Acknowledge immediately ---
        _post(channel, thread_ts, "Got it! Let me look into that for you...")

        # --- Stage 1b: Build glossary context + classify intent ---
        ctx = _build_dynamic_context(message_text, rt.gold_index)
        if ctx:
            rt.nexus.set_session_context(ctx)
            rt.quant.set_session_context(ctx)

        intent: IntentClassification = rt.nexus.classify_intent(message_text)

        # --- Stage 1c: Post an intent-aware progress message ---
        _post(channel, thread_ts, _intent_progress_message(intent))

        # --- Stage 2a: For sizing requests, warn the user before BigQuery starts ---
        if intent.intent_type in ("sizing_request", "campaign_execution"):
            _post(
                channel, thread_ts,
                "Running the audience analysis in BigQuery now. "
                "This usually takes about 15-20 seconds -- hang tight!",
            )

        # --- Stage 2b: Run the rest of the pipeline ---
        spec, log, brief_output = route_by_intent(
            rt.nexus,
            rt.quant,
            rt.briefing,
            intent,
            message_text,
            rt.gold_index,
            rt.schema_snapshot.to_dict(),
            rt.rules_registry,
            allow_interactive=False,
        )

        # --- Store result for HITL button clicks ---
        if log is not None or brief_output is not None:
            session.store_result(
                query=message_text,
                intent=intent,
                spec=spec,
                log=log,
                brief=brief_output,
            )

        # --- Post final result ---
        _post_result(channel, thread_ts, intent, log, brief_output)

        # --- Write audit log entry ---
        if rt.audit_logger is not None:
            try:
                rt.audit_logger.log(
                    session_id=channel,
                    user=user,
                    intent_type=intent.intent_type,
                    campaign_id=spec.campaign_code if spec else None,
                    sql=log.sql if log else None,
                    agent_called=_agent_called_for_intent(intent.intent_type),
                    hitl_outcome=None,
                    duration_ms=int((time.perf_counter() - _start) * 1000),
                )
            except Exception:
                pass

    except Exception as exc:
        _log.exception("_process_query failed: %s", exc)
        _post(
            channel, thread_ts,
            "I ran into an issue and couldn't complete your request. "
            "Please try rephrasing, or check that the campaign name is correct.",
        )


def _process_correction(
    correction: str,
    channel: str,
    thread_ts: str,
    session,
) -> None:
    """Run FeedbackAgent for a free-text correction. Runs in a thread."""
    _post(channel, thread_ts, "Thank you for that! Working through your feedback now...")

    rt = _runtime
    if rt is None:
        return

    try:
        _run_adhoc_feedback(
            session.last_spec,
            session.last_audit_log,
            session.last_query,
            correction,
            knowledge_ctx=rt.knowledge_ctx,
        )
        session.clear_hitl()
        _post(
            channel, thread_ts,
            "Got it -- I have noted that correction and will apply it to future requests.",
        )
    except Exception as exc:
        _log.warning("_process_correction failed: %s", exc)
        _post(
            channel, thread_ts,
            "Your feedback has been noted. I'll use it to improve future responses.",
        )


# ---------------------------------------------------------------------------
# Helper: intent-aware progress message
# ---------------------------------------------------------------------------

def _intent_progress_message(intent: IntentClassification) -> str:
    """Return a warm second-stage progress message based on intent type and campaign."""
    campaign_code = intent.campaign_code or ""
    campaign_label = f"*{campaign_code}*" if campaign_code else ""
    intent_type = intent.intent_type

    if campaign_label and intent_type == "sizing_request":
        return (
            f"I can see you are asking about the {campaign_label} campaign. "
            "Pulling the targeting blueprint and checking the business rules now..."
        )
    if campaign_label and intent_type in ("brief_generation", "brief_qa"):
        return (
            f"Found the {campaign_label} campaign. "
            "Assembling the campaign intelligence brief now..."
        )
    if campaign_label and intent_type == "campaign_execution":
        return (
            f"Working on a full execution package for {campaign_label}. "
            "Building the audience waterfall and brief together -- this will take a moment..."
        )
    if intent_type == "sizing_request":
        return (
            "Understood -- sizing an ad-hoc audience. "
            "Checking the schema and building the waterfall query..."
        )
    if intent_type in ("brief_generation", "brief_qa"):
        return "On it! Pulling together the campaign brief now..."
    # general_question or unknown
    return "On it! Checking the knowledge base..."


# ---------------------------------------------------------------------------
# Helper: post final pipeline result
# ---------------------------------------------------------------------------

def _post_result(
    channel: str,
    thread_ts: str,
    intent: IntentClassification,
    log: Optional[QuantAuditLog],
    brief_output: Optional[BriefingOutput],
) -> None:
    """Format the pipeline result and post it to the Slack thread."""
    if log is not None and brief_output is not None:
        blocks = format_combined_result(log, brief_output)
        fallback = "Vibe OCTO -- Campaign Execution complete"
    elif log is not None:
        blocks = format_sizing_result(log)
        fallback = "Vibe OCTO -- Audience Sizing complete"
    elif brief_output is not None:
        blocks = format_brief_result(brief_output)
        fallback = "Vibe OCTO -- Campaign Brief ready"
    else:
        # general_question: the answer was printed to the server terminal by
        # route_by_intent. Post a helpful fallback prompt.
        _post(
            channel, thread_ts,
            "I've checked the knowledge base. For more specific results, try asking "
            "about a named campaign -- for example: 'Size the AAL BAUD campaign' or "
            "'Give me a brief for the AAL campaign'.",
        )
        return

    try:
        app.client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=fallback,
            blocks=blocks,
        )
    except Exception as exc:
        _log.warning("_post_result blocks failed (%s); posting plain text fallback", exc)
        _post(channel, thread_ts, fallback)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    global _runtime

    _log.info("Vibe OCTO Slack bot starting -- initializing runtime...")
    _runtime = build_runtime()

    fuelix_api_key = os.getenv("FUELIX_API_KEY", "")
    bq_project = os.getenv("BQ_PROJECT_ID", "bi-srv-hsmdet-pr-7b9def")
    run_startup_health_check(
        api_key=fuelix_api_key,
        bq_project=bq_project,
        artifacts_dir=_ARTIFACTS_DIR,
    )

    # Validate Slack credentials before starting (deferred from module init).
    try:
        app.client.auth_test()
        _log.info("Slack credentials validated.")
    except Exception as exc:
        raise SystemExit(
            f"Slack token validation failed: {exc}\n"
            "Check that SLACK_BOT_TOKEN is set to a valid xoxb-... token."
        ) from exc

    socket_mode = os.getenv("SLACK_SOCKET_MODE", "true").lower() != "false"

    if socket_mode:
        if not _APP_TOKEN:
            raise EnvironmentError(
                "SLACK_APP_TOKEN is not set. "
                "Generate an App-Level Token with 'connections:write' scope at "
                "api.slack.com/apps -- Basic Information -- App-Level Tokens. "
                "Set SLACK_SOCKET_MODE=false to use HTTP mode instead."
            )
        _log.info("Connected to Slack in Socket Mode. Bot is ready.")
        handler = SocketModeHandler(app, _APP_TOKEN)
        handler.start()
    else:
        # HTTP mode for internal server deployment using FastAPI (already installed).
        from fastapi import FastAPI, Request  # noqa: PLC0415
        from fastapi.responses import JSONResponse  # noqa: PLC0415
        from slack_bolt.adapter.fastapi import SlackRequestHandler  # noqa: PLC0415
        import uvicorn  # noqa: PLC0415

        fastapi_app = FastAPI(title="Vibe OCTO -- Slack HTTP", version="4.0.0")
        bolt_handler = SlackRequestHandler(app)

        @fastapi_app.post("/slack/events")
        async def slack_events(req: Request) -> JSONResponse:
            return await bolt_handler.handle(req)

        port = int(os.getenv("SLACK_PORT", "3000"))
        _log.info("Starting Slack bot in HTTP mode on port %d...", port)
        uvicorn.run(fastapi_app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
