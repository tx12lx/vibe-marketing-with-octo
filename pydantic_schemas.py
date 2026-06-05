from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator


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
    bq_dataset: str = "adobe"

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
    campaign_tier: Literal["GOLD", "BRONZE"]
    knowledge_source: Literal["brief_text", "bq_metadata", "nl_only"]
    gold_blueprint_id: Optional[str] = None  # "{camp_id}::{sub_camp_id}" if GOLD

    # Audience definition
    target_population: str
    filters: list[str]
    exclusion_layers: Optional[list[str]] = None
    optimization_context: Optional[str] = None

    # BQ routing
    bq_project: str = "bi-srv-hsmdet-pr-7b9def"
    bq_dataset: str = "adobe"

    # Runtime audit output
    discrepancy_flags: list[str] = []
    runtime_schema_snapshot: Optional[dict] = None

    # Briefing agent inputs
    brief_agent_inputs: Optional[dict] = None

    # Execution guardrails
    max_waterfall_steps: int = 7
    require_gch_suppression: bool = False
    dnc_channels: list[str] = []

    @field_validator("filters")
    @classmethod
    def filters_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("filters must contain at least one entry")
        return v

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


class SemanticFailureLog(BaseModel):
    """Structured failure record written by HITLAuditLoop on HITL NO responses."""

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
    ]
    glossary_gaps: list[str]
