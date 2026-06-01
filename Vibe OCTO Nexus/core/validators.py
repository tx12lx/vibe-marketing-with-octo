from __future__ import annotations

from typing import Any, List, Literal, Optional, Union

from pydantic import BaseModel, Field


class FieldRef(BaseModel):
    table: str
    field: str


class Condition(BaseModel):
    condition_id: str
    business_term: str
    data_source: FieldRef
    operator: str
    value: Any
    confidence_score: float = Field(ge=0.0, le=1.0)
    glossary_matched: bool
    sql_snippet: str


class DataRules(BaseModel):
    filter_logic: Literal["AND", "OR"]
    conditions: List[Condition]


class Join(BaseModel):
    join_id: str
    from_table: str
    from_field: str
    to_table: str
    to_field: str
    join_type: Literal["INNER", "LEFT", "RIGHT", "FULL"]
    sql_snippet: str


class PrimarySegment(BaseModel):
    segment_name: str
    business_description: str
    data_rules: DataRules
    joins: List[Join] = []


class Exclusion(BaseModel):
    exclusion_id: str
    exclusion_name: str
    business_description: str
    data_source: FieldRef
    operator: str
    value: Any
    lookback_window_days: Optional[int] = None
    confidence_score: float = Field(ge=0.0, le=1.0)
    sql_snippet: str


class FrequencyCap(BaseModel):
    max_contacts_per_customer: int
    lookback_window_days: int
    sql_snippet: str


class Audience(BaseModel):
    primary_segment: PrimarySegment
    exclusions: List[Exclusion] = []
    frequency_cap: Optional[FrequencyCap] = None


class EligibilityRule(BaseModel):
    rule_id: str
    description: str
    sql_snippet: str


class OfferConflict(BaseModel):
    conflict_type: str
    description: str
    severity: Literal["high", "medium", "low"]


class Offer(BaseModel):
    offer_id: str
    offer_name: str
    business_description: str
    offer_type: Literal["discount", "bundle", "loyalty", "limited_time", "other"]
    eligibility_rules: List[EligibilityRule] = []
    known_conflicts: List[OfferConflict] = []


class SendTiming(BaseModel):
    send_on_day: int
    send_time: str
    send_time_zone: str


class ChannelStep(BaseModel):
    sequence_order: int
    channel: Literal["sms", "email", "outbound_call", "push", "in_app"]
    timing: SendTiming
    rationale: str


class ChannelConstraint(BaseModel):
    channel: str
    constraint_description: str
    sql_snippet: str


class Channels(BaseModel):
    preferred_channels: List[str]
    channel_sequence: List[ChannelStep] = []
    channel_constraints: List[ChannelConstraint] = []


class Ambiguity(BaseModel):
    flag_id: str
    field: str
    issue: str
    severity: Literal["high", "medium", "low"]
    suggested_clarification: str


class Contradiction(BaseModel):
    type: str
    description: str
    fields_involved: List[str]
    recommended_resolution: str


class LowConfidenceRule(BaseModel):
    condition_id: str
    confidence_score: float
    reason: str


class GlossaryMiss(BaseModel):
    business_term: str
    closest_match: str
    action_required: str


class QCFlags(BaseModel):
    ambiguities: List[Ambiguity] = []
    contradictions: List[Contradiction] = []
    low_confidence_rules: List[LowConfidenceRule] = []
    glossary_misses: List[GlossaryMiss] = []


class ExecutionReadiness(BaseModel):
    completeness_score: float = Field(ge=0, le=100)
    overall_confidence: float = Field(ge=0.0, le=1.0)
    qa_passed: bool
    ready_to_execute: bool
    blockers: List[str] = []
    warnings: List[str] = []


class TableDef(BaseModel):
    table_name: str
    alias: str
    description: str


class PrimarySource(BaseModel):
    database: str
    project: str
    dataset: str
    tables: List[TableDef]


class DataSources(BaseModel):
    primary_source: PrimarySource


class BriefMetadata(BaseModel):
    brief_id: str
    brief_name: str
    campaign_objective: Literal[
        "winback", "upsell", "retention", "acquisition", "reactivation"
    ]
    portfolio: str
    purpose: str
    cadence: str
    medium: List[str]
    created_date: str
    translation_timestamp: str
    vibe_briefing_version: str
    glossary_version: str


class ToolInputBase(BaseModel):
    status: str
    instructions: str


class SizingToolInput(ToolInputBase):
    primary_segment_ref: str = "audience.primary_segment"
    exclusions_ref: str = "audience.exclusions"


class QAChecklistInput(ToolInputBase):
    flags_ref: str = "qc_flags"


class FrequencyGovernorInput(ToolInputBase):
    frequency_cap_ref: str = "audience.frequency_cap"


class ChannelSequencerInput(ToolInputBase):
    channel_sequence_ref: str = "channels.channel_sequence"


class DownstreamToolInputs(BaseModel):
    sizing_tool: SizingToolInput
    qa_checklist: QAChecklistInput
    frequency_governor: FrequencyGovernorInput
    channel_sequencer: ChannelSequencerInput
    data_brief_assistant: ToolInputBase


class VibeBriefingOutput(BaseModel):
    brief_metadata: BriefMetadata
    data_sources: DataSources
    audience: Audience
    offer: Offer
    channels: Channels
    qc_flags: QCFlags
    execution_readiness: ExecutionReadiness
    downstream_tool_inputs: DownstreamToolInputs

    def readiness_summary(self) -> str:
        r = self.execution_readiness
        return (
            f"Completeness: {r.completeness_score:.0f}% | "
            f"Confidence: {r.overall_confidence:.2f} | "
            f"Ready: {r.ready_to_execute}"
        )
