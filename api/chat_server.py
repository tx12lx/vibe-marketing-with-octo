"""
api/chat_server.py -- Vibe OCTO Google Chat HTTP endpoint.

Receives events from Google Chat (MESSAGE, CARD_CLICKED, ADDED_TO_SPACE) via
webhook HTTP POST and returns Google Chat Card v2 responses.

Processing model:
  All pipeline work runs in a thread-pool executor so the async FastAPI loop
  is not blocked, while still completing within Google Chat's 30-second
  synchronous response window for typical requests.

How to run for local testing with ngrok:
  Terminal 1:
    cd "path/to/Vibe Marketing with OCTO"
    uvicorn api.chat_server:app --reload --port 8000

  Terminal 2:
    ngrok http 8000

  Then in Google Cloud Console -> Google Chat API -> Configuration:
    App URL: https://<ngrok-id>.ngrok.io/chat

For team server deployment:
  uvicorn api.chat_server:app --host 0.0.0.0 --port 8000 --workers 1
  (use workers=1 to keep the shared VibeRuntime consistent)

HITL flow:
  1. User sends a message -> pipeline runs -> result card posted with 3 buttons
  2. User clicks a button -> CARD_CLICKED event -> /chat handler routes to HITL
     - "Looks good!"       -> save result (HITLAuditLoop._handle_yes), confirm card
     - "Show how it built" -> SQL or brief sources card (no state change)
     - "Something's wrong" -> text prompt; session.awaiting_correction = True
  3. User types correction -> processed by FeedbackAgent, confirmation sent
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Ensure project root is on sys.path so all project imports resolve.
# chat_server.py lives in api/ which is one level below the project root.
# ---------------------------------------------------------------------------
_API_DIR = Path(__file__).resolve().parent
_ROOT = _API_DIR.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from vibe_orchestrator import (  # noqa: E402
    VibeRuntime,
    RequestResult,
    build_runtime,
    process_core_request,
    _ARTIFACTS_DIR,
)
from core.audit_logger import HITL_YES  # noqa: E402
from core.resilience import run_startup_health_check  # noqa: E402
from pydantic_schemas import BriefingOutput, QuantAuditLog, UniversalJSONSpec  # noqa: E402
from api.session_store import SessionStore  # noqa: E402
from api.chat_formatter import (  # noqa: E402
    format_audit_log_card,
    format_brief_card,
    format_combined_card,
    format_confirmation_card,
    format_error_card,
    format_general_answer_card,
    format_greeting,
    format_sql_card,
    format_brief_sources_card,
)

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# ---------------------------------------------------------------------------
# Shared runtime (initialized once at startup, shared across all requests)
# ---------------------------------------------------------------------------

_runtime: Optional[VibeRuntime] = None
_session_store = SessionStore()
_executor = ThreadPoolExecutor(max_workers=4)

app = FastAPI(title="Vibe OCTO — Google Chat API", version="4.0.0")


@app.on_event("startup")
async def _startup() -> None:
    global _runtime
    _log.info("Vibe OCTO API server starting — initializing runtime...")
    loop = asyncio.get_event_loop()
    _runtime = await loop.run_in_executor(_executor, build_runtime)

    _fuelix_api_key = os.getenv("FUELIX_API_KEY", "")
    _bq_project = os.getenv("BQ_PROJECT_ID", "bi-srv-hsmdet-pr-7b9def")
    run_startup_health_check(
        api_key=_fuelix_api_key,
        bq_project=_bq_project,
        artifacts_dir=_ARTIFACTS_DIR,
    )
    _log.info("Vibe OCTO API server ready.")


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "runtime_ready": _runtime is not None,
        "active_sessions": len(_session_store._sessions),
    }


# ---------------------------------------------------------------------------
# Main Google Chat webhook — receives ALL event types at one URL
# ---------------------------------------------------------------------------

@app.post("/chat")
async def chat_webhook(request: Request) -> JSONResponse:
    """Single endpoint for all Google Chat events.

    Google Chat requires a response within 30 seconds.  Pipeline work runs in
    a thread-pool executor to keep the async loop unblocked while still
    returning the final result synchronously in the webhook response.
    """
    payload: dict = await request.json()
    event_type: str = payload.get("type", "")

    if event_type == "ADDED_TO_SPACE":
        return JSONResponse(format_greeting())

    if event_type == "REMOVED_FROM_SPACE":
        space_name = payload.get("space", {}).get("name", "")
        _session_store.clear(space_name)
        return JSONResponse({})

    if event_type == "MESSAGE":
        return await _handle_message(payload)

    if event_type == "CARD_CLICKED":
        return _handle_card_click(payload)

    return JSONResponse({})


# ---------------------------------------------------------------------------
# Message handler
# ---------------------------------------------------------------------------

async def _handle_message(payload: dict) -> JSONResponse:
    if _runtime is None:
        return JSONResponse({"text": "Vibe OCTO is still starting up. Please try again in a moment."})

    space_name: str = payload.get("space", {}).get("name", "")
    # `argumentText` strips the @-mention prefix; fall back to full message text.
    raw_text: str = (
        payload.get("message", {}).get("argumentText")
        or payload.get("message", {}).get("text", "")
    ).strip()

    # Strip any residual @-mention markup  (<users/xxx> BotName)
    message_text = re.sub(r"<users/[^>]+>\s*\S+\s*", "", raw_text).strip()

    if not message_text:
        return JSONResponse({"text": "I didn't catch that. Could you rephrase your question?"})

    sender_name: str = (
        payload.get("message", {}).get("sender", {}).get("displayName", "unknown")
    )
    session = _session_store.get(space_name)

    # If the user previously clicked "Something's wrong", treat this message as
    # a free-text correction for FeedbackAgent.
    if session.awaiting_correction:
        session.awaiting_correction = False
        loop = asyncio.get_event_loop()
        response_body = await loop.run_in_executor(
            _executor,
            _process_correction_sync,
            message_text,
            session.last_spec,
            session.last_audit_log,
            session.last_query,
        )
        session.clear_hitl()
        return JSONResponse(response_body)

    # Normal request — run the pipeline synchronously in the thread pool.
    loop = asyncio.get_event_loop()
    response_body = await loop.run_in_executor(
        _executor,
        _process_query_sync,
        message_text,
        space_name,
        sender_name,
    )
    return JSONResponse(response_body)


# ---------------------------------------------------------------------------
# Card button handler
# ---------------------------------------------------------------------------

def _handle_card_click(payload: dict) -> JSONResponse:
    space_name: str = payload.get("space", {}).get("name", "")
    # Google Chat Cards v2 sends the function name in action.function
    action = payload.get("action", {})
    function_name: str = action.get("function") or action.get("actionMethodName", "")

    session = _session_store.get(space_name)

    if function_name == "hitl_yes":
        body = _handle_hitl_yes(session, space_name=space_name)
        session.clear_hitl()
        return JSONResponse({"actionResponse": {"type": "UPDATE_MESSAGE"}, **body})

    if function_name == "hitl_review":
        body = _build_review_response(session)
        # POST as a new message so the original result card stays intact.
        return JSONResponse({"actionResponse": {"type": "NEW_MESSAGE"}, **body})

    if function_name == "hitl_no":
        session.awaiting_correction = True
        session.hitl_pending = False
        return JSONResponse({
            "actionResponse": {"type": "NEW_MESSAGE"},
            "text": (
                "No problem! Please describe what looks wrong in your next message "
                "and I'll take note of it for future improvements."
            ),
        })

    return JSONResponse({})


# ---------------------------------------------------------------------------
# Sync pipeline execution (runs in thread pool)
# ---------------------------------------------------------------------------

def _process_query_sync(message_text: str, space_name: str, user: str = "unknown") -> dict:
    """Run process_core_request and format the result as a Chat card dict."""
    result: RequestResult = process_core_request(
        message_text, _runtime, session_id=space_name, user=user
    )
    session = _session_store.get(space_name)

    if result.error:
        return format_error_card(
            "I ran into an issue and couldn't complete your request.",
            f"Error details: {result.error[:200]}",
        )

    intent_type = result.intent.intent_type if result.intent else "general_question"

    # Store result for HITL (before formatting so session is ready for button clicks)
    if result.log is not None or result.brief_output is not None:
        session.store_result(
            query=message_text,
            intent=result.intent,
            spec=result.spec,
            log=result.log,
            brief=result.brief_output,
        )

    # Format based on what was returned
    if result.log is not None and result.brief_output is not None:
        return format_combined_card(result.log, result.brief_output, include_hitl=True)

    if result.log is not None:
        return format_audit_log_card(result.log, include_hitl=True)

    if result.brief_output is not None:
        return format_brief_card(result.brief_output, include_hitl=True)

    if intent_type == "general_question":
        # answer_general_question() already printed to stdout; re-request from pipeline
        # is not available here. Provide a fallback card.
        return {"text": "I've answered your question above. Is there anything else I can help with?"}

    return format_error_card(
        "I understood your request but couldn't produce a result.",
        "Try rephrasing, or check that the campaign name is correct.",
    )


def _process_correction_sync(
    correction: str,
    spec: Optional[UniversalJSONSpec],
    log: Optional[QuantAuditLog],
    query: str,
) -> dict:
    """Run FeedbackAgent for a free-text correction and return a confirmation dict."""
    if _runtime is None:
        return {"text": "Runtime not ready. Please try again."}
    try:
        from vibe_orchestrator import _run_adhoc_feedback  # noqa: PLC0415
        _run_adhoc_feedback(spec, log, query, correction, knowledge_ctx=_runtime.knowledge_ctx)
        return {"text": "Thanks for the feedback! I've noted your correction and will apply it going forward."}
    except Exception as exc:
        _log.warning("_process_correction_sync failed: %s", exc)
        return {"text": "Your feedback has been noted. I'll use it to improve future responses."}


# ---------------------------------------------------------------------------
# HITL action handlers
# ---------------------------------------------------------------------------

def _handle_hitl_yes(session, space_name: str = "") -> dict:
    """Save the result via HITLAuditLoop._handle_yes() and return a confirmation card."""
    if _runtime is None:
        return format_error_card("Runtime not available.")

    spec = session.last_spec
    log = session.last_audit_log
    brief = session.last_brief

    if spec is not None and log is not None:
        try:
            _runtime.hitl._handle_yes(spec, log, brief)
            campaign = spec.campaign_name or "Result"

            # Audit: log HITL resolution as a follow-up entry (pipeline entry was
            # already written in process_core_request with hitl_outcome=None).
            if _runtime.audit_logger is not None:
                try:
                    _runtime.audit_logger.log_hitl_resolution(
                        session_id=space_name or "api",
                        user=session.last_sender if hasattr(session, "last_sender") else "unknown",
                        campaign_id=spec.campaign_code,
                        hitl_outcome=HITL_YES,
                    )
                except Exception:
                    pass

            return format_confirmation_card(
                f"'{campaign}' has been saved as a verified blueprint.\n"
                "I'll use it as a reference for future requests."
            )
        except Exception as exc:
            _log.warning("HITL yes failed: %s", exc)
            return format_confirmation_card("Result noted. Thanks for confirming!")

    return format_confirmation_card("Result confirmed. Thanks!")


def _build_review_response(session) -> dict:
    """Build the 'Show how it was built' response card."""
    log = session.last_audit_log
    brief = session.last_brief
    intent_type = session.last_intent.intent_type if session.last_intent else ""

    if intent_type in ("brief_generation", "brief_qa") and brief is not None:
        return format_brief_sources_card(brief)

    if log is not None and log.sql:
        return format_sql_card(log.sql)

    return {"text": "No detail is available for this result."}
