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

    Quant strictly rejects any payload that does not conform to this schema.
    The strict config prevents silent type coercion — wrong types fail loudly.
    """

    model_config = ConfigDict(strict=True)

    campaign_name: str
    campaign_code: str
    campaign_sub_code: str
    cadence: str
    medium: str
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


class AdHocSizingRequest(AudienceSizingRequest):
    """Path 2 variant. Cadence and medium default to safe sentinels instead of requiring
    values from the caller — avoids validation errors on un-stated campaign attributes."""

    cadence: str = "ad-hoc"
    medium: str = "unspecified"


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
    """Intent classification emitted by NexusAgent.classify_intent().

    intent_type is a plain str so new agents can register custom intent types
    via HANDLED_INTENTS without changing this schema.

    campaign_identified/campaign_code are informational context about whether
    the request named a specific past campaign -- kept for audit-trail quality
    even though only one intent type (sizing_request) currently exists to act
    on it, and it does so by translating the request directly from the
    schema/glossary/business-rules knowledge base rather than by looking up a
    stored campaign brief (see agents/nexus_agent.py's module docstring).
    """

    intent_type: str
    confidence: float
    campaign_identified: bool
    campaign_code: Optional[str] = None
    knowledge_sources_consulted: list[str] = []
    business_rules_applied: list[str] = []
    reasoning: str = ""


class SemanticFailureLog(BaseModel):
    """Structured failure record written on HITL NO responses (terminal and web)."""

    timestamp: str
    campaign_code: str
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
    intent_type: str = ""  # sizing_request | general_question


class BusinessRule(BaseModel):
    """A human-verified business rule extracted from HITL NO feedback."""

    rule_id: str
    created_at: str
    verified_by: str

    raw_correction: str
    rule_description: str
    rule_type: str          # filter_add | exclusion_add | lookback_days | population_note | general
    structured_value: dict  # rule-type-specific payload

    scope: str              # campaign | pattern | universal
    campaign_code: Optional[str] = None
    campaign_name: Optional[str] = None
    medium: Optional[str] = None
    cadence: Optional[str] = None
    pattern_description: Optional[str] = None
    pattern_match_logic: Optional[str] = None

    confidence: float
    source: str
    clarification_rounds: int

    applies_to_future: bool
    overrides_acc_summary: bool
    priority: int           # campaign=3, pattern=2, universal=1

    applied_count: int = 0
    last_applied_at: Optional[str] = None
    hitl_yes_after_rule_count: int = 0  # YES responses on queries where this rule was applied
    conflict_notes: Optional[str] = None  # set when this rule resolved a conflict with a prior rule


class FeedbackInput(BaseModel):
    """Input contract for FeedbackAgent — all context needed to interpret a correction."""

    raw_correction: str       # User's exact words
    campaign_code: str
    campaign_name: str
    medium: str
    cadence: str
    campaign_purpose: str
    execution_context: dict   # filters applied, tables used, audience count, waterfall steps
    existing_rules: list[dict]
    knowledge_tier: str       # GOLD | SILVER | BRONZE
    raw_input_prompt: str     # What user originally asked
    user_identity: str = "unknown"  # who is actually submitting this correction -- carried through to BusinessRule.verified_by
    # Set only when the PREVIOUS correction on this session was blocked because it
    # contradicted an existing confirmed rule -- raw_correction above is then the user's
    # answer to "do you want this to override that rule?", not a fresh correction to
    # interpret from scratch. See FeedbackAgent._resolve_pending_contradiction().
    pending_contradiction_text: str = ""


class FeedbackOutput(BaseModel):
    """Output produced by FeedbackAgent.execute()."""

    rules_extracted: list[BusinessRule]
    rules_confirmed: list[BusinessRule]
    rules_pending: list[BusinessRule]
    new_glossary_terms: list[dict]
    interpretation_summary: str
    success: bool
    clarifying_question: str = ""  # Non-empty when AI needs more info before saving a rule
    # Set alongside clarifying_question only when the question is specifically about a
    # contradiction with an existing confirmed rule (not general ambiguity) -- the caller
    # persists this so the user's next reply can be resolved as a yes/no answer instead of
    # being re-run through the full interpretation pipeline, which would just flag the same
    # contradiction again.
    contradiction_existing_rule_text: str = ""
