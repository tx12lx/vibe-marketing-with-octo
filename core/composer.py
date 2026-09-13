"""core/composer.py -- the single shared step every agent's routine, user-facing
wording flows through, so the whole tool speaks with one consistent, warm,
professional voice instead of each corner hand-rolling its own tone.

Policy (deliberately explicit, not left fuzzy -- this is the resolution to the
"never hardcode, but what about when the AI itself just failed" tension):
everything on a normal, explainable path -- an answer, a narrated caveat, a
greeting, a clarifying question -- must be composed here, grounded strictly in
real facts the caller supplies (never invented). The one sanctioned exception
is when the AI call that would have generated the real message has itself just
failed (timeout, malformed response, network error): asking that same broken
AI to explain its own failure in the same breath is circular, so compose()
falls back to a small, fixed, neutral line in that case ONLY -- see
_DEGRADED_MODE_FALLBACKS below. That dict is the one place a hardcoded
user-facing string is allowed, and only for this reason; it must not quietly
grow into a place to stash "convenient" canned sentences for anything else.
"""
from __future__ import annotations

import logging

from core.ai_client import ask_ai

_log = logging.getLogger(__name__)

_COMPOSER_SYSTEM = (
    "You are the voice of Vibe OCTO, an AI teammate for a telecom marketing team. "
    "Write in plain English, warm and professional -- like a sharp, friendly colleague, "
    "never robotic or corporate. Never use jargon the person didn't already use themselves. "
    "Be concise: say only what's needed for this specific moment, no filler, no preamble "
    "like 'Sure, here is...'. Never invent facts -- only narrate the concrete facts you're given."
)

# The one sanctioned exception to "never hardcode a user-facing sentence" -- see module
# docstring. Used ONLY when the AI call inside compose() itself failed, never otherwise.
_DEGRADED_MODE_FALLBACKS: dict[str, str] = {
    "greeting": "Hi! Ask me something and I'll do my best to help.",
    "general_answer": "I've answered based on what I currently know. Let me know if you need more detail.",
    "stuck": "I wasn't able to fully work through that. Could you add a bit more detail?",
    "optimization_note": (
        "This result was narrowed down significantly by the filters applied -- worth "
        "double-checking they're what you intended."
    ),
    "clarifying_question": "Could you clarify what you meant, so I get this right?",
    "error": "I ran into a technical problem and wasn't able to complete that. Please try again in a moment.",
}
_DEFAULT_FALLBACK = "Something didn't go as expected there -- please try again in a moment."


def compose(outcome_kind: str, facts: dict, *, max_sentences: int = 3, temperature: float = 0.4) -> str:
    """Ask the AI to write the actual sentence(s) shown to a person for this outcome,
    grounded strictly in `facts` (never inventing new information beyond them).

    Never raises -- on any failure this returns the fixed, neutral fallback for
    `outcome_kind` (see module docstring for why that's the one sanctioned exception),
    so a caller can always trust it gets *some* reasonable string back.
    """
    facts_text = "\n".join(f"- {k}: {v}" for k, v in facts.items() if v not in (None, "")) or "(none)"
    prompt = (
        f"Situation: {outcome_kind}\n"
        f"Known facts (do not invent anything beyond these):\n{facts_text}\n\n"
        f"Write what to say to the person, in at most {max_sentences} sentence(s)."
    )
    try:
        text = ask_ai(
            prompt, system=_COMPOSER_SYSTEM, temperature=temperature, max_tokens=200, thinking_budget=0,
        ).strip()
        if text:
            return text
    except Exception:
        _log.exception("compose() failed for outcome_kind=%s", outcome_kind)
    return _DEGRADED_MODE_FALLBACKS.get(outcome_kind, _DEFAULT_FALLBACK)
