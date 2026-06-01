from __future__ import annotations

from typing import Optional

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
