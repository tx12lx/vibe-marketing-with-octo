from __future__ import annotations

from core.claude_client import ClaudeClient

_SYSTEM = (
    "You are an intent routing agent for Vibe Briefing, a campaign intelligence platform. "
    "Parse natural language requests and return structured routing JSON. "
    "Return only valid JSON, no explanation."
)

_PROMPT = """Parse this user request for Vibe Briefing.

Available actions:
- size_audience   : user wants to count eligible customers ("how many", "eligible count", "audience size", "run sizing")
- get_criteria    : user wants targeting/eligibility criteria explained ("what are the criteria", "who qualifies", "targeting logic", "tell me about the campaign")
- validate_brief  : user wants to check brief quality or completeness ("what's missing", "is the brief complete", "any issues", "review the brief")
- regenerate_brief: user wants a clean standardized brief produced ("generate a brief", "clean up the brief", "standardize")
- general_info    : anything else about a campaign

User request: "{question}"

Known campaign names for reference:
{known_campaigns}

Return ONLY this JSON — no markdown, no explanation:
{{
  "action": "<size_audience | get_criteria | validate_brief | regenerate_brief | general_info>",
  "campaign_hint": "<campaign name or description as mentioned — null if not clear>",
  "portfolio": "<portfolio code or name if mentioned — null if not>",
  "brand": "<TELUS | Koodo | null>",
  "medium": "<email | sms | mms | ob | null>",
  "confidence": <0.0-1.0 — how confident you are in this routing>
}}"""


class IntentRouter:
    """Routes natural language user requests to the appropriate Vibe Briefing action.

    Uses Claude to parse intent, extract campaign references, and identify
    which downstream connector should handle the request.
    """

    def __init__(self):
        self._client = ClaudeClient()

    def route(self, question: str, known_campaigns: list[str] | None = None) -> dict:
        """Parse a natural language question into a structured routing decision.

        Args:
            question: The user's natural language request.
            known_campaigns: List of known campaign IDs to help with resolution.

        Returns:
            Dict with keys: action, campaign_hint, portfolio, brand, medium, confidence.
        """
        campaigns_str = (
            "\n".join(f"  - {c}" for c in known_campaigns)
            if known_campaigns
            else "  (none loaded yet — run learn first)"
        )
        prompt = _PROMPT.format(question=question, known_campaigns=campaigns_str)
        raw = self._client._call(_SYSTEM, prompt)
        result = self._client._extract_json(raw)
        # Ensure required keys exist
        result.setdefault("action", "general_info")
        result.setdefault("campaign_hint", None)
        result.setdefault("confidence", 0.5)
        return result
