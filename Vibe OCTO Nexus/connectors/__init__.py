"""Vibe Briefing connector layer.

Each connector transforms the master brief JSON into a tool-specific output
slice. All connectors are registered in _CONNECTORS and run automatically
via enrich().

To add a new downstream tool:
1. Create connectors/your_tool.py subclassing BaseConnector
2. Add it to _CONNECTORS below

The enrich() function wraps a brief JSON in the full master contract:
  _meta        — schema version, tool, portfolio, timestamps
  <brief data> — campaign, targeting, default_exclusions, channel, vendor, etc.
  connectors   — one entry per registered connector
  feedback_log — append-only log for downstream tools to push results back
"""

from __future__ import annotations

from datetime import datetime, timezone

from .base import BaseConnector
from .sizing_tool import SizingToolConnector
from .brief_regeneration import BriefRegenerationConnector
from .brief_assistant import BriefAssistantConnector

SCHEMA_VERSION = "1.0"

_CONNECTORS: list[BaseConnector] = [
    SizingToolConnector(),
    BriefRegenerationConnector(),
    BriefAssistantConnector(),
]


def enrich(brief_json: dict, glossary_data: dict, glossary_version: str = "unknown") -> dict:
    """Wrap brief JSON in the master contract and run all connectors.

    Args:
        brief_json: Raw structured JSON from the learn/translate phase.
        glossary_data: Full glossary dict including any BQ mappings.
        glossary_version: Version string from the glossary metadata.

    Returns:
        Master contract dict with _meta, all brief fields, connectors block,
        and an empty feedback_log ready for downstream tool writes.
    """
    connector_outputs: dict[str, dict] = {}
    for connector in _CONNECTORS:
        try:
            connector_outputs[connector.name] = connector.generate(brief_json, glossary_data)
        except Exception as exc:
            connector_outputs[connector.name] = {
                "status": "error",
                "error": str(exc),
            }

    return {
        "_meta": {
            "schema_version": SCHEMA_VERSION,
            "tool": "vibe-briefing",
            "portfolio": brief_json.get("campaign", {}).get("portfolio", ""),
            "glossary_version": glossary_version,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        },
        **brief_json,
        "connectors": connector_outputs,
        "feedback_log": brief_json.get("feedback_log", []),
    }


def registered_connectors() -> list[str]:
    """Return names of all registered connectors."""
    return [c.name for c in _CONNECTORS]
