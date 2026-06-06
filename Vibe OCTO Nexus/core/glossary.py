from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pydantic_schemas import SemanticFailureLog


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

    def patch_from_failure(self, log_entry: "SemanticFailureLog") -> int:
        """Infer new or corrected terms from a semantic failure and merge into the glossary.

        For each entry in log_entry.glossary_gaps that is not already present:
          - Adds a stub entry with confidence_score=0.3, source='failure_inference'.

        For 'wrong_column' failures: finds the closest existing term matching
          any incorrect column reference and reduces its confidence_score by 0.15
          (floor at 0.05).

        For 'missing_exclusion' failures: extracts the missing term from
          correction_description and adds a stub exclusion entry.

        Saves the glossary with portfolio='inferred_failure' and returns the count
        of newly added terms.
        """
        added = 0
        terms = self.data.setdefault("terms", {})

        for gap in log_entry.glossary_gaps:
            key = gap.lower().strip()
            if key and key not in terms:
                terms[key] = {
                    "meaning": f"Inferred from failure correction: {log_entry.campaign_code}",
                    "criterion_type": "unknown",
                    "confidence_score": 0.3,
                    "source": "failure_inference",
                    "frequency_in_briefs": 1,
                    "variations": [],
                }
                added += 1

        if log_entry.inferred_failure_type == "wrong_column":
            # Find column-like tokens in the correction and reduce confidence
            # of any matching existing term to flag it as potentially wrong.
            col_tokens = re.findall(
                r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b",
                log_entry.correction_description.lower(),
            )
            for tok in col_tokens:
                if tok in terms:
                    entry = terms[tok]
                    current = float(entry.get("confidence_score", 0.5))
                    entry["confidence_score"] = max(0.05, current - 0.15)

        elif log_entry.inferred_failure_type == "missing_exclusion":
            # Extract the noun phrase after "missing" or "exclusion" as a stub term.
            m = re.search(
                r"\b(?:missing|exclusion)\s+([a-z][a-z0-9_\s]{2,30})",
                log_entry.correction_description.lower(),
            )
            if m:
                new_term = m.group(1).strip().replace(" ", "_")
                if new_term and new_term not in terms:
                    terms[new_term] = {
                        "meaning": "Missing exclusion term inferred from HITL correction.",
                        "criterion_type": "exclude",
                        "confidence_score": 0.3,
                        "source": "failure_inference",
                        "frequency_in_briefs": 1,
                        "variations": [],
                    }
                    added += 1

        built_from = self.data.get("glossary_metadata", {}).get("built_from_briefs", 0)
        self.save(portfolio="inferred_failure", built_from=built_from)
        return added

    def to_json_string(self) -> str:
        return json.dumps(self.data, indent=2, ensure_ascii=False)

    def summary(self) -> str:
        meta = self.data.get("glossary_metadata", {})
        return (
            f"Portfolio: {meta.get('portfolio', 'unknown')} | "
            f"Terms: {len(self.data['terms'])} | "
            f"Built from: {meta.get('built_from_briefs', 0)} briefs"
        )
