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

_FUELIX_BASE = "https://api.fuelix.ai"


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
        self._load()

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

    def get_rules_for_execution(
        self,
        camp_id: str,
        medium: str,
        cadence: str,
        campaign_purpose: str,
        spec: UniversalJSONSpec,
    ) -> list[BusinessRule]:
        """Return applicable rules for this execution, sorted by priority descending."""
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._rules_path.exists():
            self._rules = []
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
        except Exception:
            self._rules = []

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
        api_key = os.getenv("FUELIX_API_KEY")
        model = os.getenv("FUELIX_MODEL", "claude-sonnet-4")
        if not api_key:
            return pattern_rules

        try:
            import requests  # noqa: PLC0415

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
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 256,
                "temperature": 0,
            }
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            resp = requests.post(
                f"{_FUELIX_BASE}/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            resp_json = resp.json()
            choices = resp_json.get("choices") or []
            text = ""
            if choices:
                msg = choices[0].get("message") or {}
                text = str(msg.get("content") or "")

            m = re.search(r"\{[^}]+\}", text, re.DOTALL)
            if m:
                result = json.loads(m.group(0))
                matching_ids = set(result.get("matching_rule_ids", []))
                return [r for r in pattern_rules if r.rule_id in matching_ids]
        except Exception:
            pass

        return pattern_rules
