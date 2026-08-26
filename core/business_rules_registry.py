"""core/business_rules_registry.py — Verified Business Rules Registry.

Persists and retrieves human-verified business rules extracted by FeedbackAgent.
Rules are stored in business_rules.json and applied automatically to future specs.
Thread-safety: single-process writes only; uses atomic os.replace().
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pydantic_schemas import BusinessRule, UniversalJSONSpec  # noqa: E402
from core.ai_client import ask_ai  # noqa: E402


class BusinessRulesRegistry:
    """Load, store, and apply human-verified business rules.

    Lifecycle:
      __init__(rules_path)          — loads rules from disk
      add_rule(rule)                — appends and persists immediately
      get_rules_for_execution(...)  — returns applicable rules sorted by priority
      apply_rules_to_spec(...)      — returns a modified copy of the spec
      get_display_summary(rules)    — plain English for ThoughtDisplay
    """

    def __init__(self, rules_path: Path) -> None:
        self._rules_path = Path(rules_path)
        self._rules: list[BusinessRule] = []
        self._cache_generation: int = 0
        self._loaded_generation: int = 0
        self._last_applied_ids: list[str] = []
        self._load()

    @property
    def last_applied_ids(self) -> list[str]:
        """Rule IDs applied in the most recent apply_rules_to_spec() call."""
        return list(self._last_applied_ids)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_rule(self, rule: BusinessRule) -> None:
        """Add or replace a rule (matched by rule_id) and persist immediately."""
        replaced = False
        for i, existing in enumerate(self._rules):
            if existing.rule_id == rule.rule_id:
                self._rules[i] = rule
                replaced = True
                break
        if not replaced:
            self._rules.append(rule)
        self._persist()
        self._cache_generation += 1

    def get_rules_for_execution(
        self,
        camp_id: str,
        medium: str,
        cadence: str,
        campaign_purpose: str,
        spec: UniversalJSONSpec,
    ) -> list[BusinessRule]:
        """Return applicable rules for this execution, sorted by priority descending.

        Automatically reloads from disk when a write has occurred since the last
        load so corrections written by FeedbackAgent in the same session take
        effect on the very next call without an explicit _load() from the orchestrator.
        """
        if self._cache_generation != self._loaded_generation:
            self._load()
        if not self._rules:
            return []

        applicable: list[BusinessRule] = []
        pattern_rules: list[BusinessRule] = []

        for rule in self._rules:
            if not rule.applies_to_future:
                continue
            scope = rule.scope.lower()
            if scope == "universal":
                applicable.append(rule)
            elif scope == "campaign":
                if rule.campaign_code and rule.campaign_code.upper() == camp_id.upper():
                    applicable.append(rule)
            elif scope == "pattern":
                pattern_rules.append(rule)

        if pattern_rules:
            matched = self._match_pattern_rules(
                pattern_rules, camp_id, medium, cadence, campaign_purpose, spec
            )
            applicable.extend(matched)

        # Higher priority first (campaign=3 > pattern=2 > universal=1)
        applicable.sort(key=lambda r: -r.priority)
        return applicable

    def apply_rules_to_spec(
        self,
        spec: UniversalJSONSpec,
        rules: list[BusinessRule],
    ) -> UniversalJSONSpec:
        """Return a new spec with all verified rules applied.

        Rules are applied in priority order; higher priority overrides lower
        for conflicting structural changes.
        """
        new_filters = list(spec.filters)
        new_exclusions = list(spec.exclusion_layers or [])
        notes: list[str] = []

        for rule in rules:
            rt = rule.rule_type.lower()
            rv = rule.structured_value

            if rt == "filter_add":
                sql = rv.get("sql") or rv.get("filter", "")
                if sql and sql not in new_filters:
                    new_filters.append(sql)

            elif rt == "exclusion_add":
                sql = rv.get("sql") or rv.get("exclusion", "")
                if sql and sql not in new_exclusions:
                    new_exclusions.append(sql)

            else:
                note = rv.get("note") or rv.get("description") or rule.rule_description
                if note:
                    notes.append(f"[BUSINESS RULE] {note}")

        opt_parts: list[str] = []
        if spec.optimization_context:
            opt_parts.append(spec.optimization_context)
        opt_parts.extend(notes)
        new_opt = "\n".join(opt_parts).strip() or None

        return spec.model_copy(update={
            "filters": new_filters,
            "exclusion_layers": new_exclusions if new_exclusions else spec.exclusion_layers,
            "optimization_context": new_opt,
        })

    def get_display_summary(self, rules: list[BusinessRule]) -> str:
        """Return a plain English summary suitable for ThoughtDisplay."""
        if not rules:
            return ""
        lines = [f"Applying {len(rules)} of your verified business rules:"]
        for rule in rules:
            dt = rule.created_at[:10] if rule.created_at else "unknown date"
            lines.append(f"  - {rule.rule_description} (verified {dt})")
        return "\n".join(lines)

    def record_rules_applied(self, rules: list[BusinessRule]) -> None:
        """Increment applied_count and update last_applied_at for each rule, then persist."""
        if not rules:
            self._last_applied_ids = []
            return
        self._last_applied_ids = [r.rule_id for r in rules]
        now = datetime.now(tz=timezone.utc).isoformat()
        ids = {r.rule_id for r in rules}
        changed = False
        for i, existing in enumerate(self._rules):
            if existing.rule_id in ids:
                self._rules[i] = existing.model_copy(update={
                    "applied_count": existing.applied_count + 1,
                    "last_applied_at": now,
                })
                changed = True
        if changed:
            self._persist()
            self._cache_generation += 1

    def record_hitl_yes(self, rule_ids: list[str]) -> None:
        """Increment hitl_yes_after_rule_count for each rule ID, then persist.

        Called by the orchestrator when the user confirms YES on a query where
        rules were applied, so each rule's effectiveness score grows over time.
        """
        if not rule_ids:
            return
        id_set = set(rule_ids)
        changed = False
        for i, existing in enumerate(self._rules):
            if existing.rule_id in id_set:
                self._rules[i] = existing.model_copy(update={
                    "hitl_yes_after_rule_count": existing.hitl_yes_after_rule_count + 1,
                })
                changed = True
        if changed:
            self._persist()
            self._cache_generation += 1

    def find_similar_or_conflicting(
        self, new_rule: BusinessRule, api_key: str, model: str
    ) -> dict:
        """Use LLM to check whether new_rule duplicates or conflicts with any existing rule.

        Returns a dict with keys:
          is_duplicate   : bool
          duplicate_of   : Optional[str]   — rule_id of the existing rule
          conflicts_with : Optional[str]   — rule_id of the conflicting rule
          conflict_description : Optional[str]
          reinforced_confidence : Optional[float]  — merged confidence if duplicate
        """
        if not self._rules:
            return {"is_duplicate": False, "conflicts_with": None}

        existing_summaries = "\n".join(
            f'{i+1}. rule_id={r.rule_id} scope={r.scope} '
            f'type={r.rule_type} description="{r.rule_description}" '
            f'structured_value={json.dumps(r.structured_value)}'
            for i, r in enumerate(self._rules[:30])
            if r.applies_to_future
        )
        if not existing_summaries:
            return {"is_duplicate": False, "conflicts_with": None}

        prompt = (
            "You are checking whether a new business rule duplicates or conflicts with existing rules.\n\n"
            f"New rule:\n"
            f"  rule_id: {new_rule.rule_id}\n"
            f"  scope: {new_rule.scope}\n"
            f"  rule_type: {new_rule.rule_type}\n"
            f"  description: \"{new_rule.rule_description}\"\n"
            f"  structured_value: {json.dumps(new_rule.structured_value)}\n\n"
            f"Existing rules:\n{existing_summaries}\n\n"
            "Answer these three questions:\n"
            "1. Is the new rule saying essentially the same thing as any existing rule "
            "(even if worded differently)?\n"
            "2. Does the new rule directly contradict any existing rule?\n"
            "3. If duplicate: what is the merged confidence (average of both, max 1.0)?\n\n"
            'Output JSON only — no explanation:\n'
            '{"is_duplicate": <bool>, "duplicate_of": "<rule_id or null>", '
            '"conflicts_with": "<rule_id or null>", '
            '"conflict_description": "<one sentence or null>", '
            '"reinforced_confidence": <float or null>}'
        )
        try:
            text = ask_ai(prompt, temperature=0, max_tokens=256)
            m = re.search(r"\{[^}]+\}", text, re.DOTALL)
            if m:
                return json.loads(m.group(0))
        except Exception:
            pass
        return {"is_duplicate": False, "conflicts_with": None}

    def disable_rule(self, rule_id: str, conflict_note: str) -> None:
        """Mark a rule as applies_to_future=False and record why, then persist."""
        for i, existing in enumerate(self._rules):
            if existing.rule_id == rule_id:
                self._rules[i] = existing.model_copy(update={
                    "applies_to_future": False,
                    "conflict_notes": conflict_note,
                })
                self._persist()
                self._cache_generation += 1
                return

    def reinforce_rule(self, rule_id: str, extra_confidence: float) -> None:
        """Increase confidence of an existing rule (capped at 1.0), then persist."""
        for i, existing in enumerate(self._rules):
            if existing.rule_id == rule_id:
                new_conf = min(1.0, (existing.confidence + extra_confidence) / 2.0)
                self._rules[i] = existing.model_copy(update={"confidence": new_conf})
                self._persist()
                self._cache_generation += 1
                return

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._rules_path.exists():
            self._rules = []
            self._loaded_generation = self._cache_generation
            return
        try:
            data = json.loads(self._rules_path.read_text(encoding="utf-8"))
            loaded: list[BusinessRule] = []
            for raw in data.get("rules", []):
                try:
                    loaded.append(BusinessRule.model_validate(raw))
                except Exception:
                    pass
            self._rules = loaded
            self._loaded_generation = self._cache_generation
        except Exception:
            self._rules = []
            self._loaded_generation = self._cache_generation

    def _persist(self) -> None:
        """Atomically write current rules list to disk."""
        data = {
            "schema_version": "1.0",
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
            "rules": [r.model_dump() for r in self._rules],
        }
        self._rules_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(self._rules_path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
            os.replace(tmp_name, str(self._rules_path))
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    def _match_pattern_rules(
        self,
        pattern_rules: list[BusinessRule],
        camp_id: str,
        medium: str,
        cadence: str,
        campaign_purpose: str,
        spec: UniversalJSONSpec,
    ) -> list[BusinessRule]:
        """Use LLM to identify which pattern rules apply to this campaign.

        Falls back to including all pattern rules if the API key is unavailable
        or the call fails (conservative — better to over-apply than miss a rule).
        """
        try:
            rules_desc = "\n".join(
                f'{i + 1}. rule_id={r.rule_id}  pattern="{r.pattern_description or r.rule_description}"'
                for i, r in enumerate(pattern_rules)
            )
            prompt = (
                "You are checking which stored business rules apply to a campaign.\n\n"
                f"Campaign:\n"
                f"  camp_id: {camp_id}\n"
                f"  campaign_name: {spec.campaign_name}\n"
                f"  medium: {medium}\n"
                f"  cadence: {cadence}\n"
                f"  campaign_purpose: {campaign_purpose}\n\n"
                f"Pattern rules to evaluate:\n{rules_desc}\n\n"
                'Return JSON only: {"matching_rule_ids": ["<rule_id>", ...]}'
            )
            text = ask_ai(prompt, temperature=0, max_tokens=256)

            m = re.search(r"\{[^}]+\}", text, re.DOTALL)
            if m:
                result = json.loads(m.group(0))
                matching_ids = set(result.get("matching_rule_ids", []))
                return [r for r in pattern_rules if r.rule_id in matching_ids]
        except Exception:
            pass

        return pattern_rules
