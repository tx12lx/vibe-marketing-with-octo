"""
Vibe OCTO Feedback — FeedbackAgent Worker
Worker ID: feedback_v1

Interprets natural language corrections submitted from the web/Slack HITL flow
and extracts them as structured business rules, grounded in the knowledge
layer (real schema, glossary, and existing confirmed rules) rather than any
hardcoded taxonomy.

A rule is never saved by this agent directly -- it returns rules_confirmed /
rules_pending in FeedbackOutput, and the caller (api/web_app.py) persists a
rule to the knowledge layer only after the submitter has explicitly confirmed
the interpretation shown to them. That confirm-before-save step, plus the
maker-checker gate on 'pattern'/'universal'-scoped rules (see
knowledge/context.py's add_rule()), is what keeps a correction from silently
governing every future user's results on one person's word alone.

Pipeline stages:
  1. Interpret        — LLM analysis grounded in the knowledge base
  2. Clarify           — ask one targeted question when a rule is too
                         ambiguous to save as stated
  3. Scope             — classify each rule as campaign | pattern | universal
  4. Structure         — package each rule as a BusinessRule for the caller
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
from typing import Optional

from dotenv import load_dotenv
from core.ai_client import ask_ai
from core.json_extract import extract_json

_AGENTS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _AGENTS_DIR.parent

for _p in [str(_ROOT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

load_dotenv(_ROOT_DIR / ".env")

from pydantic_schemas import (  # noqa: E402
    BusinessRule,
    FeedbackInput,
    FeedbackOutput,
    SemanticFailureLog,
)
from core.base_agent import BaseAgent  # noqa: E402

_FAILURE_LOG_PATH = _ROOT_DIR / "semantic_failure_log.json"

# ---------------------------------------------------------------------------
# System prompt — identity anchor
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
3. Check it against EXISTING CONFIRMED BUSINESS RULES in the knowledge base context above --
   if this rule would contradict or reverse one of those (e.g. an existing rule says to
   exclude something and this one would stop excluding it, or vice versa), do NOT propose it
   as a normal new rule. Instead set contradicts_existing_rule to true, quote the exact
   existing rule text in contradicting_rule_text, and phrase clarifying_question as a direct
   question asking the user to confirm they mean to override that existing rule (quoting it),
   rather than a general ambiguity question. This check is independent of confidence -- a rule
   can be a clear, confident interpretation of the user's words and still contradict something
   already confirmed; both cases must be flagged.
4. Flag any other ambiguities requiring clarification
5. Classify scope from available signals
6. List any terms not found in the provided knowledge base

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
      "clarifying_question": "<targeted question if needs_clarification or contradicts_existing_rule is true, else empty string>",
      "contradicts_existing_rule": <true | false>,
      "contradicting_rule_text": "<exact text of the existing confirmed rule this contradicts, else empty string>",
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
    """Correction interpretation and business rule extraction.

    Overrides subscribe() to accept FeedbackInput instead of the usual agent
    input. Never raises to the orchestrator; wraps all failures in
    FeedbackOutput(success=False).
    """

    WORKER_ID = "feedback_v1"
    HANDLED_INTENTS: frozenset[str] = frozenset()  # feedback is invoked explicitly, not intent-routed
    INPUT_SCHEMA = FeedbackInput
    OUTPUT_SCHEMA = FeedbackOutput

    def __init__(self) -> None:
        self._input: Optional[FeedbackInput] = None
        self._knowledge_ctx: Optional["KnowledgeContext"] = None
        self._pending_clarification: str = ""
        # Set alongside _pending_clarification only when stage 2 blocked a rule for
        # contradicting an existing confirmed rule -- lets _run_pipeline's early return
        # tell the caller which existing rule text triggered it (see FeedbackOutput.
        # contradiction_existing_rule_text and _resolve_pending_contradiction() below).
        self._pending_contradiction_rule_text: str = ""

    # ------------------------------------------------------------------
    # Injection points
    # ------------------------------------------------------------------

    def set_knowledge_context(self, ctx: "KnowledgeContext") -> None:
        """Bind the centralised KnowledgeContext built at startup."""
        self._knowledge_ctx = ctx

    def set_session_context(self, context: str) -> None:
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
        except Exception as exc:
            return self._error_output(str(exc)[:400])

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------

    def _run_pipeline(self, inp: FeedbackInput) -> FeedbackOutput:
        self._write_failure_log_entry(inp)

        # The previous turn on this session was blocked by a contradiction with an
        # existing confirmed rule -- inp.raw_correction here is the user's answer to
        # "do you want this to override that rule?", not a fresh correction. Resolve it
        # directly rather than running it through stage 1 again, which would just
        # re-extract the same rule and flag the same contradiction a second time.
        if inp.pending_contradiction_text:
            return self._resolve_pending_contradiction(inp)

        # Stage 1 — LLM interpretation
        interpretation = self._stage1_interpret(inp)
        if interpretation is None:
            fallback_rule = self._make_verbatim_rule(inp)
            return FeedbackOutput(
                rules_extracted=[fallback_rule],
                rules_confirmed=[fallback_rule],
                rules_pending=[],
                new_glossary_terms=[],
                interpretation_summary="I wasn't able to fully interpret this correction automatically. I've recorded it exactly as you wrote it and will use it going forward.",
                success=True,
            )

        rules_raw: list[dict] = interpretation.get("rules", [])
        if not rules_raw:
            return self._error_output("No rules could be extracted from the correction.")

        # Stage 2 — clarification: ask one targeted question when a rule is
        # too ambiguous to save as stated (skips straight through otherwise).
        rules_raw = self._stage2_clarify(rules_raw)

        if self._pending_clarification:
            question = self._pending_clarification
            contradiction_text = self._pending_contradiction_rule_text
            self._pending_clarification = ""
            self._pending_contradiction_rule_text = ""
            return FeedbackOutput(
                rules_extracted=[],
                rules_confirmed=[],
                rules_pending=[],
                new_glossary_terms=[],
                interpretation_summary="",
                success=False,
                clarifying_question=question,
                contradiction_existing_rule_text=contradiction_text,
            )

        # Stage 3 — scope classification
        rules_raw = self._stage3_classify_scope(rules_raw, inp)

        # Stage 4 — package into BusinessRule objects. Rules are NOT saved here --
        # the caller persists rules_confirmed only after the submitter explicitly
        # confirms the interpretation shown to them (see api/web_app.py).
        confirmed: list[BusinessRule] = []
        pending: list[BusinessRule] = []
        for rule_dict in rules_raw:
            (pending if rule_dict.get("_skipped") else confirmed).append(
                self._dict_to_rule(rule_dict, inp)
            )

        return FeedbackOutput(
            rules_extracted=[self._dict_to_rule(r, inp) for r in rules_raw],
            rules_confirmed=confirmed,
            rules_pending=pending,
            new_glossary_terms=[],
            interpretation_summary=self._build_summary(confirmed, pending),
            success=True,
        )

    # ------------------------------------------------------------------
    # Stage 1: LLM interpretation
    # ------------------------------------------------------------------

    def _stage1_interpret(self, inp: FeedbackInput) -> Optional[dict]:
        cached_ctx = self._build_cached_context()
        dynamic_ctx = self._build_dynamic_context(inp)
        prompt = cached_ctx + "\n\n" + dynamic_ctx + "\n\n" + _INTERPRETATION_INSTRUCTIONS
        try:
            raw = self._call_with_caching(prompt)
            return self._extract_json(raw)
        except Exception:
            return None

    def _build_cached_context(self) -> str:
        """Build the knowledge context passed with each correction call.

        Uses KnowledgeContext.feedback_context (real schema/glossary/business
        rules from the knowledge layer) so interpretation is grounded rather
        than a guess -- there is no local-file fallback because the knowledge
        layer is always available in production (bound at startup).
        """
        if self._knowledge_ctx is None:
            return "=== KNOWLEDGE BASE CONTEXT ===\n\n(knowledge layer not available for this call)"
        return "=== KNOWLEDGE BASE CONTEXT ===\n\n" + self._knowledge_ctx.feedback_context

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
    # Stage 2: Clarification
    #
    # Only asks the user something when the model itself flagged low
    # confidence -- otherwise the rule is accepted as interpreted, since there
    # is no terminal session to run a multi-turn clarification dialog in.
    # ------------------------------------------------------------------

    def _stage2_clarify(self, rules_raw: list[dict]) -> list[dict]:
        updated = list(rules_raw)
        for i, rule in enumerate(updated):
            # A contradiction with an already-confirmed rule is forced to clarification
            # regardless of confidence -- unlike plain ambiguity, high confidence in the
            # interpretation says nothing about whether the user actually meant to reverse
            # something already confirmed, so it must never silently pass through into a
            # second, contradicting rule the way it did before this check existed.
            if rule.get("contradicts_existing_rule") and not self._pending_clarification:
                existing = rule.get("contradicting_rule_text", "")
                question = rule.get("clarifying_question") or (
                    f"This looks like it conflicts with a rule you already confirmed: "
                    f"\"{existing}\". Do you want this new correction to replace that rule?"
                )
                self._pending_clarification = question
                self._pending_contradiction_rule_text = existing
                updated[i] = {**rule, "_skipped": True}
                continue

            if not rule.get("needs_clarification"):
                continue
            question = rule.get("clarifying_question", "")
            confidence = rule.get("confidence", 1.0)
            if question and confidence < 0.5 and not self._pending_clarification:
                self._pending_clarification = question
                updated[i] = {**rule, "_skipped": True}
            else:
                updated[i] = {**rule, "needs_clarification": False}
        return updated

    # ------------------------------------------------------------------
    # Stage 3: Scope classification — AI-driven reasoning
    # ------------------------------------------------------------------

    def _stage3_classify_scope(
        self, rules_raw: list[dict], inp: FeedbackInput
    ) -> list[dict]:
        updated = list(rules_raw)
        for i, rule in enumerate(updated):
            if rule.get("scope_detected", "unclear") != "unclear":
                # Already classified by Stage 1 with sufficient confidence — keep it.
                if rule.get("scope_detected") == "pattern" and not rule.get("pattern_description"):
                    updated[i] = {
                        **rule,
                        "pattern_description": rule.get("scope_signals") or rule.get("understood_as", ""),
                    }
                continue
            if rule.get("_skipped"):
                continue

            ai_result = self._ai_classify_scope(rule, inp)
            updated[i] = {
                **rule,
                "scope_detected": ai_result.get("scope", "universal"),
                "pattern_description": ai_result.get("pattern_description") or rule.get("scope_signals", ""),
            }
        return updated

    def _ai_classify_scope(self, rule: dict, inp: FeedbackInput) -> dict:
        """Ask the LLM to determine the scope of a rule from its text and context."""
        prompt = (
            "Determine the scope of this business rule based on its text and context.\n\n"
            f"Rule text: \"{rule.get('raw_text', inp.raw_correction)}\"\n"
            f"AI interpretation: \"{rule.get('understood_as', '')}\"\n"
            f"Original query context: \"{inp.raw_input_prompt}\"\n"
            f"Was the original query about a specific campaign: {inp.campaign_code not in ('', 'AD_HOC')}\n"
            f"Campaign code (if any): {inp.campaign_code}\n\n"
            "Scope definitions:\n"
            "  campaign  — applies only to this specific campaign (signals: 'this campaign', 'this run', 'here')\n"
            "  pattern   — applies to a type of campaign or context (signals: 'add mob campaigns', 'whenever', 'all X')\n"
            "  universal — applies to every query always (signals: 'always', 'never', 'all campaigns', a definition)\n\n"
            "Important: if the original query had no campaign context (campaign code is AD_HOC or empty), "
            "the scope CANNOT be 'campaign' — choose 'pattern' or 'universal' instead.\n"
            "If the rule is defining a term or establishing a business concept, default to 'universal'.\n\n"
            'Output JSON only:\n'
            '{"scope": "<campaign|pattern|universal>", "confidence": <0.0-1.0>, '
            '"pattern_description": "<one phrase describing when this applies, or empty string>", '
            '"reasoning": "<one sentence explaining the scope choice>"}'
        )
        try:
            raw = self._call_with_caching(prompt)
            result = self._extract_json(raw)
            if result and "scope" in result:
                return result
        except Exception:
            pass
        # Fallback: if no campaign context, default universal; else use scope_signals
        scope = "universal" if inp.campaign_code in ("", "AD_HOC") else "campaign"
        return {"scope": scope, "confidence": 0.5, "pattern_description": "", "reasoning": "fallback"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _read_affirmation(reply: str) -> Optional[bool]:
        """Deterministically read a plain yes/no opening (case-insensitive, allowing simple
        leading punctuation/quotes). Returns None when the reply doesn't clearly start
        either way, so the caller can fall back to an AI read for genuinely unclear text."""
        text = reply.strip().lstrip("\"'").lower()
        if re.match(r"^yes\b", text) or re.match(r"^yep\b", text) or re.match(r"^yeah\b", text):
            return True
        if re.match(r"^no\b", text) or re.match(r"^nope\b", text):
            return False
        return None

    def _resolve_pending_contradiction(self, inp: FeedbackInput) -> FeedbackOutput:
        """Resolve the user's answer to "do you want this to override that existing rule?"
        instead of re-running the full interpretation pipeline on it -- which would just
        re-extract the same correction and flag the same contradiction again, trapping the
        user in a loop.

        Whether to override an already-confirmed rule is consequential enough that it
        shouldn't rest on a single non-deterministic model call: a direct "yes"/"no" opening
        is read deterministically first (this covers the overwhelming majority of real
        replies to a yes/no question), and the model is only asked when the reply doesn't
        clearly start either way.
        """
        existing_text = inp.pending_contradiction_text
        affirmed = self._read_affirmation(inp.raw_correction)
        if affirmed is None:
            check_prompt = (
                "A user was asked the following question about a correction they submitted:\n"
                f'  "This looks like it conflicts with a rule you already confirmed: '
                f'\\"{existing_text}\\". Do you want this new correction to replace that rule?"\n\n'
                f'Their reply: "{inp.raw_correction}"\n\n'
                "Does their reply affirm that they want the new correction to replace/override "
                "the existing rule? Answer with exactly one word: YES or NO."
            )
            try:
                raw = ask_ai(check_prompt, system=_FEEDBACK_SYSTEM, temperature=0, max_tokens=10)
                affirmed = raw.strip().upper().startswith("Y")
            except Exception:
                affirmed = False

        if not affirmed:
            return FeedbackOutput(
                rules_extracted=[], rules_confirmed=[], rules_pending=[],
                new_glossary_terms=[],
                interpretation_summary="Okay, I'll leave the existing rule as it is -- no changes made.",
                success=True,
            )

        rule = BusinessRule(
            rule_id=str(uuid.uuid4()),
            created_at=datetime.now(tz=timezone.utc).isoformat(),
            verified_by=inp.user_identity or "unknown",
            raw_correction=inp.raw_correction,
            rule_description=f'Supersedes an earlier rule ("{existing_text}"): {inp.raw_correction}',
            rule_type="general",
            structured_value={
                "note": inp.raw_correction,
                "description": "User-confirmed override of a previously confirmed, conflicting rule.",
            },
            scope="campaign",
            campaign_code=inp.campaign_code if inp.campaign_code not in ("", "AD_HOC") else None,
            campaign_name=inp.campaign_name if inp.campaign_code not in ("", "AD_HOC") else None,
            priority=3,
            applies_to_future=True,
            overrides_acc_summary=False,
            clarification_rounds=1,
            source="hitl_feedback_contradiction_override",
            confidence=0.9,
        )
        return FeedbackOutput(
            rules_extracted=[rule], rules_confirmed=[rule], rules_pending=[],
            new_glossary_terms=[],
            interpretation_summary=rule.rule_description,
            success=True,
        )

    def _make_verbatim_rule(self, inp: FeedbackInput) -> BusinessRule:
        """Create a BusinessRule directly from the raw correction text.

        Used as a fallback when LLM interpretation (Stage 1) is unavailable.
        Scope defaults to 'universal' for AD_HOC corrections so the rule is
        applied to all future ad-hoc queries via optimization_context.
        """
        scope = "campaign" if inp.campaign_code not in ("", "AD_HOC") else "universal"
        priority_map = {"campaign": 3, "pattern": 2, "universal": 1}
        return BusinessRule(
            rule_id=str(uuid.uuid4()),
            created_at=datetime.now(tz=timezone.utc).isoformat(),
            verified_by=inp.user_identity or "unknown",
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
            overrides_acc_summary=False,
            clarification_rounds=0,
            source="hitl_feedback_verbatim",
            confidence=0.7,
        )

    def _dict_to_rule(self, rule_dict: dict, inp: FeedbackInput) -> BusinessRule:
        scope = rule_dict.get("scope_detected", "campaign")
        priority_map = {"campaign": 3, "pattern": 2, "universal": 1}
        return BusinessRule(
            rule_id=str(uuid.uuid4()),
            created_at=datetime.now(tz=timezone.utc).isoformat(),
            verified_by=inp.user_identity or "unknown",
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
    def _build_summary(
        confirmed: list[BusinessRule], pending: list[BusinessRule]
    ) -> str:
        lines: list[str] = []

        for i, rule in enumerate(confirmed, 1):
            desc = rule.rule_description.strip()
            if desc:
                prefix = f"{i}. " if len(confirmed) + len(pending) > 1 else ""
                lines.append(f"{prefix}{desc}")

        for i, rule in enumerate(pending, len(confirmed) + 1):
            desc = rule.rule_description.strip()
            if desc:
                prefix = f"{i}. " if len(confirmed) + len(pending) > 1 else ""
                lines.append(f"{prefix}{desc} (I need a little more information to apply this one -- I'll ask you below.)")

        if lines:
            return "\n\n".join(lines)

        if confirmed or pending:
            total = len(confirmed) + len(pending)
            return f"I picked up {total} correction{'s' if total > 1 else ''} from your feedback. Does this match what you intended?"

        return "I wasn't able to extract a specific learning from that feedback. Could you try rephrasing it?"

    # ------------------------------------------------------------------
    # API callers
    # ------------------------------------------------------------------

    def _call_with_caching(self, prompt: str) -> str:
        """Ask the AI, with the knowledge context folded into the prompt.

        Note: Gemini has no equivalent to Fuel iX/Anthropic's ephemeral
        prompt-caching, so this is a plain call regardless.

        thinking_budget=0: every call this method makes asks for a fixed JSON
        shape and nothing else -- exactly the kind of straightforward
        formatting task core/ai_client.py's ask_ai() warns can get silently
        truncated by hidden "thinking" tokens eating the token budget before
        any visible output is written. Disabling it here is what makes the
        response reliably complete.
        """
        return ask_ai(prompt, system=_FEEDBACK_SYSTEM, temperature=0, max_tokens=4096, thinking_budget=0)

    @staticmethod
    def _extract_json(text: str) -> Optional[dict]:
        """Extract and parse the first JSON object from an LLM response."""
        return extract_json(text)

    # ------------------------------------------------------------------
    # Failure log — raw record of every correction, for manual review
    # ------------------------------------------------------------------

    def _write_failure_log_entry(self, inp: FeedbackInput) -> None:
        """Append a SemanticFailureLog record for manual review.

        This raw log is not currently auto-summarized -- an earlier digest
        feature that scanned it for patterns depended on a registry class
        that no longer exists, and was removed as broken rather than kept as
        a silent no-op. The log itself is real and complete; a human can read
        semantic_failure_log.json directly.
        """
        try:
            failure_type = self._infer_failure_type_from_text(inp.raw_correction)
            entry = SemanticFailureLog(
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                campaign_code=inp.campaign_code or "AD_HOC",
                worker_id="feedback_agent",
                raw_input=inp.raw_input_prompt or "",
                generated_output=json.dumps(inp.execution_context, default=str),
                correction_description=inp.raw_correction,
                inferred_failure_type=failure_type,
                glossary_gaps=[],
                intent_type="general_question" if inp.campaign_code in ("", "AD_HOC") else "sizing_request",
            )
            records: list[dict] = []
            if _FAILURE_LOG_PATH.exists():
                try:
                    raw = json.loads(_FAILURE_LOG_PATH.read_text(encoding="utf-8"))
                    if isinstance(raw, list):
                        records = raw
                except Exception:
                    pass
            records.append(entry.model_dump())
            tmp_fd, tmp_name = tempfile.mkstemp(dir=str(_FAILURE_LOG_PATH.parent), suffix=".tmp")
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    json.dump(records, fh, indent=2, ensure_ascii=False, default=str)
                os.replace(tmp_name, str(_FAILURE_LOG_PATH))
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
        except Exception:
            pass  # failure log write must never break the correction pipeline

    @staticmethod
    def _infer_failure_type_from_text(correction: str) -> str:
        lower = correction.lower()
        if "missing" in lower and "exclusion" in lower:
            return "missing_exclusion"
        if "tier" in lower or "blueprint" in lower:
            return "tier_mismatch"
        if "wrong column" in lower or ("column" in lower and "filter" not in lower):
            return "wrong_column"
        if "filter" in lower or "sql" in lower or "query" in lower:
            return "wrong_filter"
        return "general_answer"

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
