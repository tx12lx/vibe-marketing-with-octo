"""
Vibe OCTO Nexus — Intent Classification & Sizing Request Agent

Nexus has two jobs, both grounded in the knowledge layer (real column schema,
glossary, and confirmed business rules for the synced table) rather than any
hardcoded taxonomy:

  1. classify_intent() — decide whether a request needs the data queried
     (sizing_request) or can be answered directly from the knowledge base
     (general_question).

  2. build_sizing_request_from_nl() — translate a natural-language audience
     description directly into a validated AudienceSizingRequest for Quant,
     using the schema/glossary/business-rules context as the authoritative
     reference for what any term means.

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
4. If cadence or medium are not stated, use empty strings.
5. Leave exclusion_layers as an empty list unless a confirmed business rule or the
   consultant's own request calls for one.
6. Write optimization_context as exactly 3 concise sentences focused solely on cadence risk,
   channel suitability, or send-timing safety for this specific input.

Return exactly this JSON (no markdown):
{{
  "audience_label": "<short descriptive label for this audience segment, for display only>",
  "cadence": "<cadence if explicitly stated by consultant, else 'ad-hoc'>",
  "medium": "<medium if explicitly stated by consultant, else 'unspecified'>",
  "target_population": "<precise plain-English restatement of who qualifies>",
  "filters": ["<filter grounded in the schema/glossary/business rules or the consultant's stated criteria>"],
  "exclusion_layers": [],
  "optimization_context": "<3 sentences on cadence or channel safety>",
  "bq_project": "{bq_project}",
  "bq_dataset": "{bq_dataset}"
}}"""

_INTENT_CLASSIFY_V2_PROMPT = """\
A consultant submitted the following request to a Canadian telecom marketing AI:
  "{query}"

Knowledge context loaded:
  Known glossary terms: {glossary_summary}

Classify this request into exactly one intent type:

  "sizing_request"     -- User wants an audience count or headcount, or is
                         otherwise asking something that requires querying the
                         data.

  "general_question"   -- User has a question about data or strategy that
                         does not require querying the data -- it can be
                         answered directly from the knowledge base.

Note: this classification step only ever consults the glossary summary shown
above -- it never sees the confirmed business rules text, so it cannot
honestly report which rules were applied. Always return an empty list for
business_rules_applied here; the orchestrator fills knowledge_sources_consulted
itself from what was actually consulted.

Return exactly this JSON (no markdown, no explanation):
{{
  "intent_type": "<sizing_request or general_question>",
  "confidence": <0.0 to 1.0>,
  "knowledge_sources_consulted": [],
  "business_rules_applied": [],
  "reasoning": "<one sentence explaining the classification>"
}}"""


class NexusAgent(BaseAgent):
    WORKER_ID = "nexus_v1"
    HANDLED_INTENTS: frozenset[str] = frozenset({"general_question"})
    INPUT_SCHEMA = AudienceSizingRequest
    OUTPUT_SCHEMA = IntentClassification

    def subscribe(self, spec) -> None:
        """Not used — NexusAgent is invoked via classify_intent(), not subscribe/execute."""

    def execute(self) -> IntentClassification:
        """Not used — NexusAgent is invoked via classify_intent(), not subscribe/execute."""
        raise NotImplementedError(
            "NexusAgent does not use the subscribe/execute interface. "
            "Call classify_intent() directly."
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

    def classify_intent(self, query: str) -> IntentClassification:
        """Classify user intent by consulting the knowledge layer.

        Falls back to a safe default if classification itself fails.
        """
        glossary_summary = (
            self._knowledge_ctx.glossary_summary
            if self._knowledge_ctx is not None
            else "(knowledge layer not available)"
        )
        sources_consulted = ["glossary"]

        prompt = _INTENT_CLASSIFY_V2_PROMPT.format(
            query=query,
            glossary_summary=glossary_summary,
        )
        try:
            raw = self._call_simple(prompt)
            data = self._extract_json(raw)
            # Always trust Python's own record of what was consulted over
            # whatever the model echoed back -- this is what actually ran,
            # not a claim the model is in a position to verify. Likewise,
            # this call never receives business-rule text, so it can never
            # honestly report a rule as applied.
            data["knowledge_sources_consulted"] = sources_consulted
            data["business_rules_applied"] = []
            classification = IntentClassification(**data)
        except Exception:
            classification = IntentClassification(
                intent_type="sizing_request",
                confidence=0.5,
                knowledge_sources_consulted=sources_consulted,
                reasoning="Classification failed; defaulting to sizing_request",
            )

        ThoughtDisplay.intent_classified(
            classification.intent_type,
            query,
            confidence=classification.confidence,
            knowledge_sources=classification.knowledge_sources_consulted,
        )
        return classification

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

    def _call_simple(self, user_prompt: str) -> str:
        """Single call, no knowledge-base block prepended -- used for classification,
        where the caller has already assembled exactly the (small) context it needs."""
        system = (
            _NEXUS_SYSTEM + "\n\n" + self._session_context
            if self._session_context
            else _NEXUS_SYSTEM
        )
        return ask_ai(user_prompt, system=system, temperature=0, max_tokens=_MAX_TOKENS_BUILD)

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
