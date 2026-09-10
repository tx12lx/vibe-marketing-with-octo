"""
api/web_app.py -- Vibe OCTO browser chat interface.

Serves a professional HTML chat page and handles campaign queries,
HITL button clicks, and correction submissions over HTTP.

Multiple concurrent users are supported via per-session state isolation.
File writes to shared learning artifacts (glossary.json, business_rules.json,
verified_app_registry.json) are protected by a threading.Lock so simultaneous
corrections from two users cannot corrupt those files.

How to run:
    uvicorn api.web_app:app --host 0.0.0.0 --port 3000

Then open http://localhost:3000 in any browser on the same network.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs

_API_DIR = Path(__file__).resolve().parent
_ROOT = _API_DIR.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env")

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response  # noqa: E402

from vibe_orchestrator import (  # noqa: E402
    RequestResult,
    VibeRuntime,
    _ARTIFACTS_DIR,
    build_runtime,
    generate_stuck_explanation,
    process_core_request,
)
from core.audit_logger import HITL_YES  # noqa: E402
from core.resilience import run_startup_health_check  # noqa: E402
from pydantic_schemas import BriefingOutput, QuantAuditLog, UniversalJSONSpec  # noqa: E402
from api.session_store import SessionStore  # noqa: E402

_log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

_runtime: Optional[VibeRuntime] = None
_session_store = SessionStore()

# Intents whose result actually drives real customer targeting -- for these,
# looking at the evidence (SQL / sources) is required before a "yes" is
# accepted, not just offered as an optional button. See hitl_endpoint().
_HIGH_IMPACT_INTENTS = frozenset({"sizing_request", "campaign_execution"})
_executor = ThreadPoolExecutor(max_workers=4)
_write_lock = threading.Lock()

_TEMPLATES_DIR = _API_DIR / "templates"

app = FastAPI(title="Vibe Marketing with OCTO — Web Interface", version="4.0.0")


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
        try:
            from knowledge.sync_schema import main as sync_schema_main  # noqa: PLC0415

            _log.info("Syncing the knowledge layer's schema in the background...")
            sync_schema_main()
            _log.info("Knowledge layer schema sync complete.")
        except Exception:
            _log.exception("Schema sync failed (the app keeps serving with whatever schema was already synced).")

    threading.Thread(target=_run, daemon=True).start()


def _start_slack_bot_thread(runtime: VibeRuntime) -> None:
    """Start the Slack bot in a background thread, sharing this process's
    already-built runtime (and therefore its one KnowledgeContext) instead of
    letting the Slack side build its own -- that's the whole point of running
    both front ends in one process: exactly one knowledge database, not two
    that can drift apart. Only runs if Slack credentials are configured; the
    import is deferred so a slow or failing Slack import can't block the web
    app's own startup or health checks (same reasoning as slack_cloud_run.py's
    existing background-thread pattern this reuses)."""
    if not (os.getenv("SLACK_BOT_TOKEN") and os.getenv("SLACK_APP_TOKEN")):
        _log.info("SLACK_BOT_TOKEN/SLACK_APP_TOKEN not set -- Slack front end disabled, serving web chat only.")
        return

    def _run() -> None:
        try:
            from api.slack_app import main as start_slack_bot  # noqa: PLC0415

            start_slack_bot(runtime=runtime)
        except Exception:
            # Previously this could fail silently in the background with nothing
            # but a buried stack trace -- log it loudly instead so a broken Slack
            # connection is never mistaken for a working one.
            _log.exception("Slack bot failed to start -- web chat is unaffected, but Slack is not connected.")

    threading.Thread(target=_run, daemon=True).start()


@app.on_event("startup")
async def _startup() -> None:
    global _runtime
    _log.info("Vibe OCTO web server starting — initializing runtime...")
    _sync_schema_thread()
    loop = asyncio.get_event_loop()
    _runtime = await loop.run_in_executor(_executor, build_runtime)
    _start_slack_bot_thread(_runtime)

    fuelix_key = os.getenv("FUELIX_API_KEY", "")
    bq_project = os.getenv("BQ_PROJECT_ID", "bi-srv-hsmdet-pr-7b9def")
    run_startup_health_check(
        api_key=fuelix_key,
        bq_project=bq_project,
        artifacts_dir=_ARTIFACTS_DIR,
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
    campaign_count = 0
    if _runtime is not None:
        try:
            campaign_count = _runtime.knowledge_ctx.campaign_count
        except Exception:
            pass
    return {
        "status": "ok",
        "runtime_ready": _runtime is not None,
        "campaign_count": campaign_count,
        "active_sessions": len(_session_store._sessions),
    }


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    campaign_count = 0
    if _runtime is not None:
        try:
            campaign_count = _runtime.knowledge_ctx.campaign_count
        except Exception:
            pass
    html = (_TEMPLATES_DIR / "chat.html").read_text(encoding="utf-8")
    html = html.replace("{{CAMPAIGN_COUNT}}", str(campaign_count))
    return HTMLResponse(content=html)


@app.post("/query")
async def query_endpoint(request: Request) -> JSONResponse:
    body = await request.json()
    session_id: str = body.get("session_id", "")
    text: str = body.get("text", "").strip()

    if not session_id or not text:
        raise HTTPException(status_code=400, detail="session_id and text are required")
    if _runtime is None:
        return JSONResponse({"type": "error", "message": "The tool is still starting up. Please try again in a moment."})

    _session_store.get(session_id).last_sender = _caller_identity(request)

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, _process_query_sync, text, session_id)
    return JSONResponse(result)


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
            # from confirming a sizing/campaign-execution result sight-unseen.
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
        session.reviewed_this_result = True
        return JSONResponse(_build_review_response(session))

    # action == "no"
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
        session.last_spec,
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
# Rule review -- maker-checker gate for 'pattern'/'universal' business rules.
#
# A rule scoped to one campaign is contained to the blast radius the submitter
# is already working in, so it goes live immediately. A rule scoped 'pattern'
# or 'universal' governs every future user's results, so knowledge_ctx.add_rule()
# stages it as pending_review instead -- these three endpoints are how a second
# person sees it and approves or rejects it. Reachable only behind whatever
# already gates this whole app (IAP in production; the shared-password gate
# otherwise) -- no separate auth layer of its own.
# ---------------------------------------------------------------------------

@app.get("/admin/pending-rules")
async def list_pending_rules() -> JSONResponse:
    if _runtime is None or _runtime.knowledge_ctx is None:
        return JSONResponse({"rules": []})
    try:
        rules = _runtime.knowledge_ctx.get_pending_rules()
    except Exception as exc:
        _log.warning("Listing pending rules failed: %s", exc)
        return JSONResponse({"rules": [], "error": str(exc)})
    return JSONResponse({"rules": rules})


@app.post("/admin/pending-rules/approve")
async def approve_pending_rule(request: Request) -> JSONResponse:
    body = await request.json()
    rule_id = body.get("rule_id")
    if rule_id is None:
        raise HTTPException(status_code=400, detail="rule_id is required")
    if _runtime is None or _runtime.knowledge_ctx is None:
        return JSONResponse({"status": "error", "message": "Runtime not available."}, status_code=503)

    approver = _caller_identity(request)
    with _write_lock:
        try:
            _runtime.knowledge_ctx.approve_rule(int(rule_id), approver_identity=approver)
        except Exception as exc:
            # Includes knowledge.store.MakerCheckerViolation when the approver is the
            # same person who submitted the rule -- surfaced plainly, not swallowed.
            return JSONResponse({"status": "error", "message": str(exc)}, status_code=409)
    return JSONResponse({"status": "approved", "approved_by": approver})


@app.post("/admin/pending-rules/reject")
async def reject_pending_rule(request: Request) -> JSONResponse:
    body = await request.json()
    rule_id = body.get("rule_id")
    note = body.get("note", "")
    if rule_id is None:
        raise HTTPException(status_code=400, detail="rule_id is required")
    if _runtime is None or _runtime.knowledge_ctx is None:
        return JSONResponse({"status": "error", "message": "Runtime not available."}, status_code=503)

    approver = _caller_identity(request)
    with _write_lock:
        try:
            _runtime.knowledge_ctx.reject_rule(int(rule_id), approver_identity=approver, note=note)
        except Exception as exc:
            return JSONResponse({"status": "error", "message": str(exc)}, status_code=409)
    return JSONResponse({"status": "rejected", "rejected_by": approver})


# ---------------------------------------------------------------------------
# Sync pipeline functions (all run in thread pool executor)
# ---------------------------------------------------------------------------

def _process_query_sync(text: str, session_id: str) -> dict:
    session = _session_store.get(session_id)
    result: RequestResult = process_core_request(
        text, _runtime,
        session_id=session_id,
        session_corrections=session.active_corrections if session.active_corrections else None,
    )

    if result.error:
        return {
            "type": "error",
            "message": "I ran into an issue and couldn't complete your request.",
            "detail": result.error[:300],
        }

    if result.log is not None or result.brief_output is not None:
        session.store_result(
            query=text,
            intent=result.intent,
            spec=result.spec,
            log=result.log,
            brief=result.brief_output,
        )

    processing_notes = _build_processing_notes(result.intent, result.spec)
    intent_type = result.intent.intent_type if result.intent else "general_question"

    if intent_type == "general_question" and result.log is None and result.brief_output is None:
        return {
            "type": "general_answer",
            "processing_notes": processing_notes,
            "message": "I've answered from the knowledge base. Is there anything else I can help with?",
        }

    # If a non-general-question intent produced nothing useful, use the AI to
    # generate a warm explanation and ask the user for a clarifying detail.
    if result.log is None and result.brief_output is None:
        session.last_query = text  # store for retry without user retyping
        explanation = generate_stuck_explanation(
            _runtime.nexus, text, result.intent, result.spec, result.brief_output
        )
        return {
            "type": "stuck",
            "explanation": explanation,
            "processing_notes": processing_notes,
            "intent_type": intent_type,
        }

    return _format_result(result, processing_notes)


def _format_result(result: RequestResult, processing_notes: list) -> dict:
    out: dict = {
        "type": "result",
        "processing_notes": processing_notes,
        "intent_type": result.intent.intent_type if result.intent else "",
        "error": None,
        # Defaults so the frontend never KeyErrors
        "campaign_name": "",
        "campaign_code": "",
        "campaign_sub_code": "",
        "medium": "",
        "cadence": "",
        "tier": "",
        "discrepancy_flags": [],
        "audience_count": None,
        "optimization_note": None,
        "confidence": None,
        "brief_universe": "",
        "brief_exclusions": [],
        "brief_segments": [],
        "brief_executive_summary": "",
        "knowledge_sources": [],
    }

    if result.spec:
        out["campaign_name"] = result.spec.campaign_name
        out["campaign_code"] = result.spec.campaign_code
        out["campaign_sub_code"] = result.spec.campaign_sub_code
        out["medium"] = result.spec.medium
        out["cadence"] = result.spec.cadence
        out["tier"] = result.spec.campaign_tier
        out["discrepancy_flags"] = result.spec.discrepancy_flags or []

    if result.log:
        out["audience_count"] = result.log.final_count
        out["optimization_note"] = result.log.optimization_note

    if result.brief_output:
        out["confidence"] = round(result.brief_output.confidence_score * 100)
        out["brief_universe"] = result.brief_output.structured_universe or ""
        out["brief_exclusions"] = result.brief_output.structured_exclusions or []
        out["brief_segments"] = [
            {"name": s.name, "description": s.description}
            for s in (result.brief_output.structured_segments or [])
        ]
        out["brief_executive_summary"] = result.brief_output.executive_summary or ""
        out["knowledge_sources"] = result.brief_output.knowledge_sources_used or []
    elif result.intent:
        out["confidence"] = round(result.intent.confidence * 100)
        out["knowledge_sources"] = result.intent.knowledge_sources_consulted or []

    return out


def _build_processing_notes(intent, spec) -> list:
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

    if spec and spec.discrepancy_flags:
        for flag in spec.discrepancy_flags[:2]:
            notes.append(f"Advisory: {flag}")

    return notes


def _handle_hitl_yes_sync(session, session_id: str) -> dict:
    spec = session.last_spec
    log = session.last_audit_log
    brief = session.last_brief

    if spec is None or log is None:
        return {
            "type": "hitl_confirmed",
            "message": "Result confirmed. Thanks!",
            "detail": "",
        }

    confirmation_saved = False
    user_identity = getattr(session, "last_sender", "web-unverified")

    with _write_lock:
        if _runtime.knowledge_ctx is not None:
            try:
                _runtime.knowledge_ctx.record_confirmation(
                    campaign_code=spec.campaign_code,
                    user_identity=user_identity,
                )
                confirmation_saved = True
            except Exception as exc:
                _log.warning("Knowledge-layer confirmation write failed: %s", exc)

        if _runtime.audit_logger is not None:
            try:
                _runtime.audit_logger.log_hitl_resolution(
                    session_id=session_id,
                    user=user_identity,
                    campaign_id=spec.campaign_code,
                    hitl_outcome=HITL_YES,
                )
            except Exception:
                pass

    # This message must describe only what actually happened: a confirmation
    # receipt recorded in the knowledge layer's history (knowledge/store.py's
    # feedback_events table). There is no GOLD-tier promotion mechanism in the
    # current architecture -- the old one was removed in the knowledge-layer
    # rebuild -- so this no longer claims one, even implicitly.
    if confirmation_saved:
        return {
            "type": "hitl_confirmed",
            "message": f"Got it -- I've recorded that '{spec.campaign_name}' was confirmed correct.",
            "detail": "This confirmation is saved in the knowledge layer's history.",
        }
    return {
        "type": "hitl_confirmed",
        "message": "Thanks for confirming -- but I wasn't able to save that confirmation just now.",
        "detail": "The result itself is unaffected; only the record of your confirmation may be missing.",
    }


def _build_review_response(session) -> dict:
    log = session.last_audit_log
    brief = session.last_brief
    intent_type = session.last_intent.intent_type if session.last_intent else ""

    if intent_type in ("brief_generation", "brief_qa") and brief is not None:
        return {
            "type": "review_sources",
            "sources": brief.data_sources_cited or [],
            "knowledge_sources": brief.knowledge_sources_used or [],
        }

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
    spec: Optional[UniversalJSONSpec],
    log: Optional[QuantAuditLog],
    query: str,
    session=None,
) -> dict:
    try:
        from vibe_orchestrator import _run_adhoc_feedback  # noqa: PLC0415
        feedback_output = _run_adhoc_feedback(
            spec, log, query, correction,
            knowledge_ctx=_runtime.knowledge_ctx,
            non_interactive=True,
            user_identity=getattr(session, "last_sender", "web-unverified") if session is not None else "web-unverified",
        )

        if feedback_output is not None and feedback_output.clarifying_question:
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
        return {
            "type": "correction_error",
            "message": "I had trouble processing that feedback. Could you rephrase it and try again?",
        }


def _save_confirmed_correction_sync(session, pending: dict) -> dict:
    with _write_lock:
        try:
            rules = pending.get("rules", [])
            interpretation = pending.get("interpretation", "")
            text = pending.get("text", "")

            # add_rule() stages 'pattern'/'universal' rules as pending_review rather
            # than applying them immediately -- they'd otherwise govern every future
            # user's results on this submitter's word alone. A 'campaign'-scoped rule
            # (contained to the one campaign already being worked on) stays immediate.
            any_active = False
            any_pending = False
            if rules and _runtime.rules_registry is not None:
                for rule in rules:
                    outcome = _runtime.rules_registry.add_rule(rule)
                    if outcome.get("status") == "pending_review":
                        any_pending = True
                    else:
                        any_active = True

            if _runtime.rules_registry is not None:
                _runtime.rules_registry._load()
            if _runtime.knowledge_ctx is not None:
                _runtime.knowledge_ctx.reload_rules()

            record = text + (f" [Understood: {interpretation}]" if interpretation else "")
            session.active_corrections.append(record)
            session.pending_correction = {}

            if any_pending and any_active:
                message = (
                    "Got it -- I'll apply this for the rest of this session. Part of it only affects "
                    "this campaign, so that part is already saved for future sessions too; the part "
                    "that would apply to every future request still needs a second reviewer's approval "
                    "before it goes live for everyone."
                )
            elif any_pending:
                message = (
                    "Got it -- I'll apply this for the rest of this session. Since this would change "
                    "behavior for every future request across the whole team, it's saved as pending "
                    "review and needs a second person to approve it before it applies more broadly."
                )
            else:
                message = "Perfect, I've got it! I'll apply this from now on -- for the rest of this session and every future session."

            return {
                "type": "correction_confirmed",
                "message": message,
            }
        except Exception as exc:
            _log.warning("Correction save failed: %s", exc)
            return {
                "type": "correction_confirmed",
                "message": "Your feedback has been saved.",
            }


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
            session.last_spec,
            session.last_audit_log,
            original_query,
            clarification,
            knowledge_ctx=_runtime.knowledge_ctx,
            non_interactive=True,
            user_identity=getattr(session, "last_sender", "web-unverified"),
        )
        if feedback_output is not None:
            rules = feedback_output.rules_confirmed or []
            interpretation = feedback_output.interpretation_summary or ""
            with _write_lock:
                if rules and _runtime.rules_registry is not None:
                    for rule in rules:
                        _runtime.rules_registry.add_rule(rule)
                if _runtime.rules_registry is not None:
                    _runtime.rules_registry._load()
                if _runtime.knowledge_ctx is not None:
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
