"""tools/sizing_tools.py — Tool schema for structured audience sizing output.

Design-time only.  Activation: set FUELIX_USE_TOOL_CALLING=1 and confirm
Fuel iX supports tool use.  When active, NexusAgent uses this instead of
asking the model to "return JSON" and parsing the response with regex.

Migration path:
  Phase 3E now: schema defined here, JSON-prompting still used in nexus_agent.py
  When Fuel iX confirms tool use: flip use_tool_calling flag in the _call methods,
  remove regex JSON parsing fallbacks in nexus_agent.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pydantic_schemas import AudienceSizingRequest  # noqa: E402

# ---------------------------------------------------------------------------
# Audience sizing request tool
# ---------------------------------------------------------------------------

emit_sizing_request: dict = {
    "name": "emit_sizing_request",
    "description": (
        "Emit a validated audience sizing request for the Quant agent. "
        "Call this tool to produce a structured sizing request from the user's "
        "natural language campaign description. All fields must be populated — "
        "do not leave target_population or filters empty."
    ),
    "input_schema": AudienceSizingRequest.model_json_schema(),
}

# ---------------------------------------------------------------------------
# Intent classification tool
# ---------------------------------------------------------------------------

classify_intent_tool: dict = {
    "name": "classify_intent",
    "description": (
        "Classify the user's request intent and identify the campaign if mentioned. "
        "Return intent_type, confidence, whether a campaign was identified, and "
        "which knowledge sources were consulted."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "intent_type": {
                "type": "string",
                "enum": [
                    "sizing_request",
                    "brief_generation",
                    "brief_qa",
                    "campaign_execution",
                    "general_question",
                ],
                "description": "The classified intent type",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Classification confidence 0.0-1.0",
            },
            "campaign_identified": {
                "type": "boolean",
                "description": "Whether a specific campaign was identified in the request",
            },
            "campaign_code": {
                "type": "string",
                "description": "Campaign code if identified (e.g. AALBAU, PFE). Null if not identified.",
            },
            "knowledge_sources_consulted": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Which knowledge sources were used (glossary, campaign_index, gold_patterns)",
            },
            "reasoning": {
                "type": "string",
                "description": "One-sentence explanation of the classification decision",
            },
        },
        "required": ["intent_type", "confidence", "campaign_identified"],
    },
}
