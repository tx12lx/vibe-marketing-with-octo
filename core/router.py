"""core/router.py -- the bot's own "which specialist should handle this"
reasoning, kept as its own small, focused component rather than living inside
any one specialist agent.

Why this is separate from any agent: today's two specialists (Nexus, Quant)
are peers, not one-special-one-ordinary -- folding "decide who handles this"
into one of them would mean that agent has to know every sibling's job just
to decide whether to hand off, which gets backwards and increasingly tangled
as more agents are added. A router that depends on the registry (not the
other way around) stays clean no matter how many agents exist.

This follows the "Routing" workflow pattern from Anthropic's own published
guidance on building agents -- classify the request, then send it to a
clearly-described specialist -- see
https://www.anthropic.com/engineering/building-effective-agents

The classification prompt is built ENTIRELY from each registered agent's own
CAPABILITIES (see pydantic_schemas.AgentCapability) -- no agent name or intent
string is ever hardcoded here. Adding a new agent with new CAPABILITIES changes
what this router can pick with zero code change to this file.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.ai_client import ask_ai
from core.json_extract import extract_json
from core.thought_display import ThoughtDisplay
from pydantic_schemas import IntentClassification

if TYPE_CHECKING:
    from core.base_agent import BaseAgent

_log = logging.getLogger(__name__)

# Below this confidence, the router asks a clarifying question instead of
# guessing which specialist fits -- "don't guess, ask" applies to routing
# itself, not just to interpreting corrections.
_CONFIDENCE_FLOOR = 0.55

_ROUTER_SYSTEM = (
    "You are Vibe OCTO's routing brain -- a senior AI teammate for a telecom marketing "
    "team, powered by Claude Sonnet 5. Your only job right now is to read a request and "
    "decide which specialist on the team should handle it, from the real list of "
    "specialists given to you. Never assume a request is about any one specific kind of "
    "task just because that's the most common one -- read what's actually being asked."
)

_ROUTER_PROMPT = """{context_block}A person submitted this request:
  "{query}"

Here is the real, current list of specialists available, and what each one is for:

{capability_menu}

Classify this request into exactly one of the intent types listed above. If recent
conversation is given above, use it to resolve a request that only makes sense as a
follow-up (e.g. "what about Quebec instead?" after a prior sizing question is itself a
sizing request, not an ambiguous one -- don't treat it as unclassifiable just because it
has no meaning standing alone). If you are not at least {confidence_pct}% confident which
specialist genuinely fits even after considering that context, say so honestly instead of
guessing -- set needs_clarification to true and write one specific, friendly question that
would resolve the ambiguity.

Also write one short, warm, plain-English sentence telling the person what you understood
them to be asking and that you're getting started -- tailored to what THIS request
actually asked, not a generic stock phrase. No jargon, no mention of "intent" or
"classification". (Skip this if needs_clarification is true -- the question above covers it.)

Return exactly this JSON (no markdown, no explanation):
{{
  "intent_type": "<one of the intent types listed above>",
  "confidence": <0.0 to 1.0>,
  "reasoning": "<one sentence explaining the choice>",
  "narration": "<the warm sentence described above, or empty string if needs_clarification>",
  "needs_clarification": <true or false>,
  "clarifying_question": "<a specific question, only if needs_clarification is true, else empty string>"
}}"""


class IntentRouter:
    """Decides which registered agent should handle a request, reasoning over
    the live set of agents actually available rather than a fixed, hardcoded
    pair of choices."""

    def __init__(self, agents: dict[str, "BaseAgent"]) -> None:
        self._agents = agents

    def _capability_menu(self) -> str:
        lines: list[str] = []
        for agent in self._agents.values():
            for cap in getattr(agent, "CAPABILITIES", None) or []:
                example_text = f" (e.g. {'; '.join(cap.examples)})" if cap.examples else ""
                lines.append(f'  "{cap.intent_type}" -- {cap.description}{example_text}')
        return "\n".join(lines) if lines else '  "general_question" -- (no specialists registered)'

    def classify(self, query: str, context: str = "") -> IntentClassification:
        """Never raises -- falls back to a safe, low-confidence default (routed to
        whichever specialist looks most like a general-purpose fallback) if the
        classification call itself fails, so one broken call never crashes the
        whole request.

        context is this session's recent conversation (and any confirmed
        corrections), the same string vibe_orchestrator.process_core_request()
        already builds via KnowledgeContext.get_dynamic_context() and hands to
        every agent -- without it, a follow-up like "what about Quebec
        instead?" is classified with no idea a prior sizing question exists,
        looks ambiguous in isolation, and gets misrouted to a clarifying
        question instead of the specialist that could actually have answered
        it using that same context.
        """
        menu = self._capability_menu()
        context_block = f"{context}\n\n" if context else ""
        prompt = _ROUTER_PROMPT.format(
            context_block=context_block, query=query, capability_menu=menu,
            confidence_pct=int(_CONFIDENCE_FLOOR * 100),
        )
        try:
            raw = ask_ai(prompt, system=_ROUTER_SYSTEM, temperature=0, max_tokens=600, thinking_budget=0)
            data = extract_json(raw)
            confidence = float(data.get("confidence", 0.5))
            needs_clarification = bool(data.get("needs_clarification")) or confidence < _CONFIDENCE_FLOOR
            classification = IntentClassification(
                intent_type=data.get("intent_type", "general_question"),
                confidence=confidence,
                reasoning=data.get("reasoning", ""),
                narration=data.get("narration", ""),
                needs_clarification=needs_clarification,
                clarifying_question=data.get("clarifying_question", "") if needs_clarification else "",
                knowledge_sources_consulted=[],
                business_rules_applied=[],
            )
        except Exception:
            _log.exception("IntentRouter.classify() failed; asking the person to clarify instead of guessing.")
            classification = IntentClassification(
                intent_type="general_question",
                confidence=0.0,
                reasoning="Routing failed; asking for clarification rather than guessing.",
                needs_clarification=True,
                clarifying_question=(
                    "I had trouble understanding that request -- could you rephrase it, "
                    "maybe with a bit more detail?"
                ),
            )

        if not classification.needs_clarification:
            ThoughtDisplay.intent_classified(
                classification.intent_type,
                query,
                confidence=classification.confidence,
                knowledge_sources=classification.knowledge_sources_consulted,
                narration=classification.narration or None,
            )
        return classification
