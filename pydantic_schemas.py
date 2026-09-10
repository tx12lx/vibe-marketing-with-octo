from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator


class BriefExtraction(BaseModel):
    """Structured extraction from a campaign data brief — produced by two-stage LLM extraction
    in `--refresh-briefs` mode.  All fields default to empty so records written by
    `--full-refresh` (which does not contact Google Sheets) are schema-valid."""

    campaign_strategy_summary: str = ""
    targeting_filters: list[str] = []
    exclusion_rules: list[str] = []
    channel_governance: dict[str, Any] = {}
    geographic_scope: list[str] = []
    lifecycle_constraints: list[str] = []
    product_eligibility_pairs: list[str] = []
    segmentation_only_notes: list[str] = []
    ambiguities_found: list[str] = []
    extraction_confidence: dict[str, float] = {}
    extracted_at: Optional[str] = None


class CampaignCriteria(BaseModel):
    """Original schema — preserved for backwards compatibility."""

    model_config = ConfigDict(strict=True)

    campaign_name: str
    campaign_code: str
    campaign_sub_code: str
    cadence: str
    medium: str
    exclusion_layers: Optional[list[str]] = None


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
    bq_project: str = "bi-srv-hsmdet-pr-7b9def"
    bq_dataset: str = "campaign_data"

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


class UniversalJSONSpec(BaseModel):
    """Universal inter-agent contract. Superset of AudienceSizingRequest.

    Emitted by NexusAgent.build_universal_spec().
    Consumed by QuantAgent.audit_from_spec() and BriefingAgent.subscribe().
    """

    model_config = ConfigDict(strict=True)

    # Core identity
    campaign_name: str
    campaign_code: str
    campaign_sub_code: str
    cadence: str
    medium: str

    # Tier and knowledge source provenance
    campaign_tier: Literal["GOLD", "SILVER", "BRONZE"]
    knowledge_source: Literal["brief_text", "bq_metadata", "nl_only"]
    gold_blueprint_id: Optional[str] = None  # "{camp_id}::{sub_camp_id}" if GOLD

    # Audience definition
    target_population: str
    # filters is required and non-empty when consumed by QuantAgent (sizing/execution).
    # For brief-only requests it defaults to [] -- BriefingAgent does not use SQL filters.
    filters: list[str] = []
    exclusion_layers: Optional[list[str]] = None
    optimization_context: Optional[str] = None

    # BQ routing
    bq_project: str = "bi-srv-hsmdet-pr-7b9def"
    bq_dataset: str = "campaign_data"

    # Runtime audit output
    discrepancy_flags: list[str] = []
    runtime_schema_snapshot: Optional[dict] = None

    # Briefing agent inputs
    brief_agent_inputs: Optional[dict] = None

    # Execution guardrails
    max_waterfall_steps: int = 10
    require_gch_suppression: bool = False
    dnc_channels: list[str] = []

    def to_audience_sizing_request(self) -> "AudienceSizingRequest":
        """Backwards-compatible downcast for QuantAgent.audit()."""
        return AudienceSizingRequest(
            campaign_name=self.campaign_name,
            campaign_code=self.campaign_code,
            campaign_sub_code=self.campaign_sub_code,
            cadence=self.cadence,
            medium=self.medium,
            target_population=self.target_population,
            filters=self.filters,
            exclusion_layers=self.exclusion_layers,
            optimization_context=self.optimization_context,
            bq_project=self.bq_project,
            bq_dataset=self.bq_dataset,
        )


class IntentClassification(BaseModel):
    """Intent classification emitted by NexusAgent.classify_intent().

    Replaces the WORKFLOW_A / WORKFLOW_B binary with a five-type taxonomy
    so every request can be routed through the unified knowledge pipeline.

    intent_type is a plain str so new agents can register custom intent types
    via HANDLED_INTENTS without changing this schema.  The orchestrator validates
    at runtime against the discovered intent set.
    """

    intent_type: str  # validated at runtime against _INTENT_ROUTING keys
    confidence: float
    campaign_identified: bool
    campaign_code: Optional[str] = None
    knowledge_sources_consulted: list[str] = []
    business_rules_applied: list[str] = []
    reasoning: str = ""
    data_domains: list[str] = []  # AI-selected knowledge layer domains for this request


class SegmentCriterion(BaseModel):
    """A single named, mutually exclusive audience segment."""

    name: str        # Short label, e.g. "Never Had Mobility"
    description: str  # One precise sentence describing who qualifies


class BriefingOutput(BaseModel):
    """Output produced by BriefingAgent.execute()."""

    campaign_name: str
    tier: str
    brief_markdown: str
    executive_summary: str
    targeting_logic_summary: str
    strategic_recommendations: list[str]
    data_sources_cited: list[str]
    confidence_score: float  # 0.0 to 1.0
    generated_at: str        # ISO 8601
    error_reason: Optional[str] = None
    knowledge_sources_used: Optional[list[str]] = None

    # Structured targeting criteria — populated by all data brief generation paths
    structured_universe: Optional[str] = None          # one-sentence initial universe
    structured_exclusions: Optional[list[str]] = None  # ordered list of exclusion criteria
    structured_segments: Optional[list[SegmentCriterion]] = None  # mutually exclusive segments


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
    intent_type: str = ""  # campaign_execution | general_question | brief_generation | etc.


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


class FeedbackOutput(BaseModel):
    """Output produced by FeedbackAgent.execute()."""

    rules_extracted: list[BusinessRule]
    rules_confirmed: list[BusinessRule]
    rules_pending: list[BusinessRule]
    new_glossary_terms: list[dict]
    interpretation_summary: str
    success: bool
    clarifying_question: str = ""  # Non-empty when AI needs more info before saving a rule
