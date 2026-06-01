from __future__ import annotations

from .base import BaseConnector


class SizingToolConnector(BaseConnector):
    """Confirms the brief has the required targeting structure for the sizing tool.

    The sizing tool builds the full audience waterfall at query time from the
    brief's targeting criteria. This connector validates that structure is present
    and returns a ready status — it does not generate SQL or BQ field mappings.
    """

    name = "sizing_tool"

    def generate(self, brief_json: dict, glossary_data: dict) -> dict:
        include_count = len(brief_json.get("targeting", {}).get("include", []))
        exclude_count = len(brief_json.get("targeting", {}).get("exclude", []))
        return {
            "status": "ready",
            "include_criteria_count": include_count,
            "exclude_criteria_count": exclude_count,
        }
