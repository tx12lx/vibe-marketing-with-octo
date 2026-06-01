from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class GlossaryManager:
    def __init__(self, glossary_path: str):
        self.path = Path(glossary_path)
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return {"glossary_metadata": {}, "terms": {}}

    @property
    def terms(self) -> dict:
        return self.data.get("terms", {})

    def merge_learned_terms(self, new_terms: dict) -> int:
        """Merge terms from one brief analysis into the glossary. Returns count of new terms added."""
        added = 0
        for term, entry in new_terms.items():
            if not isinstance(entry, dict):
                continue
            term_lower = term.lower().strip()
            if term_lower in self.data["terms"]:
                existing = self.data["terms"][term_lower]
                existing["frequency_in_briefs"] = existing.get("frequency_in_briefs", 1) + 1
                new_conf = entry.get("confidence_score", 0)
                if new_conf > existing.get("confidence_score", 0):
                    # Update with higher-confidence version, preserving any BQ mapping if present
                    for field in ("meaning", "criterion_type", "logic", "threshold",
                                  "confidence_score", "bq_field", "operator", "value",
                                  "sql_snippet"):
                        if field in entry:
                            existing[field] = entry[field]
                existing_vars = set(existing.get("variations", []))
                new_vars = set(entry.get("variations", []))
                existing["variations"] = sorted(existing_vars | new_vars)
            else:
                self.data["terms"][term_lower] = {
                    **entry,
                    "frequency_in_briefs": 1,
                    "source": "learned",
                }
                added += 1
        return added

    def merge_from_brief_json(self, brief_json: dict) -> int:
        """Extract include/exclude criteria from the structured brief JSON and merge into glossary."""
        added = 0
        medium = brief_json.get("campaign", {}).get("medium", [])
        if isinstance(medium, str):
            medium = [medium]

        for items, criterion_type in [
            (brief_json.get("targeting", {}).get("include", []), "include"),
            (brief_json.get("targeting", {}).get("exclude", []), "exclude"),
        ]:
            for item in items:
                if not isinstance(item, dict):
                    continue
                term = item.get("term", "").lower().strip()
                if not term:
                    continue
                entry = {
                    "meaning": item.get("logic", ""),
                    "criterion_type": criterion_type,
                    "logic": item.get("logic", ""),
                    "threshold": item.get("threshold"),
                    "lookback_days": item.get("lookback_days"),
                    "confidence_score": item.get("confidence_score", 0.8),
                    "context": {"medium": medium},
                    "variations": [],
                }
                if term in self.data["terms"]:
                    existing = self.data["terms"][term]
                    existing["frequency_in_briefs"] = existing.get("frequency_in_briefs", 1) + 1
                    if entry["confidence_score"] > existing.get("confidence_score", 0):
                        for field in ("meaning", "criterion_type", "logic", "threshold",
                                      "lookback_days", "confidence_score"):
                            if field in entry:
                                existing[field] = entry[field]
                    existing_med = set(existing.get("context", {}).get("medium", []))
                    if "context" not in existing:
                        existing["context"] = {}
                    existing["context"]["medium"] = sorted(existing_med | set(medium))
                else:
                    self.data["terms"][term] = {**entry, "frequency_in_briefs": 1, "source": "learned"}
                    added += 1
        return added

    def add_campaign_summary(self, campaign_id: str, summary: str) -> None:
        """Store a plain-English summary of a brief's campaign logic."""
        if "campaign_summaries" not in self.data:
            self.data["campaign_summaries"] = {}
        self.data["campaign_summaries"][campaign_id] = summary

    def save(
        self,
        portfolio: str,
        built_from: int,
        held_out: int = 0,
        version: str = "1.0",
    ) -> None:
        self.data["glossary_metadata"] = {
            "portfolio": portfolio,
            "version": version,
            "built_from_briefs": built_from,
            "held_out_briefs": held_out,
            "total_portfolio_briefs": built_from + held_out,
            "last_updated": datetime.now(tz=timezone.utc).isoformat(),
            "total_terms": len(self.data["terms"]),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def to_json_string(self) -> str:
        return json.dumps(self.data, indent=2, ensure_ascii=False)

    def summary(self) -> str:
        meta = self.data.get("glossary_metadata", {})
        return (
            f"Portfolio: {meta.get('portfolio', 'unknown')} | "
            f"Terms: {len(self.data['terms'])} | "
            f"Built from: {meta.get('built_from_briefs', 0)} briefs"
        )
