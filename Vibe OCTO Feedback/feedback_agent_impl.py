"""
Vibe OCTO Feedback — FeedbackAgent Worker
Worker ID: feedback_v1

Interprets natural language corrections from users after HITL NO,
extracts verified business rules via multi-stage interactive pipeline,
and saves them as GOLD-tier context that guides all future executions.

Pipeline stages:
  1. Acknowledge     — display warm transition message
  2. Interpret       — LLM analysis with prompt-cached knowledge base context
  3. Unknown terms   — interactively resolve any unrecognised business terms
  4. Clarify         — ask targeted questions for ambiguous rules
  5. Compound        — surface multi-rule summary and resolve conflicts
  6. Scope           — classify as campaign | pattern | universal
  7. Validate        — confirm each rule with the user before saving
  8. Save            — persist confirmed rules to business_rules.json
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import requests
from dotenv import load_dotenv

if TYPE_CHECKING:
    from core.knowledge_context import KnowledgeContext

_FEEDBACK_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _FEEDBACK_DIR.parent

for _p in [str(_ROOT_DIR), str(_FEEDBACK_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

load_dotenv(_FEEDBACK_DIR / ".env")

from pydantic_schemas import (  # noqa: E402
    BusinessRule,
    FeedbackInput,
    FeedbackOutput,
)
from core.base_agent import BaseAgent  # noqa: E402

import importlib.util as _ilu  # noqa: E402
if "core.business_rules_registry" not in sys.modules:
    _spec = _ilu.spec_from_file_location(
        "core.business_rules_registry",
        _ROOT_DIR / "core" / "business_rules_registry.py",
    )
    _mod = _ilu.module_from_spec(_spec)
    sys.modules["core.business_rules_registry"] = _mod
    _spec.loader.exec_module(_mod)
del _ilu

from core.business_rules_registry import BusinessRulesRegistry  # noqa: E402
from core.thought_display import ThoughtDisplay  # noqa: E402

_FUELIX_BASE = "https://api.fuelix.ai"
_RULES_PATH = _ROOT_DIR / "business_rules.json"
_GLOSSARY_PATH = _ROOT_DIR / "glossary.json"
_KNOWLEDGE_INDEX_PATH = _ROOT_DIR / "knowledge_base" / "artifacts" / "semantic_knowledge_index.json"
_ADOBE_SCHEMA_PATH = _ROOT_DIR / "knowledge_base" / "artifacts" / "adobe_schema.json"

# ---------------------------------------------------------------------------
# System prompt — identity anchor (cached)
# ---------------------------------------------------------------------------

_FEEDBACK_SYSTEM = (
    "You are Vibe OCTO Feedback, a specialist in interpreting marketing analyst "
    "corrections and extracting precise, reusable business rules from them. "
    "Your role is to understand WHAT went wrong in an audience sizing run, "
    "extract each distinct correction as a structured rule, classify its scope "
    "(campaign-specific, pattern-based, or universal), and identify any unknown "
    "terms that require clarification. "
    "You are campaign-agnostic — never hardcode business terms or jargon. "
    "All term understanding must come from the provided knowledge base. "
    "When a term is absent from the knowledge base, flag it as unknown. "
    "Output must be valid JSON matching the required structure exactly. "
    "Never guess at business meaning — flag uncertainty explicitly with "
    "needs_clarification: true."
)

# ---------------------------------------------------------------------------
# Interpretation output schema injected into the dynamic prompt
# ---------------------------------------------------------------------------

_INTERPRETATION_INSTRUCTIONS = """\
Analyse the user's correction and extract ALL distinct business rules it contains.

For each rule:
1. Identify exactly what is being corrected (in plain business language)
2. Determine what correct behaviour should be
3. Flag any ambiguities requiring clarification
4. Classify scope from available signals
5. List any terms not found in the provided knowledge base

Output a single JSON object with this exact structure — no preamble, no explanation:
{
  "rules_found": <integer>,
  "rules": [
    {
      "raw_text": "<exact user words for this specific rule>",
      "understood_as": "<plain English: what this rule requires going forward>",
      "rule_type": "<one of: filter_add | exclusion_add | lookback_days | population_note | general>",
      "structured_value": {
        "sql": "<SQL fragment if rule_type is filter_add or exclusion_add, else omit>",
        "days": <integer if rule_type is lookback_days, else omit>,
        "field": "<column name if rule_type is lookback_days, else omit>",
        "note": "<plain English note for lookback_days / population_note / general>",
        "description": "<always include: one sentence description of what this value represents>"
      },
      "confidence": <float 0.0 to 1.0>,
      "needs_clarification": <true | false>,
      "clarifying_question": "<targeted question if needs_clarification is true, else empty string>",
      "scope_detected": "<one of: campaign | pattern | universal | unclear>",
      "scope_signals": "<words or context that led to this scope classification>",
      "unknown_terms": ["<term1>", "<term2>"]
    }
  ],
  "conflicting_rules": [
    {
      "rule_indices": [<index_A>, <index_B>],
      "conflict_description": "<plain English: how these two rules might conflict>"
    }
  ],
  "unknown_terms_found": ["<term1>", "<term2>"]
}

rule_type guide:
  filter_add      — user wants to ADD an inclusion criterion (new targeting filter)
  exclusion_add   — user wants to ADD an exclusion or suppression
  lookback_days   — user wants to change a lookback window (specify days)
  population_note — user wants to restrict or clarify the target population
  general         — any other correction that does not fit the above

scope_detected guide:
  campaign  — signals: "this campaign", "this one", "here", "for this run"
  pattern   — signals: "all X campaigns", "whenever", "every time I run", "always for Y type"
  universal — signals: "always", "never", "all campaigns", "every campaign", "company standard"
  unclear   — scope cannot be determined from the text alone
"""


class FeedbackAgent(BaseAgent):
    """Pillar 6 Worker — correction interpretation and business rule extraction.

    Overrides subscribe() to accept FeedbackInput instead of UniversalJSONSpec.
    Interactive pipeline: drives console dialog for clarifications and validation.
    Never raises to the orchestrator; wraps all failures in FeedbackOutput(success=False).
    """

    WORKER_ID = "feedback_v1"
    HANDLED_INTENTS: frozenset[str] = frozenset()  # feedback is invoked explicitly, not intent-routed
    INPUT_SCHEMA = FeedbackInput
    OUTPUT_SCHEMA = FeedbackOutput

    def __init__(self) -> None:
        self._api_key: Optional[str] = os.getenv("FUELIX_API_KEY")
        self._model: str = os.getenv("FUELIX_MODEL", "claude-sonnet-4")
        self._input: Optional[FeedbackInput] = None
        self._registry = BusinessRulesRegistry(_RULES_PATH)
        self._knowledge_ctx: Optional["KnowledgeContext"] = None

    # ------------------------------------------------------------------
    # Injection points
    # ------------------------------------------------------------------

    def set_knowledge_context(self, ctx: "KnowledgeContext") -> None:
        """Bind the centralised KnowledgeContext built at startup."""
        self._knowledge_ctx = ctx

    def set_session_context(self, context: str) -> None:
        pass

    def set_runtime_schema(self, schema_str: str) -> None:
        pass

    # ------------------------------------------------------------------
    # BaseAgent contract
    # ------------------------------------------------------------------

    def subscribe(self, spec: FeedbackInput) -> None:  # type: ignore[override]
        """Accept the FeedbackInput for this correction cycle."""
        self._input = spec

    def execute(self) -> FeedbackOutput:
        """Run the full correction pipeline. Returns FeedbackOutput; never raises."""
        if self._input is None:
            return self._error_output("subscribe() must be called before execute()")
        try:
            return self._run_pipeline(self._input)
        except KeyboardInterrupt:
            return self._error_output("Session interrupted by user.")
        except Exception as exc:
            return self._error_output(str(exc)[:400])

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------

    def _run_pipeline(self, inp: FeedbackInput) -> FeedbackOutput:
        # Stage 1 — acknowledge
        self._stage1_acknowledge(inp)

        # Stage 2 — LLM interpretation
        interpretation = self._stage2_interpret(inp)
        if interpretation is None:
            # LLM interpretation failed — save the raw correction verbatim so feedback
            # is never silently lost.  The rule goes in as a general/universal rule so
            # it surfaces in optimization_context on future ad-hoc queries.
            fallback_rule = self._make_verbatim_rule(inp)
            self._registry.add_rule(fallback_rule)
            print(
                "\n  Your feedback has been saved and will be applied to future queries.\n"
                "  (Automated interpretation was unavailable; the full text has been stored.)"
            )
            return FeedbackOutput(
                rules_extracted=[fallback_rule],
                rules_confirmed=[fallback_rule],
                rules_pending=[],
                new_glossary_terms=[],
                interpretation_summary="Feedback saved verbatim (LLM interpretation unavailable).",
                success=True,
            )

        rules_raw: list[dict] = interpretation.get("rules", [])
        unknown_terms: list[str] = interpretation.get("unknown_terms_found", [])

        if not rules_raw:
            return self._error_output("No rules could be extracted from the correction.")

        # Stage 3 — resolve unknown terms
        if unknown_terms:
            self._stage3_resolve_unknown_terms(unknown_terms)

        # Stage 4 — clarification loop
        rules_raw = self._stage4_clarify(rules_raw, inp)

        # Stage 5 — compound corrections
        if len(rules_raw) > 1:
            self._stage5_compound_summary(
                rules_raw, interpretation.get("conflicting_rules", [])
            )

        # Stage 6 — scope classification
        rules_raw = self._stage6_classify_scope(rules_raw, inp)

        # Stage 7 — user validation
        confirmed_rules, pending_rules = self._stage7_validate(rules_raw, inp)

        # Stage 8 — save confirmed rules
        new_terms = self._stage8_save(confirmed_rules)

        return FeedbackOutput(
            rules_extracted=[self._dict_to_rule(r, inp) for r in rules_raw],
            rules_confirmed=confirmed_rules,
            rules_pending=pending_rules,
            new_glossary_terms=new_terms,
            interpretation_summary=self._build_summary(confirmed_rules, pending_rules),
            success=True,
        )

    # ------------------------------------------------------------------
    # Stage 1: Acknowledge
    # ------------------------------------------------------------------

    def _stage1_acknowledge(self, inp: FeedbackInput) -> None:
        ThoughtDisplay.feedback_acknowledging(inp.campaign_name, inp.raw_correction)

    # ------------------------------------------------------------------
    # Stage 2: LLM interpretation
    # ------------------------------------------------------------------

    def _stage2_interpret(self, inp: FeedbackInput) -> Optional[dict]:
        cached_ctx = self._build_cached_context(inp)
        dynamic_ctx = self._build_dynamic_context(inp)

        system_blocks = [
            {
                "type": "text",
                "text": _FEEDBACK_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        user_content = [
            {
                "type": "text",
                "text": cached_ctx,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": dynamic_ctx + "\n\n" + _INTERPRETATION_INSTRUCTIONS,
            },
        ]

        try:
            raw = self._call_with_caching(
                system_blocks,
                user_content,
                thinking={"type": "enabled", "budget_tokens": 6000},
            )
            return self._extract_json(raw)
        except Exception:
            return None

    def _build_cached_context(self, inp: FeedbackInput) -> str:
        """Build the stable knowledge context cached with each correction call.

        When KnowledgeContext is available (injected at startup), uses its
        pre-built feedback_context string directly.  Falls back to reading the
        individual files for backward compatibility.
        """
        if self._knowledge_ctx is not None:
            return (
                "=== KNOWLEDGE BASE CONTEXT ===\n\n"
                + self._knowledge_ctx.feedback_context
            )

        # Fallback: build from individual files (no KnowledgeContext available)
        parts: list[str] = ["=== KNOWLEDGE BASE CONTEXT ===\n\n"]

        # Glossary
        glossary = self._load_json_safe(_GLOSSARY_PATH)
        if glossary:
            parts.append("--- Glossary (team-defined terms) ---\n")
            parts.append(json.dumps(glossary, indent=2, ensure_ascii=False)[:4000])
            parts.append("\n\n")

        # GOLD tier insights + relevant campaign summaries
        knowledge = self._load_json_safe(_KNOWLEDGE_INDEX_PATH)
        if knowledge:
            gold_insights = knowledge.get("gold_insights", {})
            if gold_insights:
                parts.append("--- GOLD Tier Insights ---\n")
                parts.append(json.dumps(gold_insights, indent=2, ensure_ascii=False)[:3000])
                parts.append("\n\n")

            campaigns = knowledge.get("campaigns", [])
            relevant = [
                c for c in campaigns
                if c.get("tier") == "GOLD"
                and (
                    c.get("camp_id", "").upper() == inp.campaign_code.upper()
                    or c.get("medium", "").upper() == inp.medium.upper()
                )
            ][:5]
            if relevant:
                parts.append("--- ACC Summaries for Relevant Campaigns ---\n")
                for camp in relevant:
                    acc = camp.get("acc_summaries") or {}
                    ts = (acc.get("targeting_summary") or "")[:400]
                    ss = (acc.get("segment_summary") or "")[:200]
                    parts.append(
                        f"Campaign: {camp.get('campaign_name')}\n"
                        f"  Targeting: {ts}\n"
                        f"  Segments:  {ss}\n\n"
                    )

        # Existing business rules
        existing = self._load_json_safe(_RULES_PATH)
        if existing and existing.get("rules"):
            parts.append("--- Existing Verified Business Rules ---\n")
            for rule in existing["rules"][:20]:
                parts.append(
                    f"- [{rule.get('scope', '?').upper()}] "
                    f"{rule.get('rule_description', '')} "
                    f"(type: {rule.get('rule_type', '?')})\n"
                )
            parts.append("\n")

        # Adobe schema (compact: key table names only)
        adobe = self._load_json_safe(_ADOBE_SCHEMA_PATH)
        if adobe:
            views = adobe.get("views", {})
            if isinstance(views, dict) and views:
                parts.append("--- Adobe Schema Views (key tables) ---\n")
                key_tables = [
                    k for k in views
                    if any(t in k for t in ("mob_mobility", "customer_profl", "model_score"))
                ]
                for name in key_tables[:6]:
                    view_data = views[name]
                    cols = [c.get("name", "") for c in (view_data.get("columns") or [])[:20]]
                    if cols:
                        parts.append(f"  {name}: {', '.join(cols)}\n")
                parts.append("\n")

        return "".join(parts)

    def _build_dynamic_context(self, inp: FeedbackInput) -> str:
        parts: list[str] = ["=== CURRENT EXECUTION CONTEXT ===\n\n"]
        parts.append(
            f"Campaign:          {inp.campaign_name}\n"
            f"Campaign Code:     {inp.campaign_code}\n"
            f"Medium:            {inp.medium}\n"
            f"Cadence:           {inp.cadence}\n"
            f"Campaign Purpose:  {inp.campaign_purpose}\n"
            f"Knowledge Tier:    {inp.knowledge_tier}\n\n"
        )

        if inp.execution_context:
            parts.append("--- What Was Done This Run ---\n")
            for key, value in inp.execution_context.items():
                parts.append(f"  {key}: {value}\n")
            parts.append("\n")

        if inp.existing_rules:
            parts.append("--- Business Rules Applied This Run ---\n")
            for rule in inp.existing_rules[:10]:
                parts.append(f"  - {rule.get('rule_description', str(rule))}\n")
            parts.append("\n")

        parts.append(f"=== USER CORRECTION ===\n\n\"{inp.raw_correction}\"\n\n")
        return "".join(parts)

    # ------------------------------------------------------------------
    # Stage 3: Resolve unknown terms
    # ------------------------------------------------------------------

    def _stage3_resolve_unknown_terms(self, unknown_terms: list[str]) -> None:
        glossary = self._load_json_safe(_GLOSSARY_PATH) or {}
        user_terms: dict = glossary.setdefault("user_defined_terms", {})
        existing_lower = {k.lower() for k in user_terms.keys()}
        updated = False

        for term in unknown_terms:
            if term.lower() in existing_lower:
                continue

            print(
                f"\n  I want to make sure I understand your feedback correctly.\n"
                f"\n  You used the term '{term}' and I don't have a definition\n"
                f"  for this in my knowledge base yet.\n"
                f"\n  Could you help me understand what it means in this context?\n"
                f"  Once you tell me, I'll remember it for future use.\n"
                f"\n  (Press Enter to skip)\n"
            )
            definition = input("  Definition: ").strip()
            if not definition:
                continue

            user_terms[term] = {
                "definition": definition,
                "source": "user_feedback",
                "added_at": datetime.now(tz=timezone.utc).isoformat(),
            }
            existing_lower.add(term.lower())
            updated = True
            print(f"\n  Got it. I've added '{term}' to the knowledge base.")

        if updated:
            self._write_glossary_safe(glossary)

    # ------------------------------------------------------------------
    # Stage 4: Clarification loop
    # ------------------------------------------------------------------

    def _stage4_clarify(
        self, rules_raw: list[dict], inp: FeedbackInput
    ) -> list[dict]:
        updated = list(rules_raw)
        for i, rule in enumerate(updated):
            if not rule.get("needs_clarification"):
                continue

            question = rule.get("clarifying_question", "Could you clarify this correction?")
            raw_text = rule.get("raw_text", inp.raw_correction)

            for attempt in range(3):
                print(
                    f"\n  I want to make sure I get this right.\n"
                    f"\n  You mentioned: '{raw_text}'\n"
                    f"\n  {question}\n"
                    f"\n  (Type 'skip' to come back to this later)\n"
                )
                answer = input("  Your answer: ").strip()

                if answer.lower() == "skip":
                    updated[i] = {**rule, "_skipped": True}
                    print("\n  No problem. I've flagged this for later.")
                    break

                if answer:
                    clarified = self._reclarify_rule(rule, answer, inp)
                    if clarified:
                        updated[i] = {
                            **clarified,
                            "needs_clarification": False,
                            "clarification_rounds": attempt + 1,
                        }
                    else:
                        updated[i] = {
                            **rule,
                            "needs_clarification": False,
                            "clarification_rounds": attempt + 1,
                            "understood_as": (
                                f"{rule.get('understood_as', '')} "
                                f"(clarified: {answer})"
                            ).strip(),
                        }
                    break

        return updated

    def _reclarify_rule(
        self, rule: dict, clarification: str, inp: FeedbackInput
    ) -> Optional[dict]:
        prompt = (
            f"Re-interpret this business rule with the additional clarification.\n\n"
            f"Original text: \"{rule.get('raw_text', '')}\"\n"
            f"Original interpretation: \"{rule.get('understood_as', '')}\"\n"
            f"User clarification: \"{clarification}\"\n"
            f"Campaign: {inp.campaign_name} | Medium: {inp.medium} | Cadence: {inp.cadence}\n\n"
            "Output a single rule object JSON (same structure as in the rules array). "
            "Output JSON only."
        )
        try:
            system = [{"type": "text", "text": _FEEDBACK_SYSTEM}]
            user = [{"type": "text", "text": prompt}]
            raw = self._call_with_caching(system, user)
            extracted = self._extract_json(raw)
            if extracted:
                if "raw_text" in extracted:
                    return extracted
                if "rules" in extracted and extracted["rules"]:
                    return extracted["rules"][0]
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Stage 5: Compound corrections
    # ------------------------------------------------------------------

    def _stage5_compound_summary(
        self, rules_raw: list[dict], conflicts: list[dict]
    ) -> None:
        n = len(rules_raw)
        print(
            f"\n  I found {n} separate corrections in your feedback.\n"
            f"  Let me confirm each one with you."
        )

        for conflict in conflicts:
            indices = conflict.get("rule_indices", [])
            desc = conflict.get("conflict_description", "")
            if len(indices) >= 2 and indices[0] < len(rules_raw) and indices[1] < len(rules_raw):
                r1 = rules_raw[indices[0]].get("raw_text", f"Rule {indices[0]+1}")
                r2 = rules_raw[indices[1]].get("raw_text", f"Rule {indices[1]+1}")
                print(
                    f"\n  I noticed these two corrections might overlap:\n"
                    f"  '{r1}' and '{r2}'\n"
                    f"\n  {desc}\n"
                    f"\n  Could you help me understand how these work together?"
                )
                input("  Your answer: ").strip()

    # ------------------------------------------------------------------
    # Stage 6: Scope classification
    # ------------------------------------------------------------------

    def _stage6_classify_scope(
        self, rules_raw: list[dict], inp: FeedbackInput
    ) -> list[dict]:
        updated = list(rules_raw)
        for i, rule in enumerate(updated):
            if rule.get("scope_detected", "unclear") != "unclear":
                continue
            if rule.get("_skipped"):
                continue

            raw_text = rule.get("raw_text", inp.raw_correction)
            print(
                f"\n  For this correction:\n    '{raw_text}'\n"
                f"\n  Should this apply to:\n"
                f"    1. This campaign only\n"
                f"    2. All similar campaigns\n"
                f"    3. Every campaign we run\n"
                f"\n  Which best describes what you meant? (1/2/3)"
            )
            choice = input("\n  Your choice: ").strip()

            if choice == "1":
                updated[i] = {**rule, "scope_detected": "campaign"}
            elif choice == "2":
                print(
                    "\n  Could you describe what makes a campaign 'similar'?\n"
                    "  For example: 'all email campaigns' or 'all Koodo campaigns'\n"
                )
                pattern = input("  Pattern description: ").strip()
                updated[i] = {
                    **rule,
                    "scope_detected": "pattern",
                    "pattern_description": pattern or rule.get("scope_signals", ""),
                }
            else:
                updated[i] = {**rule, "scope_detected": "universal"}

        return updated

    # ------------------------------------------------------------------
    # Stage 7: Validate with user
    # ------------------------------------------------------------------

    def _stage7_validate(
        self, rules_raw: list[dict], inp: FeedbackInput
    ) -> tuple[list[BusinessRule], list[BusinessRule]]:
        confirmed: list[BusinessRule] = []
        pending: list[BusinessRule] = []
        total = len(rules_raw)
        sep = "─" * 53

        for i, rule_dict in enumerate(rules_raw):
            if rule_dict.get("_skipped"):
                pending.append(self._dict_to_rule(rule_dict, inp))
                continue

            scope = rule_dict.get("scope_detected", "campaign")
            scope_label = self._scope_label(scope, rule_dict, inp)

            print(
                f"\n  Here's what I understood. Please confirm:\n"
                f"\n  {sep}"
                f"\n  Rule {i + 1} of {total}\n"
                f"\n  You said:\n    '{rule_dict.get('raw_text', inp.raw_correction)}'\n"
                f"\n  I understood this as:\n    '{rule_dict.get('understood_as', '')}'\n"
                f"\n  This will apply to:\n    '{scope_label}'\n"
                f"\n  Starting from:\n    Next execution onwards\n"
                f"\n  Is this correct?"
                f"\n    Y — Save this rule"
                f"\n    N — That's not quite right"
                f"\n    E — Let me edit the description"
                f"\n  {sep}\n"
            )

            for attempt in range(3):
                answer = input("  Y / N / E: ").strip().upper()

                if answer == "Y":
                    confirmed.append(self._dict_to_rule(rule_dict, inp))
                    break

                elif answer == "E":
                    print("\n  Please type the corrected description:\n")
                    edited = input("  New description: ").strip()
                    if edited:
                        rule_dict = {**rule_dict, "understood_as": edited}
                    confirmed.append(self._dict_to_rule(rule_dict, inp))
                    break

                elif answer == "N":
                    if attempt < 2:
                        print("\n  What was wrong with my interpretation?\n")
                        feedback = input("  What I missed: ").strip()
                        if feedback:
                            rule_dict = {
                                **rule_dict,
                                "understood_as": (
                                    f"{rule_dict.get('understood_as', '')} "
                                    f"(corrected: {feedback})"
                                ).strip(),
                            }
                        print(
                            f"\n  Updated:\n    '{rule_dict.get('understood_as', '')}'\n"
                            f"\n  Is this correct now? (Y/N/E)\n"
                        )
                    else:
                        pending.append(self._dict_to_rule(rule_dict, inp))
                        print("\n  No problem. I'll leave this one for now.")
                        break

        return confirmed, pending

    # ------------------------------------------------------------------
    # Stage 8: Save confirmed rules
    # ------------------------------------------------------------------

    def _stage8_save(self, confirmed_rules: list[BusinessRule]) -> list[dict]:
        for rule in confirmed_rules:
            self._registry.add_rule(rule)

        if confirmed_rules:
            n = len(confirmed_rules)
            print(
                f"\n  Saved! I'll apply {n} rule(s) automatically from now on.\n"
                f"  You'll see them mentioned in my thought process whenever they're being used."
            )

        return []  # new_glossary_terms — terms are already written in stage 3

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_verbatim_rule(self, inp: FeedbackInput) -> BusinessRule:
        """Create a BusinessRule directly from the raw correction text.

        Used as a fallback when LLM interpretation (Stage 2) is unavailable.
        Scope defaults to 'universal' for AD_HOC corrections so the rule is
        applied to all future ad-hoc queries via optimization_context.
        """
        scope = "campaign" if inp.campaign_code not in ("", "AD_HOC") else "universal"
        priority_map = {"campaign": 3, "pattern": 2, "universal": 1}
        return BusinessRule(
            rule_id=str(uuid.uuid4()),
            created_at=datetime.now(tz=timezone.utc).isoformat(),
            verified_by="hitl_no_response",
            raw_correction=inp.raw_correction,
            rule_description=inp.raw_correction,
            rule_type="general",
            structured_value={
                "note": inp.raw_correction,
                "description": "Verbatim user correction from HITL NO response",
            },
            scope=scope,
            campaign_code=inp.campaign_code if scope == "campaign" else None,
            campaign_name=inp.campaign_name if scope == "campaign" else None,
            medium=inp.medium if scope in ("campaign", "pattern") else None,
            cadence=inp.cadence if scope in ("campaign", "pattern") else None,
            priority=priority_map.get(scope, 1),
            applies_to_future=True,
            source="hitl_feedback_verbatim",
            confidence=0.7,
        )

    def _dict_to_rule(self, rule_dict: dict, inp: FeedbackInput) -> BusinessRule:
        scope = rule_dict.get("scope_detected", "campaign")
        priority_map = {"campaign": 3, "pattern": 2, "universal": 1}
        return BusinessRule(
            rule_id=str(uuid.uuid4()),
            created_at=datetime.now(tz=timezone.utc).isoformat(),
            verified_by="hitl_no_response",
            raw_correction=rule_dict.get("raw_text", inp.raw_correction),
            rule_description=rule_dict.get("understood_as", inp.raw_correction),
            rule_type=rule_dict.get("rule_type", "general"),
            structured_value=rule_dict.get("structured_value", {}),
            scope=scope,
            campaign_code=inp.campaign_code if scope == "campaign" else None,
            campaign_name=inp.campaign_name if scope == "campaign" else None,
            medium=inp.medium if scope in ("campaign", "pattern") else None,
            cadence=inp.cadence if scope in ("campaign", "pattern") else None,
            pattern_description=rule_dict.get("pattern_description"),
            pattern_match_logic=rule_dict.get("scope_signals"),
            confidence=rule_dict.get("confidence", 1.0),
            source="hitl_feedback",
            clarification_rounds=rule_dict.get("clarification_rounds", 0),
            applies_to_future=True,
            overrides_acc_summary=False,
            priority=priority_map.get(scope, 1),
        )

    @staticmethod
    def _scope_label(scope: str, rule_dict: dict, inp: FeedbackInput) -> str:
        if scope == "campaign":
            return f"This campaign only ({inp.campaign_name})"
        elif scope == "pattern":
            pat = rule_dict.get("pattern_description") or rule_dict.get("scope_signals", "similar campaigns")
            return f"All campaigns matching: {pat}"
        elif scope == "universal":
            return "Every campaign we run"
        return f"This campaign only ({inp.campaign_name})"

    @staticmethod
    def _build_summary(
        confirmed: list[BusinessRule], pending: list[BusinessRule]
    ) -> str:
        parts: list[str] = []
        if confirmed:
            parts.append(f"{len(confirmed)} rule(s) saved")
        if pending:
            parts.append(f"{len(pending)} rule(s) pending clarification")
        return "; ".join(parts) + "." if parts else "No rules extracted."

    # ------------------------------------------------------------------
    # API callers
    # ------------------------------------------------------------------

    def _call_with_caching(
        self,
        system_blocks: list[dict],
        user_content: list[dict],
        thinking: Optional[dict] = None,
    ) -> str:
        """Call Fuel iX with prompt-cached knowledge context.

        Pass thinking={"type": "enabled", "budget_tokens": N} to enable extended
        thinking for ambiguous correction interpretation.  Requires
        FUELIX_EXTENDED_THINKING=1 and Fuel iX support.  Temperature is forced to 1.
        """
        use_thinking = thinking is not None and bool(os.getenv("FUELIX_EXTENDED_THINKING"))
        beta = (
            "prompt-caching-2024-07-31,interleaved-thinking-2025-05-14"
            if use_thinking
            else "prompt-caching-2024-07-31"
        )
        payload: dict = {
            "model": self._model,
            "system": system_blocks,
            "messages": [{"role": "user", "content": user_content}],
            "max_tokens": 2048,
            "temperature": 1 if use_thinking else 0,
        }
        if use_thinking:
            payload["thinking"] = thinking
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "anthropic-beta": beta,
        }
        resp = requests.post(
            f"{_FUELIX_BASE}/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        return self._extract_text(resp.json())

    @staticmethod
    def _extract_text(resp_json: dict) -> str:
        choices = resp_json.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            content = msg.get("content") or ""
            if isinstance(content, list):
                return "".join(
                    block.get("text", "")
                    for block in content
                    if block.get("type") == "text"
                ).strip()
            return str(content).strip()
        content = resp_json.get("content") or []
        if isinstance(content, list):
            return "".join(
                block.get("text", "")
                for block in content
                if block.get("type") == "text"
            ).strip()
        return ""

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        """Extract and parse the first JSON object from an LLM response."""
        text = text.strip()
        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start: i + 1])
                    except json.JSONDecodeError:
                        return None
        return None

    # ------------------------------------------------------------------
    # File helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _load_json_safe(path: Path) -> Optional[dict]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_glossary_safe(self, glossary: dict) -> None:
        try:
            tmp_fd, tmp_name = tempfile.mkstemp(
                dir=str(_GLOSSARY_PATH.parent), suffix=".tmp"
            )
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(glossary, fh, indent=2, ensure_ascii=False)
            os.replace(tmp_name, str(_GLOSSARY_PATH))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Error output
    # ------------------------------------------------------------------

    @staticmethod
    def _error_output(reason: str) -> FeedbackOutput:
        return FeedbackOutput(
            rules_extracted=[],
            rules_confirmed=[],
            rules_pending=[],
            new_glossary_terms=[],
            interpretation_summary=f"Feedback processing failed: {reason}",
            success=False,
        )
