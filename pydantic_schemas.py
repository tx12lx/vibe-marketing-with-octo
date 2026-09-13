from __future__ import annotations

import os
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator

# Same env vars agents/quant_agent.py and agents/nexus_agent.py read -- a schema default
# should never hardcode a value the agents themselves treat as configurable.
_DEFAULT_BQ_PROJECT = os.getenv("BQ_PROJECT_ID", "bi-srv-hsmdet-pr-7b9def")
_DEFAULT_BQ_DATASET = os.getenv("BQ_DATASET", "campaign_data")


class AudienceSizingRequest(BaseModel):
    """Validated payload emitted by Nexus and consumed exclusively by Quant.

    Every request handled by this tool is ad hoc -- it may or may not relate to
    a named campaign, and nothing here requires one. Quant strictly rejects any
    payload that does not conform to this schema. The strict config prevents
    silent type coercion — wrong types fail loudly.
    """

    model_config = ConfigDict(strict=True)

    audience_label: Optional[str] = None
    # No fake non-empty default (e.g. "ad-hoc"/"unspecified") -- these are genuinely
    # optional descriptive metadata, only meaningful when the consultant actually
    # stated a cadence/medium. A placeholder default would get displayed to the user
    # as if it were real information (see agents/nexus_agent.py's _NL_PARSE_PROMPT).
    cadence: Optional[str] = None
    medium: Optional[str] = None
    target_population: str
    filters: list[str]
    exclusion_layers: Optional[list[str]] = None
    optimization_context: Optional[str] = None
    bq_project: str = _DEFAULT_BQ_PROJECT
    bq_dataset: str = _DEFAULT_BQ_DATASET

    @field_validator("filters")
    @classmethod
    def filters_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("filters must contain at least one entry")
        return v


class WaterfallLayer(BaseModel):
    layer_name: str
    audience_count: int


class QuantAuditLog(BaseModel):
    """Clean output log produced by Quant after a successful audit run."""

    request: AudienceSizingRequest
    sql: str
    waterfall: list[WaterfallLayer]
    final_count: int
    optimization_note: Optional[str] = None
    status: str = "success"


class NexusErrorPayload(BaseModel):
    """Error envelope Quant returns to Nexus when an audit fails.

    No raw stack traces — only a human-readable summary and a hint that
    Nexus can use for its single automated retry.
    """

    error_type: str
    error_summary: str
    original_request: dict
    retry_hint: str
    attempt: int = 1
    failed_sql: Optional[str] = None


class IntentClassification(BaseModel):
    """Routing decision emitted by core.router.IntentRouter.classify().

    intent_type is a plain str so new agents can register custom intent types
    via CAPABILITIES without changing this schema.
    """

    intent_type: str
    confidence: float
    knowledge_sources_consulted: list[str] = []
    business_rules_applied: list[str] = []
    reasoning: str = ""
    narration: str = ""
    # Set when the router genuinely isn't confident which agent fits -- the caller
    # must ask clarifying_question instead of guessing/routing anywhere.
    needs_clarification: bool = False
    clarifying_question: str = ""


class AgentCapability(BaseModel):
    """One task-type an agent can perform, described so core.router.IntentRouter
    can build its classification prompt entirely from the live set of registered
    agents -- no agent name or intent type is ever hardcoded into the router
    itself. Written the way you'd describe a new teammate's job to the rest of
    the team: a short, plain description plus a couple of realistic examples."""

    intent_type: str
    description: str
    examples: list[str] = []


class AgentResult(BaseModel):
    """Uniform envelope any routed agent's handle() call returns, so the
    dispatcher (vibe_orchestrator.route_by_intent) reacts the same way no
    matter which agent produced it.

    kind:
      "answer"  -- answer_text is the final answer, nothing more to do.
      "log"     -- log is a completed QuantAuditLog (a sizing result).
      "stuck"   -- stuck_reason explains what went wrong or what's missing.
      "handoff" -- this agent's part is done; hand handoff_payload to the
                   agent registered under handoff_to and call its handle()
                   next (see NexusAgent.handle()'s sizing_request case).
    """

    kind: Literal["answer", "log", "stuck", "handoff"]
    answer_text: Optional[str] = None
    log: Optional[QuantAuditLog] = None
    stuck_reason: Optional[str] = None
    handoff_to: Optional[str] = None
    handoff_payload: Optional[AudienceSizingRequest] = None


class SemanticFailureLog(BaseModel):
    """Structured failure record written on HITL NO responses (terminal and web)."""

    timestamp: str
    worker_id: str
    raw_input: str
    generated_output: str
    correction_description: str
    inferred_failure_type: Literal[
        "wrong_column",
        "wrong_filter",
        "missing_exclusion",
        "tier_mismatch",
        "schema_gap",
        "general_answer",
    ]
    glossary_gaps: list[str]
    intent_type: str = ""  # any registered intent type, e.g. sizing_request | general_question


class BusinessRule(BaseModel):
    """A human-verified business rule extracted from HITL NO feedback."""

    rule_id: str
    created_at: str
    verified_by: str

    raw_correction: str
    rule_description: str
    rule_type: str          # filter_add | exclusion_add | lookback_days | population_note | general
    structured_value: dict  # rule-type-specific payload

    scope: str              # pattern | universal
    medium: Optional[str] = None
    cadence: Optional[str] = None
    pattern_description: Optional[str] = None
    pattern_match_logic: Optional[str] = None

    confidence: float
    source: str
    clarification_rounds: int

    applies_to_future: bool
    priority: int           # pattern=2, universal=1

    # Set when this rule is meant to replace an existing confirmed rule -- the ID (not
    # just the text) of the rule being retired, so knowledge/context.py's add_rule() can
    # atomically mark that old rule 'retired' in the same step this one is saved. None
    # for a rule that isn't overriding anything.
    supersedes_rule_id: Optional[int] = None

    applied_count: int = 0
    last_applied_at: Optional[str] = None
    hitl_yes_after_rule_count: int = 0  # YES responses on queries where this rule was applied
    conflict_notes: Optional[str] = None  # set when this rule resolved a conflict with a prior rule


class FeedbackInput(BaseModel):
    """Input contract for FeedbackAgent — all context needed to interpret a correction."""

    raw_correction: str       # User's exact words -- the most recent reply if a clarification is in progress
    audience_label: Optional[str] = None
    medium: Optional[str] = None
    cadence: Optional[str] = None
    execution_context: dict   # filters applied, tables used, audience count, waterfall steps
    existing_rules: list[dict]
    raw_input_prompt: str     # What user originally asked
    user_identity: str = "unknown"  # who is actually submitting this correction -- carried through to BusinessRule.verified_by
    # Whether this correction is on a sizing result (True) or a general_question answer
    # (False, no prior QuantAuditLog) -- set from vibe_orchestrator._run_adhoc_feedback's
    # own `log is not None` check, so SemanticFailureLog.intent_type reflects what actually
    # happened rather than a guess.
    has_prior_result: bool = False
    # Non-empty only when a clarification conversation is already in progress on this
    # session -- the very first correction that kicked it off. Stage 1 re-derives
    # everything (what/duration/scope/conflict) from this plus clarification_history
    # plus the latest reply every round, rather than assuming only the just-asked gap
    # was resolved. Empty means raw_correction above is a brand-new correction.
    original_correction: str = ""
    # Accumulated {"question": str, "answer": str} pairs for the open clarification
    # thread on this session, oldest first. Empty when no clarification is in progress.
    clarification_history: list[dict] = []
    # How many clarification round-trips have already happened on this thread -- the
    # caller (session state) owns and increments this; FeedbackAgent only reads it to
    # decide whether the round cap (see agents/feedback_agent.py) has been reached.
    clarification_round: int = 0


class FeedbackOutput(BaseModel):
    """Output produced by FeedbackAgent.execute()."""

    rules_extracted: list[BusinessRule]
    rules_confirmed: list[BusinessRule]
    rules_pending: list[BusinessRule]
    new_glossary_terms: list[dict]
    interpretation_summary: str
    success: bool
    clarifying_question: str = ""  # Non-empty when AI needs more info before saving anything
    # Set when the clarification round cap was hit without ever reaching enough
    # confidence to save something safely -- the caller must stop the conversation,
    # say so plainly, and save nothing, rather than loop forever or fall back to a guess.
    gave_up: bool = False
