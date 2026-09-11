"""
vibe_orchestrator.py — Vibe Marketing with OCTO
Central decoupled router.

Design principle: this file is the only wiring layer. It knows which agents
exist and which request maps to which agent method — nothing else. Agent cores
are fully independent and test in isolation.

Adding a new agent or capability:
  1. Drop a new agents/<name>.py file defining a BaseAgent subclass with
     WORKER_ID and HANDLED_INTENTS set (see _discover_agents() below) --
     it is auto-discovered and instantiated at startup with zero changes
     needed to this file or to any existing agent.
  2. If the new capability needs a new intent type handled, add one case to
     route_by_intent() below -- that is the only place request dispatch
     happens, and it stays a deliberate, explicit case per intent rather than
     a generic lookup, since agents don't share a uniform call signature (see
     _discover_agents()'s docstring for why) and some intents (sizing_request)
     are a real multi-agent handoff, not a single agent call.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent
_ARTIFACTS_DIR = _ROOT / "knowledge_base" / "artifacts"

# Load the project's .env before any agent code is imported.
load_dotenv(_ROOT / ".env")

# Pin ROOT at sys.path[0] so this project's core/ package always wins over any
# same-named package a dependency might ship.
while str(_ROOT) in sys.path:
    sys.path.remove(str(_ROOT))
sys.path.insert(0, str(_ROOT))

from agents.nexus_agent import NexusAgent  # noqa: E402
from agents.quant_agent import QuantAgent  # noqa: E402
from agents.feedback_agent import FeedbackAgent  # noqa: E402
from pydantic_schemas import FeedbackInput, FeedbackOutput, IntentClassification, QuantAuditLog  # noqa: E402
from core.thought_display import ThoughtDisplay  # noqa: E402
from core.audit_logger import AuditLogger  # noqa: E402


# ---------------------------------------------------------------------------
# Dynamic agent registry — auto-discovered from agents/ at startup.
# Adding a new agent requires only one new file in agents/.
# ---------------------------------------------------------------------------

def _discover_agents(agents_dir: Path) -> tuple[dict[str, type], dict[str, type]]:
    """Scan agents/ for BaseAgent subclasses and build registries.

    Returns:
      agent_registry   — {worker_id: AgentClass} for direct instantiation.
      intent_routing   — {intent_type: AgentClass}, informational only (used for
                         audit/introspection, e.g. listing what handles what) --
                         NOT a dispatch table route_by_intent() below can call
                         generically. Nexus and Quant each expose their own
                         specific methods (classify_intent, build_sizing_request_
                         from_nl, direct_count, answer_general_question) rather
                         than a uniform execute(), and sizing_request is a real
                         two-agent handoff (Nexus builds the request, Quant runs
                         it) that no single {intent: one_agent} entry could
                         express anyway. Making a new agent's intent route
                         automatically would need a uniform agent-execution
                         interface first -- a bigger, separate change, not
                         something this registry can paper over safely.
    """
    import importlib
    import inspect
    from core.base_agent import BaseAgent as _BaseAgent

    agent_registry: dict[str, type] = {}
    intent_routing: dict[str, type] = {}

    if not agents_dir.exists():
        return agent_registry, intent_routing

    for fpath in sorted(agents_dir.glob("*.py")):
        if fpath.name.startswith("_"):
            continue
        module_name = f"agents.{fpath.stem}"
        try:
            mod = importlib.import_module(module_name)
        except Exception as exc:
            logging.warning("_discover_agents: could not import %s: %s", module_name, exc)
            continue
        for _name, obj in inspect.getmembers(mod, inspect.isclass):
            if (
                obj is not _BaseAgent
                and issubclass(obj, _BaseAgent)
                and hasattr(obj, "WORKER_ID")
                and obj.__module__ == module_name
            ):
                worker_id = obj.WORKER_ID
                # Use the worker_id stem (strip version suffix) as registry key
                key = worker_id.split("_")[0]
                agent_registry[key] = obj
                for intent in (obj.HANDLED_INTENTS or frozenset()):
                    # Prefer higher-priority (longer WORKER_ID) if two agents handle the same intent
                    if intent not in intent_routing:
                        intent_routing[intent] = obj

    return agent_registry, intent_routing


_AGENTS_DIR = _ROOT / "agents"
_AGENT_REGISTRY, _ = _discover_agents(_AGENTS_DIR)

# Verify all required agents were discovered; fall back to explicit import if not.
_REQUIRED = {"nexus": NexusAgent, "quant": QuantAgent, "feedback": FeedbackAgent}
for _k, _cls in _REQUIRED.items():
    if _k not in _AGENT_REGISTRY:
        logging.warning("_discover_agents: expected agent '%s' not found — using explicit import", _k)
        _AGENT_REGISTRY[_k] = _cls


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------

def _agent_called_for_intent(intent_type: str) -> str:
    """Map intent type to the agent WORKER_ID that handles it."""
    _map = {
        "sizing_request": "quant_v1",
        "general_question": "nexus_v1",
    }
    return _map.get(intent_type, "nexus_v1")


# ---------------------------------------------------------------------------
# Unified intent-based router
# ---------------------------------------------------------------------------

def route_by_intent(
    nexus: NexusAgent,
    quant: QuantAgent,
    intent: IntentClassification,
    query: str,
) -> tuple[Optional[QuantAuditLog], Optional[str], Optional[str]]:
    """Route a classified request to the agent that can act on it.

    Intent routing:
      sizing_request    -> Nexus translates the request into filters directly
                            from the knowledge base, Quant executes it.
      general_question  -> Nexus answers directly from the knowledge base,
                            no query execution.

    Nexus and Quant already have the knowledge layer bound (see
    build_runtime()'s set_knowledge_context() calls), so nothing needs to be
    passed through here beyond the query itself.

    Returns (log, answer_text, stuck_reason). Exactly one of log/answer_text is
    populated on success; both are None when nothing could be produced, and
    stuck_reason then carries the real failure reason (a technical error from
    Nexus's request-building step or from Quant) so the caller can tell the
    user what actually went wrong instead of falling back to a guess --
    see generate_stuck_explanation().
    """
    it = intent.intent_type

    if it == "sizing_request":
        request, build_error = nexus.build_sizing_request_from_nl(query)
        if request is None:
            return None, None, build_error
        result = quant.direct_count(request)
        if isinstance(result, QuantAuditLog):
            return result, None, None
        ThoughtDisplay.translate_nexus_error(result.error_summary)
        return None, None, result.error_summary

    if it == "general_question":
        answer = nexus.answer_general_question(query)
        return None, (answer or None), None

    ThoughtDisplay.error(f"Unrecognised intent type '{it}'")
    return None, None, f"Unrecognised intent type '{it}'"


# ---------------------------------------------------------------------------
# Ad-hoc feedback helper — FeedbackAgent for a sizing or general-question correction
# ---------------------------------------------------------------------------

def _run_adhoc_feedback(
    log: Optional[QuantAuditLog],
    query: str,
    correction: str,
    knowledge_ctx: Optional["KnowledgeContext"] = None,
    user_identity: str = "unknown",
) -> Optional[FeedbackOutput]:
    """Invoke FeedbackAgent to interpret a correction on a sizing result or a
    general-question answer.

    Builds a minimal FeedbackInput from whatever context is available. Every
    correction is treated as ad-hoc (campaign_code 'AD_HOC' when there is no
    sizing log to draw a campaign code from) since every sizing request is
    already built directly from the knowledge base rather than a
    named-campaign lookup -- see agents/nexus_agent.py's module docstring.
    Returns FeedbackOutput so callers can surface the interpretation summary
    or a clarifying question to the user.

    user_identity is whoever is actually submitting this correction (a real
    Slack user id, or the IAP-authenticated email for the web chat) -- it is
    carried through to the saved BusinessRule.verified_by so a rule that will
    govern every future user's results is never attributed to a placeholder
    string instead of the person who actually approved it.
    """
    try:
        feedback_input = FeedbackInput(
            raw_correction=correction,
            campaign_code=log.request.campaign_code if log is not None else "AD_HOC",
            campaign_name=log.request.campaign_name if log is not None else "Ad-Hoc Query",
            medium=log.request.medium if log is not None else "",
            cadence=log.request.cadence if log is not None else "",
            campaign_purpose="",
            execution_context={
                "query": query,
                "final_audience_count": log.final_count if log is not None else None,
                "waterfall_steps": len(log.waterfall) if log is not None else 0,
            },
            existing_rules=[],
            knowledge_tier="BRONZE",
            raw_input_prompt=query,
            user_identity=user_identity,
        )
        agent = FeedbackAgent()
        if knowledge_ctx is not None:
            agent.set_knowledge_context(knowledge_ctx)
        agent.subscribe(feedback_input)
        return agent.execute()
    except Exception as _exc:
        logging.getLogger(__name__).warning("_run_adhoc_feedback error: %s", _exc)
        return None


# ---------------------------------------------------------------------------
# Shared runtime container — used by the API server
# ---------------------------------------------------------------------------

class VibeRuntime:
    """All shared objects initialized once at startup, shared across all requests."""

    def __init__(
        self,
        nexus: NexusAgent,
        quant: QuantAgent,
        knowledge_ctx: "KnowledgeContext",
        audit_logger: Optional[AuditLogger] = None,
    ) -> None:
        self.nexus = nexus
        self.quant = quant
        # The knowledge layer is the single front door for both reading
        # confirmed knowledge and persisting confirmed HITL feedback -- see
        # knowledge/context.py's KnowledgeContext.
        self.knowledge_ctx = knowledge_ctx
        self.audit_logger = audit_logger


class RequestResult:
    """Pipeline result returned by process_core_request(). No terminal I/O side effects."""

    def __init__(
        self,
        intent: IntentClassification,
        log: Optional[QuantAuditLog],
        answer_text: Optional[str] = None,
        error: Optional[str] = None,
        stuck_reason: Optional[str] = None,
    ) -> None:
        self.intent = intent
        self.log = log
        self.answer_text = answer_text  # populated for general_question
        self.error = error  # set when the pipeline raised an unhandled exception
        # Real reason a sizing_request produced neither log nor answer_text
        # without raising -- e.g. a SQL/schema error from Quant, or a request-
        # build failure from Nexus. None when the request was never attempted
        # (unset only if route_by_intent itself wasn't reached).
        self.stuck_reason = stuck_reason


def build_runtime() -> VibeRuntime:
    """Initialize all shared runtime objects. Called once at API-server startup.

    One KnowledgeContext instance is the single front door for both reading
    the knowledge layer (schema, glossary, business rules, campaigns) and
    persisting confirmed HITL feedback -- bound into Nexus and Quant via
    their existing set_knowledge_context() setters.
    """
    from knowledge.context import KnowledgeContext  # noqa: PLC0415

    nexus: NexusAgent = _AGENT_REGISTRY["nexus"]()
    quant: QuantAgent = _AGENT_REGISTRY["quant"]()

    knowledge_ctx = KnowledgeContext()
    nexus.set_knowledge_context(knowledge_ctx)
    quant.set_knowledge_context(knowledge_ctx)

    audit_logger = AuditLogger(logs_dir=_ROOT / "logs")

    return VibeRuntime(
        nexus=nexus,
        quant=quant,
        knowledge_ctx=knowledge_ctx,
        audit_logger=audit_logger,
    )


def generate_stuck_explanation(
    nexus: NexusAgent,
    query: str,
    intent: Optional[IntentClassification],
    stuck_reason: Optional[str] = None,
) -> str:
    """Call the AI to explain why a request produced no result.

    stuck_reason, when given, is the real failure reason from route_by_intent()
    (a technical error) and is passed straight through -- NexusAgent.explain_stuck_request()
    uses it to tell the user the truth (something broke, try again) instead of
    fabricating a business-clarification question. Only falls back to the
    generic "no matching data sources" message when the caller has no real
    reason to report.
    """
    intent_type = intent.intent_type if intent else "unknown"
    knowledge_sources = list(intent.knowledge_sources_consulted or []) if intent else []
    campaign_identified = intent.campaign_identified if intent else False
    error_details = stuck_reason or (
        "" if knowledge_sources else "No matching data sources were found for this request."
    )

    return nexus.explain_stuck_request(
        original_query=query,
        intent_type=intent_type,
        knowledge_sources=knowledge_sources,
        error_details=error_details,
        campaign_identified=campaign_identified,
    )


def process_core_request(
    query: str,
    rt: VibeRuntime,
    session_id: Optional[str] = None,
    user: str = "unknown",
    session_corrections: Optional[list] = None,
    transcript: Optional[list] = None,
) -> RequestResult:
    """Execute the full pipeline for a single query without any terminal I/O.

    Suitable for API mode: no input() calls, no interactive retry loop --
    a failed sizing request is returned as-is for the caller to surface.

    transcript is this session's prior turns (see api/session_store.py's
    SessionState.transcript) -- what makes a follow-up question ("what about
    Quebec instead?") resolve against the previous turn's criteria rather than
    being classified and sized as a cold, standalone request.
    """
    _log = logging.getLogger(__name__)
    _start = time.perf_counter()
    try:
        # Always set this, even to "" -- Nexus/Quant are shared across every request and
        # keep this per-request value in thread-local storage (see agents/nexus_agent.py),
        # not a per-session object. A worker thread is reused across many different
        # sessions over its lifetime, so skipping this call on a context-less request
        # would leave whatever an EARLIER, unrelated session set on that same thread.
        ctx = ""
        if rt.knowledge_ctx is not None and (session_corrections or transcript):
            ctx = rt.knowledge_ctx.get_dynamic_context(
                query=query, session_corrections=session_corrections, transcript=transcript,
            )
        rt.nexus.set_session_context(ctx)
        rt.quant.set_session_context(ctx)

        intent = rt.nexus.classify_intent(query)
        log, answer_text, stuck_reason = route_by_intent(rt.nexus, rt.quant, intent, query)
        result = RequestResult(intent=intent, log=log, answer_text=answer_text, stuck_reason=stuck_reason)

        if rt.audit_logger is not None:
            try:
                rt.audit_logger.log(
                    session_id=session_id or "api",
                    user=user,
                    intent_type=intent.intent_type,
                    campaign_id=log.request.campaign_code if log else intent.campaign_code,
                    sql=log.sql if log else None,
                    agent_called=_agent_called_for_intent(intent.intent_type),
                    hitl_outcome=None,
                    duration_ms=int((time.perf_counter() - _start) * 1000),
                )
            except Exception:
                pass
        return result
    except Exception as exc:
        _log.exception("process_core_request failed for query %r: %s", query[:80], exc)
        _fallback = IntentClassification(
            intent_type="general_question",
            confidence=0.0,
            campaign_identified=False,
        )
        return RequestResult(intent=_fallback, log=None, error=str(exc))
