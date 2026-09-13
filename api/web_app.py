"""
api/web_app.py -- Vibe OCTO browser chat interface.

Serves a professional HTML chat page and handles audience sizing queries,
HITL button clicks, and correction submissions over HTTP.

Multiple concurrent users are supported via per-session state isolation.
Writes to the shared knowledge layer (knowledge/store.py's SQLite database --
confirmed corrections, new business rules) are protected by a threading.Lock
so simultaneous corrections from two users cannot race each other.

How to run:
    uvicorn api.web_app:app --host 0.0.0.0 --port 3000

Then open http://localhost:3000 in any browser on the same network.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import queue
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import parse_qs

_API_DIR = Path(__file__).resolve().parent
_ROOT = _API_DIR.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env")

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse  # noqa: E402

from vibe_orchestrator import (  # noqa: E402
    RequestResult,
    VibeRuntime,
    build_runtime,
    generate_stuck_explanation,
    process_core_request,
)
from core.admin_routes import register_admin_status_page, register_knowledge_db_admin_routes  # noqa: E402
from core.audit_logger import HITL_NO, HITL_REVIEW_NO, HITL_REVIEW_YES, HITL_YES  # noqa: E402
from core.resilience import run_startup_health_check  # noqa: E402
from core.thought_display import ThoughtDisplay  # noqa: E402
from pydantic_schemas import NexusErrorPayload, QuantAuditLog  # noqa: E402
from api.session_store import SessionStore  # noqa: E402

_log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

_runtime: Optional[VibeRuntime] = None
_session_store = SessionStore()

# Intents whose result actually drives real customer targeting -- for these,
# looking at the evidence (SQL) is required before a "yes" is accepted, not
# just offered as an optional button. See hitl_endpoint().
_HIGH_IMPACT_INTENTS = frozenset({"sizing_request"})
_executor = ThreadPoolExecutor(max_workers=4)
_write_lock = threading.Lock()

_TEMPLATES_DIR = _API_DIR / "templates"

app = FastAPI(title="Vibe Marketing with OCTO — Web Interface", version="4.0.0")

# /admin and /admin/knowledge-db were previously only wired up on web_cloud_run.py,
# an entry point this VM deployment doesn't actually run -- so the durability status
# (including whether GitHub sync is silently failing) had no way to reach anyone
# looking at the app actually running in production. Registered here too, on the
# real live surface, gated by the same shared-password/IAP layer as everything else.
register_knowledge_db_admin_routes(app)
register_admin_status_page(app, get_schema_sync_status=lambda: schema_sync_status)


# ---------------------------------------------------------------------------
# Shared-password gate
#
# This is a lightweight pilot-stage lock, not real per-user authentication --
# everyone who is given the password shares one "door." It only activates if
# WEB_APP_PASSWORD is set in .env, so it's opt-in and doesn't change behavior
# for anyone already running this locally without it.
# ---------------------------------------------------------------------------

_WEB_APP_PASSWORD = os.getenv("WEB_APP_PASSWORD", "")
_AUTH_COOKIE_NAME = "octo_auth"
_AUTH_SECRET = os.getenv("WEB_APP_SECRET") or secrets.token_hex(32)
_PUBLIC_PATHS = {"/health", "/login", "/favicon.ico", "/favicon.svg"}

_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20">'
    '<rect width="20" height="20" rx="4" fill="#4B2E8C"/>'
    '<path fill="#fff" d="M10 4a6 6 0 100 12A6 6 0 0010 4zm0 1.5a4.5 4.5 0 110 9 4.5 4.5 0 010-9zm0 2a1 1 0 100 2 1 1 0 000-2zm-.75 3v3h1.5v-3H9.25z"/>'
    "</svg>"
)


def _auth_enabled() -> bool:
    return bool(_WEB_APP_PASSWORD)


def _caller_identity(request: Request) -> str:
    """Best-effort real identity of whoever is making this request.

    IAP (which fronts this app in production -- see web_cloud_run.py) puts the
    signed-in person's email in this header before the request ever reaches
    us; prefer it over a generic placeholder so a HITL "yes" or a correction
    is attributed to the actual person, not to the literal word "web" for
    everyone. Falls back to "web-unverified" when IAP isn't in front of this
    instance (e.g. local dev, or the shared-password gate used on its own) --
    that fallback is intentionally distinct from a real identity so it's
    obvious in the audit trail that no real identity was available, rather
    than quietly mislabeling every different person the same way.
    """
    raw = request.headers.get("x-goog-authenticated-user-email", "")
    if raw:
        return raw.split(":", 1)[-1] or "web-unverified"
    return "web-unverified"


def _expected_auth_cookie_value() -> str:
    return hmac.new(_AUTH_SECRET.encode(), b"vibe-octo-authenticated", "sha256").hexdigest()


def _is_authenticated(request: Request) -> bool:
    if not _auth_enabled():
        return True
    cookie_value = request.cookies.get(_AUTH_COOKIE_NAME, "")
    return hmac.compare_digest(cookie_value, _expected_auth_cookie_value())


@app.middleware("http")
async def _require_shared_password(request: Request, call_next):
    if not _auth_enabled() or request.url.path in _PUBLIC_PATHS:
        return await call_next(request)

    if not _is_authenticated(request):
        if request.url.path == "/" or request.method == "GET":
            return RedirectResponse(url="/login")
        return JSONResponse({"type": "error", "message": "Session expired. Please refresh the page and sign in again."}, status_code=401)

    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
async def login_form(error: str = "") -> HTMLResponse:
    html = (_TEMPLATES_DIR / "login.html").read_text(encoding="utf-8")
    error_html = '<div class="login-error">Incorrect password. Please try again.</div>' if error else ""
    html = html.replace("{{ERROR}}", error_html)
    return HTMLResponse(content=html)


@app.post("/login")
async def login_submit(request: Request) -> RedirectResponse:
    # Parsed manually (rather than via request.form()) so this doesn't need
    # the python-multipart package installed -- the login form is a plain
    # application/x-www-form-urlencoded POST with a single field.
    body = (await request.body()).decode("utf-8", errors="ignore")
    submitted = parse_qs(body).get("password", [""])[0]

    if _auth_enabled() and hmac.compare_digest(submitted, _WEB_APP_PASSWORD):
        response = RedirectResponse(url="/", status_code=303)
        response.set_cookie(
            _AUTH_COOKIE_NAME,
            _expected_auth_cookie_value(),
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,
        )
        return response

    return RedirectResponse(url="/login?error=1", status_code=303)


@app.get("/favicon.svg", include_in_schema=False)
async def favicon_svg() -> Response:
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico() -> Response:
    # Browsers request this by default even when a <link rel="icon"> is set
    # in the page; respond quietly instead of letting it 404 in the logs.
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

#: Set once the background schema-sync thread finishes (success or failure) -- see
#: /health and /admin's status line. This exists because a prior schema-sync failure
#: was completely invisible (no logs reached Cloud Logging on that deployment) and went
#: undiagnosed for a full day; this status is readable independent of any log pipeline.
schema_sync_status: dict = {"state": "not started"}


def _sync_schema_thread() -> None:
    """Sync the knowledge layer's schema in the background, not blocking startup.

    A real Cloud Run deploy showed this can take longer than Cloud Run's
    startup-probe timeout on a cold container -- if it ran before the app
    started serving, a slow (or hanging) sync could mean the container never
    opens its port at all and never starts. Running it here instead means the
    app is reachable immediately; queries in the first few seconds may see an
    empty or stale schema until this finishes, which is the same graceful
    degradation the knowledge layer already handles (see knowledge/store.py's
    check_health()), not a new failure mode.
    """
    def _run() -> None:
        global schema_sync_status
        schema_sync_status = {"state": "running"}
        try:
            from knowledge.sync_schema import main as sync_schema_main  # noqa: PLC0415

            _log.info("Syncing the knowledge layer's schema in the background...")
            sync_schema_main()
            _log.info("Knowledge layer schema sync complete.")
            schema_sync_status = {"state": "ok"}
        except Exception as exc:
            _log.exception("Schema sync failed (the app keeps serving with whatever schema was already synced).")
            schema_sync_status = {"state": "failed", "error": f"{type(exc).__name__}: {exc}"}

    threading.Thread(target=_run, daemon=True).start()


@app.on_event("startup")
async def _startup() -> None:
    global _runtime
    _log.info("Vibe OCTO web server starting — initializing runtime...")
    _sync_schema_thread()
    loop = asyncio.get_event_loop()
    _runtime = await loop.run_in_executor(_executor, build_runtime)
    # Rebuild the local knowledge cache (business rules, glossary, feedback) from its
    # durable GitHub-backed copy -- this container's own disk never survives a restart,
    # see knowledge/git_store.py. A no-op if GITHUB_TOKEN isn't set.
    await loop.run_in_executor(_executor, _runtime.knowledge_ctx.hydrate_from_github)

    fuelix_key = os.getenv("FUELIX_API_KEY", "")
    bq_project = os.getenv("BQ_PROJECT_ID", "bi-srv-hsmdet-pr-7b9def")
    run_startup_health_check(
        api_key=fuelix_key,
        bq_project=bq_project,
    )
    if _auth_enabled():
        _log.info("Shared-password gate is ON (WEB_APP_PASSWORD is set).")
    else:
        _log.warning("Shared-password gate is OFF -- anyone who can reach this address can use the tool. Set WEB_APP_PASSWORD in .env before sharing this beyond your own machine.")
    _log.info("Vibe OCTO web server ready.")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    business_rules_count = 0
    if _runtime is not None:
        try:
            business_rules_count = _runtime.knowledge_ctx.business_rules_count
        except Exception:
            pass
    return {
        "status": "ok",
        "runtime_ready": _runtime is not None,
        "business_rules_count": business_rules_count,
        "active_sessions": len(_session_store._sessions),
        "schema_sync": schema_sync_status,
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    business_rules_count = 0
    if _runtime is not None:
        try:
            business_rules_count = _runtime.knowledge_ctx.business_rules_count
        except Exception:
            pass
    html = (_TEMPLATES_DIR / "chat.html").read_text(encoding="utf-8")
    html = html.replace("{{RULES_COUNT}}", str(business_rules_count))
    return HTMLResponse(content=html)


@app.post("/query")
async def query_endpoint(request: Request) -> StreamingResponse:
    """Streams the tool's reasoning as it happens (Server-Sent Events), ending with
    the same result payload /query always returned -- see _stream_query_events()."""
    body = await request.json()
    session_id: str = body.get("session_id", "")
    text: str = body.get("text", "").strip()

    if not session_id or not text:
        raise HTTPException(status_code=400, detail="session_id and text are required")
    if _runtime is None:
        async def _not_ready() -> Iterator[str]:
            yield _sse_event({
                "type": "final",
                "data": {"type": "error", "message": "The tool is still starting up. Please try again in a moment."},
            })
        return StreamingResponse(_not_ready(), media_type="text/event-stream")

    _session_store.get(session_id).last_sender = _caller_identity(request)
    return StreamingResponse(_stream_query_events(text, session_id), media_type="text/event-stream")


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


async def _stream_query_events(text: str, session_id: str):
    """Runs _process_query_sync() in the background exactly as before, but streams
    each short, plain-English progress line (see core/thought_display.py's stream
    sink) to the browser as it happens, then a final event carrying the same result
    dict /query always returned -- api/templates/chat.html renders that final event
    exactly the way it rendered the old single JSON response.
    """
    q: "queue.Queue" = queue.Queue()
    loop = asyncio.get_event_loop()

    def _run() -> None:
        ThoughtDisplay.set_stream_sink(lambda message: q.put({"type": "progress", "message": message}))
        try:
            result = _process_query_sync(text, session_id)
        except Exception as exc:  # noqa: BLE001 -- a request must always end with SOME final event
            _log.exception("Unhandled error while streaming a query")
            result = {
                "type": "error",
                "message": "I ran into an issue and couldn't complete your request.",
                "detail": str(exc)[:300],
            }
        finally:
            ThoughtDisplay.clear_stream_sink()
        q.put({"type": "final", "data": result})
        q.put(None)  # sentinel -- tells the generator below there's nothing more coming

    future = loop.run_in_executor(_executor, _run)
    while True:
        item = await loop.run_in_executor(None, q.get)
        if item is None:
            break
        yield _sse_event(item)
    await future  # surface a scheduling error, if any; _run() itself never raises past its own try/except


@app.post("/hitl")
async def hitl_endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    session_id: str = body.get("session_id", "")
    action: str = body.get("action", "")

    if not session_id or action not in ("yes", "review", "no"):
        raise HTTPException(status_code=400, detail="session_id and valid action (yes/review/no) are required")
    if _runtime is None:
        return JSONResponse({"type": "error", "message": "Runtime not available."})

    session = _session_store.get(session_id)
    session.last_sender = _caller_identity(request)

    if action == "yes":
        intent_type = session.last_intent.intent_type if session.last_intent else ""
        if intent_type in _HIGH_IMPACT_INTENTS and not session.reviewed_this_result:
            # Enforced here, not just left to the UI's button order -- a direct API
            # call can't skip past looking at the evidence either. See the "review
            # before approve" gap this closes: previously nothing stopped someone
            # from confirming a sizing result sight-unseen.
            return JSONResponse({
                "type": "review_required",
                "message": (
                    "This result affects real customer targeting -- please review how it "
                    "was built before confirming it's correct."
                ),
            })
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(_executor, _handle_hitl_yes_sync, session, session_id)
        session.clear_hitl()
        return JSONResponse(result)

    if action == "review":
        # reviewed_this_result is only set once real SQL was actually returned to look
        # at -- if _build_review_response falls through to its "no detail available"
        # branch, the person never actually saw a query, so "yes" must still refuse them
        # (see the review-required check above).
        review = _build_review_response(session)
        if review.get("type") == "review_sql":
            session.reviewed_this_result = True
        return JSONResponse(review)

    # action == "no"
    _log_hitl_resolution(session, session_id, HITL_REVIEW_NO if session.reviewed_this_result else HITL_NO)
    session.awaiting_correction = True
    return JSONResponse({
        "type": "correction_prompt",
        "message": "No problem! Please describe what looks wrong in your own words -- no need to be technical.",
    })


@app.post("/correction")
async def correction_endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    session_id: str = body.get("session_id", "")
    text: str = body.get("text", "").strip()

    if not session_id or not text:
        raise HTTPException(status_code=400, detail="session_id and text are required")
    if _runtime is None:
        return JSONResponse({"type": "error", "message": "Runtime not available."})

    session = _session_store.get(session_id)
    session.last_sender = _caller_identity(request)
    session.awaiting_correction = False

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _executor,
        _process_correction_sync,
        text,
        session.last_audit_log,
        session.last_query,
        session,
    )

    session.clear_hitl()
    return JSONResponse(result)


@app.post("/confirm-correction")
async def confirm_correction_endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    session_id: str = body.get("session_id", "")

    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    if _runtime is None:
        return JSONResponse({"type": "error", "message": "Runtime not available."})

    session = _session_store.get(session_id)
    session.last_sender = _caller_identity(request)
    pending = session.pending_correction

    if not pending:
        return JSONResponse({
            "type": "correction_confirmed",
            "message": "Got it! Your feedback has been noted.",
        })

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _executor,
        _save_confirmed_correction_sync,
        session,
        pending,
        session_id,
    )
    return JSONResponse(result)


@app.post("/clarify-query")
async def clarify_query_endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    session_id: str = body.get("session_id", "")
    text: str = body.get("text", "").strip()

    if not session_id or not text:
        raise HTTPException(status_code=400, detail="session_id and text are required")
    if _runtime is None:
        return JSONResponse({"type": "error", "message": "Runtime not available."})

    session = _session_store.get(session_id)
    session.last_sender = _caller_identity(request)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _executor,
        _process_clarification_sync,
        text,
        session_id,
        session,
    )
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Sync pipeline functions (all run in thread pool executor)
# ---------------------------------------------------------------------------

def _process_query_sync(text: str, session_id: str) -> dict:
    session = _session_store.get(session_id)
    result: RequestResult = process_core_request(
        text, _runtime,
        session_id=session_id,
        session_corrections=session.active_corrections if session.active_corrections else None,
        transcript=session.transcript if session.transcript else None,
    )

    if result.error:
        return {
            "type": "error",
            "message": "I ran into an issue and couldn't complete your request.",
            "detail": result.error[:300],
        }

    if result.log is not None:
        session.store_result(query=text, intent=result.intent, log=result.log)

    processing_notes = _build_processing_notes(result.intent)
    intent_type = result.intent.intent_type if result.intent else "general_question"

    if intent_type == "general_question":
        message = result.answer_text or "I've answered from the knowledge base. Is there anything else I can help with?"
        session.record_turn(text, message)
        return {
            "type": "general_answer",
            "processing_notes": processing_notes,
            "message": message,
        }

    # If a sizing request produced nothing useful, use the AI to generate a
    # warm explanation and ask the user for a clarifying detail.
    if result.log is None:
        session.last_query = text  # store for retry without user retyping
        explanation = generate_stuck_explanation(_runtime.nexus, text, result.intent, result.stuck_reason)
        return {
            "type": "stuck",
            "explanation": explanation,
            "processing_notes": processing_notes,
            "intent_type": intent_type,
        }

    if result.log is not None:
        session.record_turn(
            text,
            f"Sized \"{result.log.request.target_population}\" -> {result.log.final_count:,} "
            f"({result.log.request.audience_label or 'ad-hoc request'})",
        )
    return _format_result(result, processing_notes)


def _format_result(result: RequestResult, processing_notes: list) -> dict:
    out: dict = {
        "type": "result",
        "processing_notes": processing_notes,
        "intent_type": result.intent.intent_type if result.intent else "",
        "error": None,
        # Defaults so the frontend never KeyErrors
        "audience_label": "",
        "medium": "",
        "cadence": "",
        "audience_count": None,
        "optimization_note": None,
        "confidence": None,
        "knowledge_sources": [],
    }

    if result.log:
        out["audience_label"] = result.log.request.audience_label or ""
        out["medium"] = result.log.request.medium
        out["cadence"] = result.log.request.cadence
        out["audience_count"] = result.log.final_count
        out["optimization_note"] = result.log.optimization_note

    if result.intent:
        out["confidence"] = round(result.intent.confidence * 100)
        out["knowledge_sources"] = result.intent.knowledge_sources_consulted or []

    return out


def _build_processing_notes(intent) -> list:
    notes = []
    if intent is None:
        return notes

    for src in (intent.knowledge_sources_consulted or []):
        src_lower = src.lower()
        if any(kw in src_lower for kw in ("glossary", "term", "acronym", "mapped")):
            notes.append(src)

    rules = intent.business_rules_applied or []
    if len(rules) == 1:
        notes.append("Applying 1 verified business rule")
    elif len(rules) > 1:
        notes.append(f"Applying {len(rules)} verified business rules")

    return notes


def _log_hitl_resolution(session, session_id: str, outcome: str) -> None:
    """Record a HITL resolution (yes / no, review-qualified or not) to the audit log.

    Covers all three buttons -- previously only "yes" was ever logged, so the
    audit trail silently had no record of "something looks wrong" or of
    someone reviewing the evidence before deciding. Never blocks the
    user-facing response on a logging failure.
    """
    if _runtime is None or _runtime.audit_logger is None:
        return
    try:
        _runtime.audit_logger.log_hitl_resolution(
            session_id=session_id,
            user=getattr(session, "last_sender", "web-unverified"),
            hitl_outcome=outcome,
        )
    except Exception:
        pass


def _handle_hitl_yes_sync(session, session_id: str) -> dict:
    log = session.last_audit_log

    if log is None:
        return {
            "type": "hitl_confirmed",
            "message": "Result confirmed. Thanks!",
            "detail": "",
        }

    audience_label = log.request.audience_label or "this request"
    confirmation_saved = False
    user_identity = getattr(session, "last_sender", "web-unverified")

    with _write_lock:
        if _runtime.knowledge_ctx is not None:
            try:
                _runtime.knowledge_ctx.record_confirmation(
                    user_identity=user_identity,
                )
                confirmation_saved = True
            except Exception as exc:
                _log.warning("Knowledge-layer confirmation write failed: %s", exc)

    # session.reviewed_this_result is read before the endpoint clears HITL
    # state, so this still reflects whether the evidence was actually looked
    # at before "yes" -- see the review-before-approve gate in hitl_endpoint().
    _log_hitl_resolution(session, session_id, HITL_REVIEW_YES if session.reviewed_this_result else HITL_YES)

    # This message must describe only what actually happened: a confirmation
    # receipt recorded in the knowledge layer's history (knowledge/store.py's
    # feedback_events table). There is no promotion-tier mechanism in this
    # architecture, so this never claims one, even implicitly.
    if confirmation_saved:
        return {
            "type": "hitl_confirmed",
            "message": f"Got it -- I've recorded that '{audience_label}' was confirmed correct.",
            "detail": "This confirmation is saved in the knowledge layer's history.",
        }
    return {
        "type": "hitl_confirmed",
        "message": "Thanks for confirming -- but I wasn't able to save that confirmation just now.",
        "detail": "The result itself is unaffected; only the record of your confirmation may be missing.",
    }


def _build_review_response(session) -> dict:
    log = session.last_audit_log

    if log is not None and log.sql:
        return {
            "type": "review_sql",
            "sql": log.sql,
            "waterfall": [
                {"step": layer.layer_name, "count": layer.audience_count}
                for layer in log.waterfall
            ],
        }

    return {"type": "error", "message": "No detail is available for this result."}


def _process_correction_sync(
    correction: str,
    log: Optional[QuantAuditLog],
    query: str,
    session=None,
) -> dict:
    """Runs every /correction submission through FeedbackAgent, whether it's a brand-new
    correction or a reply continuing an open clarification conversation. When a
    clarification is already open (session.pending_clarification_question is set),
    `correction` is the person's latest reply -- it gets appended to
    session.clarification_history and the ORIGINAL correction text plus the full history
    are sent through again, so FeedbackAgent re-derives all four confidence gates fresh
    rather than assuming only the just-asked gap is now resolved (see
    agents/feedback_agent.py)."""
    had_pending = bool(session is not None and session.pending_clarification_question)
    original_correction = session.original_correction_text if had_pending else correction
    history = list(session.clarification_history) if (session is not None and had_pending) else []
    if had_pending:
        history.append({"question": session.pending_clarification_question, "answer": correction})
    round_num = session.clarification_round if session is not None else 0

    try:
        from vibe_orchestrator import _run_adhoc_feedback  # noqa: PLC0415
        feedback_output = _run_adhoc_feedback(
            log, query, correction,
            knowledge_ctx=_runtime.knowledge_ctx,
            user_identity=getattr(session, "last_sender", "web-unverified") if session is not None else "web-unverified",
            original_correction=original_correction,
            clarification_history=history,
            clarification_round=round_num,
        )

        if session is not None:
            session.clear_pending_clarification()

        if feedback_output is not None and feedback_output.gave_up:
            return {
                "type": "correction_abandoned",
                "message": feedback_output.interpretation_summary,
            }

        if feedback_output is not None and feedback_output.clarifying_question:
            if session is not None:
                session.original_correction_text = original_correction
                session.clarification_history = history
                session.clarification_round = round_num + 1
                session.pending_clarification_question = feedback_output.clarifying_question
            return {
                "type": "correction_clarifying",
                "question": feedback_output.clarifying_question,
            }

        interpretation = feedback_output.interpretation_summary if feedback_output else ""
        rules = feedback_output.rules_confirmed if feedback_output else []

        # Store as pending -- rules are NOT saved until the user confirms understanding.
        if session is not None:
            session.pending_correction = {
                "text": correction,
                "interpretation": interpretation,
                "rules": rules,
            }

        return {
            "type": "correction_interpreted",
            "interpretation": interpretation,
            "message": "Here is what I understood from your feedback:",
        }
    except Exception as exc:
        _log.warning("Correction sync failed: %s", exc)
        if session is not None:
            session.clear_pending_clarification()
        return {
            "type": "correction_error",
            "message": "I had trouble processing that feedback. Could you rephrase it and try again?",
        }


def _save_confirmed_correction_sync(session, pending: dict, session_id: str) -> dict:
    """Save a confirmed correction as a learned rule, then immediately retry the
    original question with it applied -- capturing feedback is only half of "apply
    it in real time"; the user who just told the tool it was wrong should see the
    corrected answer in this same turn, not have to ask the same question again."""
    with _write_lock:
        try:
            rules = pending.get("rules", [])
            interpretation = pending.get("interpretation", "")
            text = pending.get("text", "")

            # Only a rule the person confirmed should last forever (applies_to_future)
            # ever gets written to the permanent knowledge layer -- a rule that was
            # clearly meant just for the current request/conversation (they said "for
            # this one", "just for now", etc.) is never saved there at all; it only
            # ever applies via session.active_corrections below, for this session only.
            any_permanent = False
            any_session_only = False
            if rules and _runtime.knowledge_ctx is not None:
                for rule in rules:
                    if not rule.applies_to_future:
                        any_session_only = True
                        continue
                    _runtime.knowledge_ctx.add_rule(rule)
                    any_permanent = True
                _runtime.knowledge_ctx.reload_rules()

            record = text + (f" [Understood: {interpretation}]" if interpretation else "")
            session.active_corrections.append(record)
            session.pending_correction = {}

            if any_permanent and any_session_only:
                message = (
                    "Got it -- I'll apply all of this for the rest of this conversation. Part of it "
                    "is also saved as a standing rule for every future conversation; the rest was just "
                    "for this one, so it won't be remembered afterward."
                )
            elif any_session_only:
                message = (
                    "Got it -- I'll apply this for the rest of this conversation, but won't save it as "
                    "a standing rule since you meant it just for this request."
                )
            else:
                message = "Perfect, I've got it! I'll apply this from now on -- for the rest of this session and every future session."

        except Exception as exc:
            _log.warning("Correction save failed: %s", exc)
            return {
                "type": "correction_confirmed",
                "message": "Your feedback has been saved.",
            }

    # Retry the original question now that the correction is in effect -- outside the
    # write lock, since this re-runs the sizing pipeline and shouldn't hold it.
    #
    # When there's a prior successful result to build on, patch the correction directly
    # onto the exact query that already worked (see QuantAgent.apply_confirmed_correction())
    # instead of asking the AI to write the whole thing over from scratch -- that full
    # regeneration is what let a confirmed correction look "ignored" when some unrelated
    # part of the freshly-rewritten query silently changed instead. Fall back to a full
    # rebuild only when there's nothing to patch (no prior sizing result, or no rule
    # description came out of this correction to apply).
    retry_result = None
    correction_descriptions = [
        r.rule_description for r in rules if getattr(r, "rule_description", "")
    ] if rules else []
    if session.last_audit_log is not None and correction_descriptions:
        retry_result = _apply_correction_and_retry_sync(
            session, session.last_audit_log, correction_descriptions, session_id,
        )
    elif session.last_query:
        retry_result = _process_query_sync(session.last_query, session_id)

    return {
        "type": "correction_confirmed",
        "message": message,
        "retry_result": retry_result,
    }


def _apply_correction_and_retry_sync(
    session, previous_log: QuantAuditLog, correction_descriptions: list, session_id: str,
) -> dict:
    """Apply a just-confirmed correction as a deterministic patch on top of the exact query
    that already worked, and update session state the same way a normal retry would."""
    result = _runtime.quant.apply_confirmed_correction(previous_log, correction_descriptions)

    if isinstance(result, NexusErrorPayload):
        _log.warning("Correction patch failed: %s", result.error_summary)
        return {
            "type": "error",
            "message": "I ran into an issue applying that correction to your last result.",
            "detail": result.error_summary[:300],
        }

    session.store_result(query=session.last_query, intent=session.last_intent, log=result)
    session.record_turn(
        session.last_query,
        f"Sized \"{result.request.target_population}\" -> {result.final_count:,} "
        f"({result.request.audience_label or 'ad-hoc request'})",
    )
    return _format_result(
        RequestResult(intent=session.last_intent, log=result),
        _build_processing_notes(session.last_intent),
    )


def _process_clarification_sync(clarification: str, session_id: str, session) -> dict:
    """Handle a clarification submitted from the stuck card.

    1. Run the clarification through the feedback learning pipeline (same as /correction).
    2. Inject it into session.active_corrections so the retry sees it immediately.
    3. Re-run session.last_query with all accumulated context loaded.
    4. Return a full result dict (type: result) or another type: stuck if still blocked.
    """
    original_query = session.last_query
    if not original_query:
        return {"type": "error", "message": "No previous query found to retry."}

    # --- Persist as a learned rule (same pipeline as the correction flow) ---
    try:
        from vibe_orchestrator import _run_adhoc_feedback  # noqa: PLC0415
        feedback_output = _run_adhoc_feedback(
            session.last_audit_log,
            original_query,
            clarification,
            knowledge_ctx=_runtime.knowledge_ctx,
            user_identity=getattr(session, "last_sender", "web-unverified"),
        )
        if feedback_output is not None:
            rules = feedback_output.rules_confirmed or []
            interpretation = feedback_output.interpretation_summary or ""
            with _write_lock:
                if rules and _runtime.knowledge_ctx is not None:
                    # Same applies_to_future gate as _save_confirmed_correction_sync --
                    # a rule meant just for this request is never written permanently.
                    for rule in rules:
                        if rule.applies_to_future:
                            _runtime.knowledge_ctx.add_rule(rule)
                    _runtime.knowledge_ctx.reload_rules()
            record = clarification + (f" [Understood: {interpretation}]" if interpretation else "")
            session.active_corrections.append(record)
        else:
            session.active_corrections.append(clarification)
    except Exception as exc:
        _log.warning("Clarification learning failed (will still retry): %s", exc)
        session.active_corrections.append(clarification)

    # --- Retry the original query with enriched context ---
    return _process_query_sync(original_query, session_id)
