"""
Vibe OCTO Nexus — Sizing Request Specialist

Nexus is one specialist among several this tool's own router (core/router.py)
can send a request to -- see NexusAgent.CAPABILITIES below for what it
declares itself good for, and BaseAgent.handle() for the uniform way the
router invokes any registered agent. Deciding WHICH agent handles a request
is no longer Nexus's job (it used to be, via a since-removed classify_intent()
method) -- that decision is reasoned over centrally, across every registered
agent's own self-description, not from inside any one specialist.

Nexus's actual job, grounded in the knowledge layer (real column schema,
glossary, and confirmed business rules for the synced table) rather than any
hardcoded taxonomy: build_sizing_request_from_nl() translates a natural-
language audience description directly into a validated AudienceSizingRequest
for Quant, using the schema/glossary/business-rules context as the
authoritative reference for what any term means. It also answers
general-knowledge questions directly (answer_general_question()) when that's
the specialist the router picked.

This tool only ever handles ad hoc requests -- a request may or may not
mention a named campaign, but nothing here tracks campaigns as first-class
objects. An earlier version of this agent looked up named campaigns'
historical briefs from BigQuery and compared them against a curated "gold"
reference before sizing; that mechanism, and the campaign-matching/tiering
scaffolding built around it, were removed during the knowledge-layer cleanup.
Every sizing request now goes through the one grounded, working path:
build_sizing_request_from_nl().
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Optional

from pydantic import ValidationError
from core.ai_client import ask_ai
from core.json_extract import extract_json

_AGENTS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _AGENTS_DIR.parent

for _p in [str(_ROOT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pydantic_schemas import (
    AgentCapability,
    AgentResult,
    AudienceSizingRequest,
    IntentClassification,
)
from core.base_agent import BaseAgent  # noqa: E402
from core.thought_display import ThoughtDisplay  # noqa: E402

_MAX_TOKENS_BUILD = 4096
_MAX_TOKENS_QUERY = 8192

# Same env vars QuantAgent reads (agents/quant_agent.py) -- kept in sync so a request
# built here and executed there always target the same table, without either file
# hardcoding a value the other doesn't know about.
_DEFAULT_BQ_PROJECT = os.getenv("BQ_PROJECT_ID", "bi-srv-hsmdet-pr-7b9def")
_DEFAULT_BQ_DATASET = os.getenv("BQ_DATASET", "campaign_data")

# Split into an identity block (shared by every call) and a JSON mandate (only for calls
# that actually want structured output) -- _call_with_knowledge() is shared by both
# JSON-producing callers (build_sizing_request_from_nl) and prose-producing callers
# (answer_general_question, explain_stuck_request), and a system prompt unconditionally
# demanding "Return ONLY valid JSON" directly contradicts a user prompt asking for a
# warm, 2-4 sentence plain-English answer. A more instruction-literal model (confirmed
# live switching to Gemini 2.5 Pro) follows the system mandate over the conflicting user
# prompt and wraps its prose answer in a raw JSON blob -- a less literal model may have
# silently ignored the system prompt instead, which is what made this go unnoticed.
_NEXUS_IDENTITY = (
    "You are Vibe OCTO Nexus, a senior management consulting AI embedded in a "
    "Canadian telecom marketing team (TELUS / Koodo). "
    "All table names, column meanings, and business rules are provided separately, from "
    "the knowledge layer -- not hardcoded here. Treat anything marked '(confirmed)' as "
    "ground truth. Treat anything marked '(unconfirmed guess)' as tentative -- you may "
    "still use it, but say so plainly if your classification depends on an unconfirmed "
    "guess, so a human can verify it."
)

_NEXUS_JSON_MANDATE = (
    "You classify what a user is asking for, extract structured targeting parameters "
    "from natural language, and emit validated JSON payloads for downstream audience "
    "sizing. "
    "Return ONLY valid JSON — no explanation, no markdown, no trailing text."
)

_NEXUS_PROSE_MANDATE = (
    "For this specific request, answer directly in plain English prose -- no JSON, no "
    "markdown code fences, no structured format of any kind. Just the answer itself."
)

_NEXUS_SYSTEM = _NEXUS_IDENTITY + " " + _NEXUS_JSON_MANDATE

_NL_PARSE_PROMPT = """A consultant has submitted an ad-hoc audience sizing request:
  "{query}"

You have the table's full confirmed knowledge above: the real column schema, a glossary of
business terms, and a set of confirmed business rules. That knowledge is your authoritative
reference for translating this request into filters — not just the consultant's literal words.

Instructions:
1. Translate the consultant's stated criteria into BQ-interpretable filter strings using the
   real column names and values from the schema above. When the consultant uses a term (a
   product name, an acronym, a customer type, a province, etc.) that already has a confirmed
   definition in the glossary or in a column's confirmed value notes, use that definition
   directly and with full confidence -- do not ask the consultant to define a term the
   knowledge base already defines for you. Only treat a term as genuinely ambiguous, and
   worth flagging, when it has no confirmed definition anywhere in the schema, glossary, or
   business rules.
2. Apply every rule under CONFIRMED BUSINESS RULES above by default, exactly as written,
   unless the consultant's request explicitly says otherwise for that specific rule.
3. Never invent a filter, column, or value that isn't grounded in the schema, the glossary,
   the business rules, or the consultant's own words.
4. If cadence or medium are not stated by the consultant, set them to null. Never invent a
   placeholder value like "ad-hoc" or "unspecified" -- those are not real information and
   must not be shown to the consultant as if they were.
5. Leave exclusion_layers as an empty list unless a confirmed business rule or the
   consultant's own request calls for one.
6. Only set optimization_context when there is a genuine, request-specific caveat worth
   surfacing -- for example, criteria that are unusually broad or narrow, or an ambiguity
   you resolved by stating an assumption. If there is nothing notable about this specific
   request, use an empty string. Never write generic boilerplate about cadence, channel
   suitability, or send-timing -- most ad-hoc requests are not a campaign send at all, and
   inventing that framing when it wasn't asked for is exactly the mistake this guards against.

Return exactly this JSON (no markdown):
{{
  "audience_label": "<short descriptive label for this audience segment, for display only>",
  "cadence": "<cadence if explicitly stated by consultant, else null>",
  "medium": "<medium if explicitly stated by consultant, else null>",
  "target_population": "<precise plain-English restatement of who qualifies>",
  "filters": ["<filter grounded in the schema/glossary/business rules or the consultant's stated criteria>"],
  "exclusion_layers": [],
  "optimization_context": "<only if there is a genuine caveat worth flagging, else empty string>",
  "bq_project": "{bq_project}",
  "bq_dataset": "{bq_dataset}"
}}"""

class NexusAgent(BaseAgent):
    WORKER_ID = "nexus_v1"
    CAPABILITIES = [
        AgentCapability(
            intent_type="general_question",
            description=(
                "Answers a question about data, definitions, business rules, or strategy "
                "directly from what's already confirmed in the knowledge base -- no new "
                "data query is run."
            ),
            examples=[
                "What does postpaid mean in this schema?",
                "What's our DNC policy for SMS?",
            ],
        ),
        AgentCapability(
            intent_type="sizing_request",
            description=(
                "Translates a natural-language audience description into a precise, "
                "structured targeting request grounded in the real schema/glossary/business "
                "rules -- the first of two steps for any 'how many X are there' style "
                "question; QuantAgent runs the actual count once this step hands it off."
            ),
            examples=[
                "How many postpaid customers in BC are eligible for upgrade?",
                "Count of FFH customers not on stop-sell, excluding DNC",
            ],
        ),
    ]
    INPUT_SCHEMA = AudienceSizingRequest
    OUTPUT_SCHEMA = IntentClassification

    def subscribe(self, spec) -> None:
        """Not used — NexusAgent is invoked via handle(), not subscribe/execute."""

    def execute(self) -> IntentClassification:
        """Not used — NexusAgent is invoked via handle(), not subscribe/execute."""
        raise NotImplementedError(
            "NexusAgent does not use the subscribe/execute interface. Call handle() directly."
        )

    def handle(self, intent_type: str, query: str, context: Optional[dict] = None) -> AgentResult:
        """Uniform entrypoint the router (core/router.py) calls once it's decided this
        is the right specialist -- dispatches to Nexus's own real methods below rather
        than forcing them to share one signature."""
        if intent_type == "general_question":
            answer = self.answer_general_question(query)
            return AgentResult(kind="answer", answer_text=answer or None)
        if intent_type == "sizing_request":
            request, build_error = self.build_sizing_request_from_nl(query)
            if request is None:
                return AgentResult(kind="stuck", stuck_reason=build_error)
            # Sizing is a genuine two-agent handoff: Nexus works out exactly who
            # qualifies, Quant runs the actual count -- see vibe_orchestrator.route_by_intent().
            return AgentResult(kind="handoff", handoff_to="quant", handoff_payload=request)
        return AgentResult(
            kind="stuck",
            stuck_reason=f"NexusAgent does not handle intent type '{intent_type}'.",
        )

    def __init__(self) -> None:
        # One NexusAgent instance is shared by every concurrent request (built once in
        # vibe_orchestrator.build_runtime()) -- thread-local storage, not a plain instance
        # attribute, so two users' requests running on different worker threads at the same
        # time can never see or overwrite each other's session context. Each thread sets its
        # own value at the start of a request and reads only that value for its duration.
        self._local = threading.local()
        self._knowledge_ctx: Optional["KnowledgeContext"] = None

    def set_knowledge_context(self, ctx: "KnowledgeContext") -> None:
        """Bind the centralised KnowledgeContext built at startup."""
        self._knowledge_ctx = ctx

    @property
    def _session_context(self) -> str:
        return getattr(self._local, "session_context", "")

    def set_session_context(self, context: str) -> None:
        """Receive dynamic session context (e.g. accumulated corrections) for prompt injection.

        Thread-local: only visible to whichever request's worker thread called this, see
        __init__'s note on why this can't be a plain instance attribute.
        """
        self._local.session_context = context

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_sizing_request_from_nl(self, query: str) -> tuple[Optional[AudienceSizingRequest], Optional[str]]:
        """Translate a natural-language audience description into an ad-hoc sizing request.

        Returns (request, error_summary). error_summary is populated only when
        request is None, so the caller has the real failure reason instead of
        a silent None it has to guess about.
        """
        ThoughtDisplay.progress("Working out exactly who this audience should include...")
        prompt = _NL_PARSE_PROMPT.format(query=query, bq_project=_DEFAULT_BQ_PROJECT, bq_dataset=_DEFAULT_BQ_DATASET)
        return self._parse_to_adhoc_request(prompt)

    def answer_general_question(self, query: str) -> str:
        """Answer a knowledge-layer question without triggering SQL execution.

        Grounded in the full schema/glossary/business-rules knowledge base
        (see _call_with_knowledge) so the answer reflects confirmed knowledge
        rather than the model's own background assumptions.
        """
        ThoughtDisplay.progress("Looking that up in what we know so far...")
        prompt = (
            f'A consultant asked: "{query}"\n\n'
            "Answer this question using the knowledge base above. "
            "Be concise and business-focused (2-4 sentences). "
            "Do not mention SQL, database columns, or technical identifiers."
        )
        try:
            return self._call_with_knowledge(prompt, expect_json=False)
        except Exception:
            return (
                "I don't have enough context to answer that question directly. "
                "Please try rephrasing or contact the OCTO team."
            )

    def explain_stuck_request(
        self,
        original_query: str,
        intent_type: str,
        knowledge_sources: list,
        error_details: str,
    ) -> str:
        """Generate a short, honest explanation for a request the tool could not complete.

        error_details, when set, is the real failure reason (a technical error
        surfaced from Quant or from Nexus's own request-building step) -- not a
        guess. In that case this must say plainly that something went wrong and
        invite a retry; it must never disguise a technical failure as a
        business-clarification question, since that fabricates an ambiguity
        that was never real. Only when error_details is empty -- meaning the
        pipeline ran without a technical error but still produced nothing --
        does this ask a genuine clarifying question.
        """
        sources_text = ", ".join(knowledge_sources) if knowledge_sources else "none"
        intent_label = intent_type.replace("_", " ") if intent_type else "unknown"

        # Every request this tool handles is ad hoc -- explicitly prevent the AI
        # from borrowing campaign vocabulary from its background context.
        scope_instruction = (
            "IMPORTANT: This is NOT a campaign-specific request. Do not use the word "
            "'campaign' in your response. Treat this as a general audience or data "
            "question.\n\n"
        )

        if error_details:
            prompt = (
                f'A user asked: "{original_query}"\n\n'
                f"The tool classified this as a {intent_label} but hit a real technical problem while "
                f"trying to answer it: {error_details}\n\n"
                + scope_instruction
                + "Write a short response (2-4 sentences) that:\n"
                "1. Acknowledges what you understood the user was asking for, in warm and plain language.\n"
                "2. Tells the user plainly that something went wrong while pulling the answer together. "
                "Do not invent a business ambiguity or ask them to define a term -- the problem is "
                "technical, not a missing definition, so do not imply otherwise.\n"
                "3. Invites them to try again (possibly rephrasing), or to contact the OCTO team if it "
                "keeps happening.\n\n"
                "Do not use bullet points. Do not mention SQL, database columns, error messages, or other "
                "technical identifiers verbatim. Do not say you are an AI. Do not apologize excessively."
            )
        else:
            prompt = (
                f'A user asked: "{original_query}"\n\n'
                f"The tool classified this as a {intent_label} and consulted these knowledge sources: "
                f"{sources_text}, but genuinely could not find enough information to proceed -- no "
                "technical error occurred; the request itself is missing something.\n\n"
                + scope_instruction
                + "Using the knowledge base above, write a short response (3-5 sentences) that:\n"
                "1. Acknowledges what you understood the user was asking for, in warm and plain language.\n"
                "2. Explains specifically what piece of information or context is missing or unclear.\n"
                "3. Asks one focused, direct question that the user could answer to help you proceed.\n\n"
                "Do not use bullet points. Write in a warm, friendly, conversational tone. "
                "Do not mention SQL, database columns, or technical identifiers. "
                "Do not say you are an AI. Do not apologize excessively."
            )
        try:
            return self._call_with_knowledge(prompt, expect_json=False)
        except Exception:
            if error_details:
                return (
                    "I ran into a technical problem while working on that. Could you try again? "
                    "If it keeps happening, please reach out to the OCTO team."
                )
            return (
                "I understood your request but I wasn't able to generate a result with the "
                "information I currently have. Could you share any additional context that "
                "might help me proceed? For example, any specific criteria, timeframes, or "
                "definitions that apply to this request."
            )

    # ------------------------------------------------------------------
    # Request construction
    # ------------------------------------------------------------------

    def _parse_to_adhoc_request(self, prompt: str) -> tuple[Optional[AudienceSizingRequest], Optional[str]]:
        try:
            raw = self._call_with_knowledge(prompt)
            data = self._extract_json(raw)
            return AudienceSizingRequest(**data), None
        except ValidationError as exc:
            summary = f"Nexus produced an invalid sizing request ({exc.error_count()} field error(s))"
            print(f"  [Nexus] Ad-hoc payload validation failed ({exc.error_count()} field error(s))")
            return None, summary
        except Exception as exc:
            summary = f"Nexus request build error: {exc.__class__.__name__}: {exc}"
            print(f"  [Nexus] Ad-hoc request build error: {exc.__class__.__name__}: {exc}")
            return None, summary

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    def _call_with_knowledge(self, user_query: str, expect_json: bool = True) -> str:
        """Call the AI model with the full knowledge-layer context prepended.

        The confirmed schema, glossary, and business rules for every synced
        table are the model's authoritative reference for this call -- this is
        what makes Nexus's answers grounded rather than freely inferred.

        expect_json: True for callers building a structured payload
        (build_sizing_request_from_nl); False for callers that want a warm,
        plain-English answer (answer_general_question, explain_stuck_request) --
        those must not carry the "return only JSON" system mandate, since that
        directly contradicts what their own prompt is asking for.
        """
        if self._knowledge_ctx is not None:
            cached_text = (
                "VIBE OCTO KNOWLEDGE BASE\n"
                "(Authoritative reference -- confirmed business rules, glossary, and schema)\n\n"
                + self._knowledge_ctx.nexus_context
            )
        else:
            cached_text = "(knowledge layer not available for this call)"

        prompt = cached_text + "\n\n" + user_query

        base_system = _NEXUS_SYSTEM if expect_json else _NEXUS_IDENTITY + " " + _NEXUS_PROSE_MANDATE
        system = (
            base_system + "\n\n" + self._session_context
            if self._session_context
            else base_system
        )
        return ask_ai(prompt, system=system, temperature=0, max_tokens=_MAX_TOKENS_QUERY)

    @staticmethod
    def _extract_json(text: str) -> dict:
        return extract_json(text)
