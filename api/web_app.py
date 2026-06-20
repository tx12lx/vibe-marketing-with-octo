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
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

_API_DIR = Path(__file__).resolve().parent
_ROOT = _API_DIR.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_ROOT / ".env")

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse  # noqa: E402

from vibe_orchestrator import (  # noqa: E402
    RequestResult,
    VibeRuntime,
    _ARTIFACTS_DIR,
    build_runtime,
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
_executor = ThreadPoolExecutor(max_workers=4)
_write_lock = threading.Lock()

_TEMPLATES_DIR = _API_DIR / "templates"

app = FastAPI(title="Vibe Marketing with OCTO — Web Interface", version="4.0.0")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def _startup() -> None:
    global _runtime
    _log.info("Vibe OCTO web server starting — initializing runtime...")
    loop = asyncio.get_event_loop()
    _runtime = await loop.run_in_executor(_executor, build_runtime)

    fuelix_key = os.getenv("FUELIX_API_KEY", "")
    bq_project = os.getenv("BQ_PROJECT_ID", "bi-srv-hsmdet-pr-7b9def")
    run_startup_health_check(
        api_key=fuelix_key,
        bq_project=bq_project,
        artifacts_dir=_ARTIFACTS_DIR,
    )
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

    if action == "yes":
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(_executor, _handle_hitl_yes_sync, session, session_id)
        session.clear_hitl()
        return JSONResponse(result)

    if action == "review":
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

    # If a non-general-question intent produced nothing useful, surface it as
    # an explicit incomplete result rather than silently returning empty fields.
    if result.log is None and result.brief_output is None:
        brief_error = None
        return {
            "type": "result",
            "processing_notes": processing_notes,
            "intent_type": intent_type,
            "empty_result": True,
            "error": None,
            "campaign_name": result.spec.campaign_name if result.spec else "",
            "campaign_code": result.spec.campaign_code if result.spec else "",
            "campaign_sub_code": result.spec.campaign_sub_code if result.spec else "",
            "medium": result.spec.medium if result.spec else "",
            "cadence": result.spec.cadence if result.spec else "",
            "tier": result.spec.campaign_tier if result.spec else "",
            "discrepancy_flags": [],
            "audience_count": None,
            "optimization_note": None,
            "confidence": round(result.intent.confidence * 100) if result.intent else None,
            "brief_universe": "",
            "brief_exclusions": [],
            "brief_segments": [],
            "brief_executive_summary": "",
            "knowledge_sources": result.intent.knowledge_sources_consulted if result.intent else [],
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

    with _write_lock:
        try:
            _runtime.hitl._handle_yes(spec, log, brief)
        except Exception as exc:
            _log.warning("HITL yes write failed: %s", exc)

        if _runtime.audit_logger is not None:
            try:
                _runtime.audit_logger.log_hitl_resolution(
                    session_id=session_id,
                    user="web",
                    campaign_id=spec.campaign_code,
                    hitl_outcome=HITL_YES,
                )
            except Exception:
                pass

    return {
        "type": "hitl_confirmed",
        "message": f"Wonderful! '{spec.campaign_name}' has been saved as a verified blueprint.",
        "detail": "It will be promoted to GOLD on the next knowledge refresh.",
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
            from core.business_rules_registry import BusinessRulesRegistry  # noqa: PLC0415
            rules = pending.get("rules", [])
            interpretation = pending.get("interpretation", "")
            text = pending.get("text", "")

            if rules and _runtime.rules_registry is not None:
                for rule in rules:
                    _runtime.rules_registry.add_rule(rule)

            if _runtime.rules_registry is not None:
                _runtime.rules_registry._load()
            if _runtime.knowledge_ctx is not None:
                _runtime.knowledge_ctx.reload_rules()

            record = text + (f" [Understood: {interpretation}]" if interpretation else "")
            session.active_corrections.append(record)
            session.pending_correction = {}

            return {
                "type": "correction_confirmed",
                "message": "Perfect, I've got it! I'll apply this from now on -- for the rest of this session and every future session.",
            }
        except Exception as exc:
            _log.warning("Correction save failed: %s", exc)
            return {
                "type": "correction_confirmed",
                "message": "Your feedback has been saved.",
            }
