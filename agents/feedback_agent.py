"""
Vibe OCTO Feedback — FeedbackAgent Worker
Worker ID: feedback_v1

Interprets natural language corrections submitted from the web HITL flow
and extracts them as structured business rules, grounded in the knowledge
layer (real schema, glossary, and existing confirmed rules) rather than any
hardcoded taxonomy.

A rule is never saved by this agent directly -- it returns rules_confirmed /
rules_pending in FeedbackOutput, and the caller (api/web_app.py) persists a
rule to the knowledge layer only after the submitter has explicitly confirmed
the interpretation shown to them. That confirm-before-save step is what keeps
a correction from silently governing every future user's results on one
person's word alone.

Nothing is ever saved on a guess. Before a correction can be confirmed, this
agent has to be genuinely confident about four things: what's being corrected,
whether it should last forever or just apply to the current request, who/what
it applies to, and whether it conflicts with anything already confirmed. If
any of those is unclear, execute() returns exactly one plain, specific
question about that one gap instead of a rule -- the caller re-invokes this
agent with the reply added to FeedbackInput.clarification_history, and Stage 1
re-derives all four gates fresh from the ORIGINAL correction plus the full
Q&A history so far, not just the one thing that was just asked. This repeats
for as many rounds as it takes, up to _MAX_CLARIFICATION_ROUNDS -- past that,
execute() gives up honestly (FeedbackOutput.gave_up=True) rather than loop
forever or fall back to saving an unreviewed guess.

The existing knowledge base is not treated as untouchable ground truth a new
correction has to fight past -- it can be wrong, and often that's exactly why
someone is correcting it. When a new correction conflicts with something
already confirmed, this agent's job is to work that out with the person (ask,
listen, reconsider), not to defensively protect the older rule. When the
person confirms an override, the rule this agent returns carries
supersedes_rule_id so the caller retires the old rule in the same step the
new one is saved (see knowledge/context.py's add_rule()) -- the knowledge
base should never end up holding two rules that quietly contradict each other.

Pipeline stages:
  1. Interpret              — LLM analysis grounded in the knowledge base; also
                               checks for contradictions against EXISTING
                               CONFIRMED BUSINESS RULES
  2. Confidence-gate check  — ask one targeted question when any rule in the
                               batch is unclear on what/duration/scope/conflict;
                               skips straight through when all four are clear
  3. Scope                  — classify each rule as pattern | universal
  4. Structure               — package each rule as a BusinessRule for the caller
"""
from __future__ import annotations

import json
import os
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

# After this many clarification round-trips on one correction without reaching enough
# confidence to save anything, _run_pipeline gives up honestly (FeedbackOutput.gave_up)
# rather than keep the conversation open forever or fall back to a guess.
_MAX_CLARIFICATION_ROUNDS = 4

# ---------------------------------------------------------------------------
# System prompt — identity anchor
# ---------------------------------------------------------------------------

_FEEDBACK_SYSTEM = (
    "You are Vibe OCTO Feedback, a specialist in interpreting marketing analyst "
    "corrections and extracting precise, reusable business rules from them. "
    "Your role is to understand WHAT went wrong in an audience sizing run, "
    "extract each distinct correction as a structured rule, classify its scope "
    "(pattern-based or universal), and identify any unknown terms that require "
    "clarification. "
    "Never hardcode business terms or jargon. "
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

Before proposing anything as ready to save, you must be genuinely confident about all
four of these for each rule -- if any one of them is unclear, do not guess: set
needs_clarification to true and ask about that specific gap.
  (a) WHAT is actually being corrected, in plain business language
  (b) DURATION -- should this last forever, or does the person mean it just for the
      current request/conversation? Read the wording carefully: phrases like "for this
      one", "just this time", "for now", "on this occasion" mean session_only; phrases
      like "always", "every time", "from now on", "going forward", or a flat definition
      of a term, mean permanent. If genuinely neither kind of signal is present, that is
      itself a reason to ask, not a reason to default to permanent.
  (c) SCOPE -- who or what this applies to (see scope_detected guide below). "unclear"
      is not a resolvable default here either -- it must also set needs_clarification.
  (d) CONFLICT -- check this rule against EXISTING CONFIRMED BUSINESS RULES in the
      knowledge base context above. If this rule would contradict or reverse one of
      those (e.g. an existing rule says to exclude something and this one would stop
      excluding it, or vice versa), do NOT propose it as a normal new rule. Instead set
      contradicts_existing_rule to true, quote the exact existing rule's id number from
      its "[id=N]" marker in contradicting_rule_id and its text in
      contradicting_rule_text, and phrase clarifying_question as a direct question
      asking the user to confirm they mean to override that existing rule (quoting it),
      rather than a general ambiguity question. This check is independent of
      confidence -- a rule can be a clear, confident interpretation of the user's words
      and still contradict something already confirmed; both cases must be flagged.

If a "=== CLARIFICATION SO FAR ===" section is present in the context above, this is not
a brand-new correction -- it is a continuing conversation. Re-derive all four gates above
fresh from the ORIGINAL correction plus every question-and-answer pair together, as one
complete picture. Do not assume that only the most recently-asked gap is now resolved;
a reply can also change what you understand about the other three gates.

Also:
5. List any terms not found in the provided knowledge base

Output a single JSON object with this exact structure — no preamble, no explanation:
{
  "rules_found": <integer>,
  "rules": [
    {
      "raw_text": "<exact user words for this specific rule>",
      "understood_as": "<plain English: what this rule requires>",
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
      "clarifying_question": "<targeted question about whichever single gap (a/b/c/d) is unclear, if any, else empty string>",
      "duration_scope": "<one of: permanent | session_only | unclear>",
      "contradicts_existing_rule": <true | false>,
      "contradicting_rule_id": <integer id of the existing confirmed rule this contradicts, from its "[id=N]" marker, or null>,
      "contradicting_rule_text": "<exact text of the existing confirmed rule this contradicts, else empty string>",
      "scope_detected": "<one of: pattern | universal | unclear>",
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
  pattern   — signals: "whenever", "every time I run", "always for Y type", "when the request is about X"
  universal — signals: "always", "never", "every request", "company standard", a definition
  unclear   — scope cannot be determined from the text alone -- must set needs_clarification
"""


class FeedbackAgent(BaseAgent):
    """Correction interpretation and business rule extraction.

    Overrides subscribe() to accept FeedbackInput instead of the usual agent
    input. Never raises to the orchestrator; wraps all failures in
    FeedbackOutput(success=False).
    """

    WORKER_ID = "feedback_v1"
    CAPABILITIES = []  # feedback is invoked explicitly, not intent-routed -- inherits BaseAgent's empty default
    INPUT_SCHEMA = FeedbackInput
    OUTPUT_SCHEMA = FeedbackOutput

    def __init__(self) -> None:
        self._input: Optional[FeedbackInput] = None
        self._knowledge_ctx: Optional["KnowledgeContext"] = None
        # Set by _stage2_check_confidence_gates when any rule in the batch has an
        # unresolved confidence gate; read and cleared by _run_pipeline.
        self._pending_clarification: str = ""

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

        if inp.clarification_round >= _MAX_CLARIFICATION_ROUNDS:
            return self._give_up()

        # Stage 1 — LLM interpretation. When a clarification is already in progress,
        # _build_dynamic_context (below) feeds in the original correction plus every
        # question-and-answer pair so far, and _INTERPRETATION_INSTRUCTIONS tells the
        # model to re-derive all four confidence gates fresh from that whole picture --
        # not just resolve the one gap it most recently asked about.
        interpretation = self._stage1_interpret(inp)
        if interpretation is None:
            return FeedbackOutput(
                rules_extracted=[], rules_confirmed=[], rules_pending=[],
                new_glossary_terms=[], interpretation_summary="", success=False,
                clarifying_question=(
                    "I wasn't able to understand that clearly enough to save it safely. "
                    "Could you rephrase your correction in a sentence or two?"
                ),
            )

        rules_raw: list[dict] = interpretation.get("rules", [])
        if not rules_raw:
            return self._error_output("No rules could be extracted from the correction.")

        # Stage 2 — confidence-gate check: ask one targeted question when ANY rule in
        # the batch is unclear on what/duration/scope/conflict (skips straight through
        # only when every rule is clear on all four).
        rules_raw = self._stage2_check_confidence_gates(rules_raw)

        if self._pending_clarification:
            question = self._pending_clarification
            self._pending_clarification = ""
            return FeedbackOutput(
                rules_extracted=[], rules_confirmed=[], rules_pending=[],
                new_glossary_terms=[], interpretation_summary="", success=False,
                clarifying_question=question,
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
            f"Audience:          {inp.audience_label or '(none -- general question)'}\n"
            f"Medium:            {inp.medium or '(not specified)'}\n"
            f"Cadence:           {inp.cadence or '(not specified)'}\n\n"
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

        if inp.clarification_history or inp.original_correction:
            original = inp.original_correction or inp.raw_correction
            parts.append(f"=== ORIGINAL CORRECTION BEING CLARIFIED ===\n\n\"{original}\"\n\n")
            if inp.clarification_history:
                parts.append("=== CLARIFICATION SO FAR (question -> answer) ===\n\n")
                for turn in inp.clarification_history:
                    parts.append(f"Q: {turn.get('question', '')}\nA: {turn.get('answer', '')}\n\n")
            parts.append(f"=== MOST RECENT REPLY ===\n\n\"{inp.raw_correction}\"\n\n")
        else:
            parts.append(f"=== USER CORRECTION ===\n\n\"{inp.raw_correction}\"\n\n")
        return "".join(parts)

    # ------------------------------------------------------------------
    # Stage 2: Confidence-gate check
    #
    # Checks every rule in the batch against all four gates (what/duration/
    # scope/conflict). If ANY rule has an open gap, asks about the single
    # highest-priority one found and marks EVERY rule in the batch skipped --
    # a multi-rule correction never partially auto-confirms one rule while a
    # sibling from the same sentence still has an open question.
    # ------------------------------------------------------------------

    def _stage2_check_confidence_gates(self, rules_raw: list[dict]) -> list[dict]:
        """Priority when multiple gaps exist across the batch: contradiction (most
        consequential -- risks silently reversing something already confirmed) >
        duration unclear > scope unclear > general ambiguity the model itself flagged
        with low confidence."""
        for rule in rules_raw:
            if rule.get("contradicts_existing_rule"):
                existing = rule.get("contradicting_rule_text", "")
                question = rule.get("clarifying_question") or (
                    f"This looks like it conflicts with a rule you already confirmed: "
                    f"\"{existing}\". Do you want this new correction to replace that rule?"
                )
                self._pending_clarification = question
                return [{**r, "_skipped": True} for r in rules_raw]

        for rule in rules_raw:
            if rule.get("duration_scope", "unclear") == "unclear":
                label = rule.get("understood_as") or rule.get("raw_text", "this")
                question = rule.get("clarifying_question") or (
                    f'Just to make sure I remember this correctly: should "{label}" apply '
                    "from now on, or just to your current question?"
                )
                self._pending_clarification = question
                return [{**r, "_skipped": True} for r in rules_raw]

        for rule in rules_raw:
            if rule.get("scope_detected", "unclear") == "unclear":
                label = rule.get("understood_as") or rule.get("raw_text", "this")
                question = rule.get("clarifying_question") or (
                    f'Should "{label}" apply to every request, or only to a specific type '
                    "of request?"
                )
                self._pending_clarification = question
                return [{**r, "_skipped": True} for r in rules_raw]

        for rule in rules_raw:
            if (
                rule.get("needs_clarification")
                and rule.get("clarifying_question")
                and rule.get("confidence", 1.0) < 0.5
            ):
                self._pending_clarification = rule["clarifying_question"]
                return [{**r, "_skipped": True} for r in rules_raw]

        # No open gaps anywhere in the batch -- every rule proceeds to Stage 3 as-is.
        return [{**r, "needs_clarification": False} for r in rules_raw]

    # ------------------------------------------------------------------
    # Stage 3: Scope classification — AI-driven reasoning
    # ------------------------------------------------------------------

    def _stage3_classify_scope(
        self, rules_raw: list[dict], inp: FeedbackInput
    ) -> list[dict]:
        """Invariant: Stage 2 already marks every rule with a genuinely 'unclear' scope
        as _skipped (see _stage2_check_confidence_gates), so the "still unclear, ask the
        AI to guess" branch below is only ever reached for rules Stage 1 already scoped
        with real confidence -- its remaining job there is just filling in
        pattern_description. Do not remove Stage 2's scope-unclear check on the
        assumption this method already handles it; that would silently re-open the
        guess-instead-of-ask path this whole rework removed."""
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
            f"Original query context: \"{inp.raw_input_prompt}\"\n\n"
            "Scope definitions:\n"
            "  pattern   — applies to a type of request or context (signals: 'whenever', 'all X requests')\n"
            "  universal — applies to every request always (signals: 'always', 'never', a definition)\n\n"
            "If the rule is defining a term or establishing a business concept, default to 'universal'.\n\n"
            'Output JSON only:\n'
            '{"scope": "<pattern|universal>", "confidence": <0.0-1.0>, '
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
        return {"scope": "universal", "confidence": 0.5, "pattern_description": "", "reasoning": "fallback"}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _give_up() -> FeedbackOutput:
        """The clarification round cap (_MAX_CLARIFICATION_ROUNDS) was reached without
        ever getting confident enough to save something safely. Stop the conversation,
        say so plainly, and save nothing -- never fall back to a guess, and never loop
        forever. The caller (api/web_app.py) clears its clarification session state on
        this same turn, so a fresh correction later starts clean."""
        return FeedbackOutput(
            rules_extracted=[], rules_confirmed=[], rules_pending=[],
            new_glossary_terms=[],
            interpretation_summary=(
                "I still wasn't able to confirm exactly what should be saved after a few "
                "tries, so I haven't remembered anything from this conversation. Feel free "
                "to try again whenever you'd like, maybe with a bit more detail."
            ),
            success=False, gave_up=True,
        )

    def _dict_to_rule(self, rule_dict: dict, inp: FeedbackInput) -> BusinessRule:
        scope = rule_dict.get("scope_detected", "universal")
        priority_map = {"pattern": 2, "universal": 1}
        return BusinessRule(
            rule_id=str(uuid.uuid4()),
            created_at=datetime.now(tz=timezone.utc).isoformat(),
            verified_by=inp.user_identity or "unknown",
            raw_correction=rule_dict.get("raw_text", inp.raw_correction),
            rule_description=rule_dict.get("understood_as", inp.raw_correction),
            rule_type=rule_dict.get("rule_type", "general"),
            structured_value=rule_dict.get("structured_value", {}),
            scope=scope,
            medium=inp.medium if scope == "pattern" else None,
            cadence=inp.cadence if scope == "pattern" else None,
            pattern_description=rule_dict.get("pattern_description"),
            pattern_match_logic=rule_dict.get("scope_signals"),
            confidence=rule_dict.get("confidence", 1.0),
            source="hitl_feedback",
            clarification_rounds=inp.clarification_round,
            applies_to_future=(rule_dict.get("duration_scope", "permanent") == "permanent"),
            supersedes_rule_id=rule_dict.get("contradicting_rule_id"),
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
                worker_id="feedback_agent",
                raw_input=inp.raw_input_prompt or "",
                generated_output=json.dumps(inp.execution_context, default=str),
                correction_description=inp.raw_correction,
                inferred_failure_type=failure_type,
                glossary_gaps=[],
                intent_type="sizing_request" if inp.has_prior_result else "general_question",
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
