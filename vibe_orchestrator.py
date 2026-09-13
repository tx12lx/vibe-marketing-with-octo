"""
vibe_orchestrator.py — Vibe Marketing with OCTO
Central decoupled router.

Design principle: this file is the only wiring layer. It knows which agents
exist -- nothing else. It does NOT know what any specific agent is good for or
which request maps to which agent; that decision belongs to core.router.
IntentRouter, reasoning over each agent's own self-declared CAPABILITIES (see
pydantic_schemas.AgentCapability, core/base_agent.py). Agent cores are fully
independent and test in isolation.

Adding a new agent or capability now requires ZERO changes to this file:
  1. Drop a new agents/<name>.py file defining a BaseAgent subclass with
     WORKER_ID and CAPABILITIES set (see _discover_agents() below) -- it is
     auto-discovered and instantiated at startup.
  2. Implement handle(intent_type, query, context) on it (see BaseAgent.handle()'s
     docstring) -- this is the uniform entrypoint route_by_intent() below calls
     once IntentRouter has decided this agent should handle the request. An
     agent invoked explicitly rather than routed (e.g. FeedbackAgent) can skip
     this and keep CAPABILITIES empty.
That's it -- IntentRouter's classification menu and route_by_intent()'s dispatch
are both built from the registry, not from anything hardcoded per-agent here.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent

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
from core.base_agent import BaseAgent  # noqa: E402
from core.router import IntentRouter  # noqa: E402
from core.thought_display import ThoughtDisplay  # noqa: E402
from core.audit_logger import AuditLogger  # noqa: E402


# ---------------------------------------------------------------------------
# Dynamic agent registry — auto-discovered from agents/ at startup.
# Adding a new agent requires only one new file in agents/.
# ---------------------------------------------------------------------------

def _discover_agents(agents_dir: Path) -> dict[str, type]:
    """Scan agents/ for BaseAgent subclasses and build {worker_id_stem: AgentClass}
    for direct instantiation. Which intent(s) each agent handles is read straight
    off its own CAPABILITIES at dispatch time (see _agent_owning_intent() and
    core/router.py) -- this function only needs to find the agent classes
    themselves, not pre-compute anything about what they're good for."""
    import importlib
    import inspect
    from core.base_agent import BaseAgent as _BaseAgent

    agent_registry: dict[str, type] = {}

    if not agents_dir.exists():
        return agent_registry

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
                # Use the worker_id stem (strip version suffix) as registry key
                agent_registry[obj.WORKER_ID.split("_")[0]] = obj

    return agent_registry


_AGENTS_DIR = _ROOT / "agents"
_AGENT_REGISTRY = _discover_agents(_AGENTS_DIR)

# Verify all required agents were discovered; fall back to explicit import if not.
_REQUIRED = {"nexus": NexusAgent, "quant": QuantAgent, "feedback": FeedbackAgent}
for _k, _cls in _REQUIRED.items():
    if _k not in _AGENT_REGISTRY:
        logging.warning("_discover_agents: expected agent '%s' not found — using explicit import", _k)
        _AGENT_REGISTRY[_k] = _cls


# ---------------------------------------------------------------------------
# Capability lookup — which registered agent owns a given intent type, read
# live off each agent's own CAPABILITIES rather than a hand-maintained map.
# ---------------------------------------------------------------------------

def _agent_owning_intent(rt: "VibeRuntime", intent_type: str) -> Optional[BaseAgent]:
    for agent in rt.agents.values():
        for cap in getattr(agent, "CAPABILITIES", None) or []:
            if cap.intent_type == intent_type:
                return agent
    return None


def _agent_called_for_intent(rt: "VibeRuntime", intent_type: str) -> str:
    """WORKER_ID of whichever agent owns this intent, for audit logging -- a
    router_v1 fallback covers the (rare) case where routing itself produced an
    intent type nothing is actually registered to handle."""
    agent = _agent_owning_intent(rt, intent_type)
    return agent.WORKER_ID if agent is not None else "router_v1"


# ---------------------------------------------------------------------------
# Unified intent-based router
# ---------------------------------------------------------------------------

def route_by_intent(
    rt: "VibeRuntime",
    intent: IntentClassification,
    query: str,
) -> tuple[Optional[QuantAuditLog], Optional[str], Optional[str]]:
    """Route a classified request to whichever registered agent's CAPABILITIES
    claim this intent type, via its uniform handle() entrypoint -- a real table
    lookup, not a hardcoded per-intent branch (see _agent_owning_intent()).

    A "handoff" result (e.g. Nexus translating a sizing request, then handing
    the built AudienceSizingRequest to Quant to actually run) is followed
    exactly once: whichever agent is registered under handoff_to gets called
    next with the handoff payload in its context. This is a genuine, explicit
    two-agent pipeline step, not something a plain {intent: one_agent} lookup
    could express on its own -- see NexusAgent.handle()'s sizing_request case.

    Returns (log, answer_text, stuck_reason). Exactly one of log/answer_text is
    populated on success; both are None when nothing could be produced, and
    stuck_reason then carries the real failure reason so the caller can tell
    the user what actually went wrong instead of falling back to a guess --
    see generate_stuck_explanation().
    """
    it = intent.intent_type
    owner = _agent_owning_intent(rt, it)
    if owner is None:
        ThoughtDisplay.error(f"Unrecognised intent type '{it}'")
        return None, None, f"Unrecognised intent type '{it}'"

    result = owner.handle(it, query)
    if result.kind == "handoff":
        next_agent = rt.agents.get(result.handoff_to) if result.handoff_to else None
        if next_agent is None:
            return None, None, f"No agent registered for handoff target '{result.handoff_to}'."
        result = next_agent.handle(it, query, context={"prebuilt_request": result.handoff_payload})

    if result.kind == "log":
        return result.log, None, None
    if result.kind == "answer":
        return None, (result.answer_text or None), None
    return None, None, result.stuck_reason


# ---------------------------------------------------------------------------
# Ad-hoc feedback helper — FeedbackAgent for a sizing or general-question correction
# ---------------------------------------------------------------------------

def _run_adhoc_feedback(
    log: Optional[QuantAuditLog],
    query: str,
    correction: str,
    knowledge_ctx: Optional["KnowledgeContext"] = None,
    user_identity: str = "unknown",
    original_correction: str = "",
    clarification_history: Optional[list[dict]] = None,
    clarification_round: int = 0,
) -> Optional[FeedbackOutput]:
    """Invoke FeedbackAgent to interpret a correction on a sizing result or a
    general-question answer.

    Builds a minimal FeedbackInput from whatever context is available -- every
    request this tool handles is ad hoc, so there is no named campaign to look
    up. Returns FeedbackOutput so callers can surface the interpretation
    summary or a clarifying question to the user.

    user_identity is whoever is actually submitting this correction (the
    IAP-authenticated email for the web chat) -- it is carried through to the
    saved BusinessRule.verified_by so a rule that will govern every future
    user's results is never attributed to a placeholder string instead of the
    person who actually approved it.
    """
    try:
        feedback_input = FeedbackInput(
            raw_correction=correction,
            audience_label=log.request.audience_label if log is not None else None,
            medium=log.request.medium if log is not None else None,
            cadence=log.request.cadence if log is not None else None,
            has_prior_result=log is not None,
            execution_context={
                "query": query,
                "final_audience_count": log.final_count if log is not None else None,
                "waterfall_steps": len(log.waterfall) if log is not None else 0,
            },
            existing_rules=[],
            raw_input_prompt=query,
            user_identity=user_identity,
            original_correction=original_correction,
            clarification_history=clarification_history or [],
            clarification_round=clarification_round,
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
    """All shared objects initialized once at startup, shared across all requests.

    Agents are held generically in `agents` (registry-key -> instance), so a
    brand-new agent type is fully wired up here with zero code changes to this
    class or to build_runtime() below -- it just needs to exist in
    _AGENT_REGISTRY (see _discover_agents()). `nexus`/`quant` stay available as
    convenience properties on top of that same dict, since every existing call
    site (api/web_app.py, this file's own route_by_intent()) already refers to
    them by name -- this is purely additive, not a breaking rename.
    """

    def __init__(
        self,
        agents: dict[str, BaseAgent],
        knowledge_ctx: "KnowledgeContext",
        audit_logger: Optional[AuditLogger] = None,
    ) -> None:
        self.agents = agents
        # The knowledge layer is the single front door for both reading
        # confirmed knowledge and persisting confirmed HITL feedback -- see
        # knowledge/context.py's KnowledgeContext.
        self.knowledge_ctx = knowledge_ctx
        self.audit_logger = audit_logger
        # Stateless (just holds a reference to `agents`) -- built once here rather
        # than per-request. Reasons over whichever agents are actually registered,
        # so a newly-added agent becomes routable with no change here.
        self.router = IntentRouter(agents)

    @property
    def nexus(self) -> NexusAgent:
        return self.agents["nexus"]

    @property
    def quant(self) -> QuantAgent:
        return self.agents["quant"]


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
    the knowledge layer (schema, glossary, business rules) and persisting
    confirmed HITL feedback -- bound into every agent that exposes
    set_knowledge_context() (all three do today). Every class in
    _AGENT_REGISTRY is instantiated here, generically -- a new agent type
    needs nothing added to this function to be wired up at startup.
    """
    from knowledge.context import KnowledgeContext  # noqa: PLC0415

    knowledge_ctx = KnowledgeContext()
    agents: dict[str, BaseAgent] = {}
    for key, cls in _AGENT_REGISTRY.items():
        instance = cls()
        set_ctx = getattr(instance, "set_knowledge_context", None)
        if set_ctx is not None:
            set_ctx(knowledge_ctx)
        agents[key] = instance

    audit_logger = AuditLogger(logs_dir=_ROOT / "logs")

    return VibeRuntime(agents=agents, knowledge_ctx=knowledge_ctx, audit_logger=audit_logger)


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
    error_details = stuck_reason or (
        "" if knowledge_sources else "No matching data sources were found for this request."
    )

    return nexus.explain_stuck_request(
        original_query=query,
        intent_type=intent_type,
        knowledge_sources=knowledge_sources,
        error_details=error_details,
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
        for agent in rt.agents.values():
            agent.set_session_context(ctx)

        intent = rt.router.classify(query)
        if intent.needs_clarification and intent.clarifying_question:
            # The router itself isn't confident which specialist fits -- ask, don't
            # guess. Forced to "general_question" shape and surfaced as a plain
            # answer (not routed anywhere) so the existing rendering path shows it
            # directly, rather than falling through to the (separate, AI-driven)
            # stuck-explanation path and generating a second, redundant question.
            intent = intent.model_copy(update={"intent_type": "general_question"})
            log, answer_text, stuck_reason = None, intent.clarifying_question, None
        else:
            log, answer_text, stuck_reason = route_by_intent(rt, intent, query)
        result = RequestResult(intent=intent, log=log, answer_text=answer_text, stuck_reason=stuck_reason)

        if rt.audit_logger is not None:
            try:
                rt.audit_logger.log(
                    session_id=session_id or "api",
                    user=user,
                    intent_type=intent.intent_type,
                    sql=log.sql if log else None,
                    agent_called=_agent_called_for_intent(rt, intent.intent_type),
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
        )
        return RequestResult(intent=_fallback, log=None, error=str(exc))
