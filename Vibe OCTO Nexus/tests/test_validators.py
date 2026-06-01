from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.validators import VibeBriefingOutput


MINIMAL_VALID = {
    "brief_metadata": {
        "brief_id": "test-001",
        "brief_name": "Test Brief",
        "campaign_objective": "winback",
        "portfolio": "postpaid-winback",
        "purpose": "winback",
        "cadence": "monthly",
        "medium": ["sms"],
        "created_date": "2026-05-28",
        "translation_timestamp": "2026-05-28T10:00:00+00:00",
        "vibe_briefing_version": "1.0",
        "glossary_version": "1.0",
    },
    "data_sources": {
        "primary_source": {
            "database": "bigquery",
            "project": "test-project",
            "dataset": "test-dataset",
            "tables": [
                {"table_name": "customer_base", "alias": "c", "description": "Main customer table"}
            ],
        }
    },
    "audience": {
        "primary_segment": {
            "segment_name": "lapsed postpaid HVC",
            "business_description": "High-value postpaid customers who have lapsed",
            "data_rules": {
                "filter_logic": "AND",
                "conditions": [
                    {
                        "condition_id": "c001",
                        "business_term": "high-value customer",
                        "data_source": {"table": "customer_base", "field": "arpu"},
                        "operator": ">=",
                        "value": 80,
                        "confidence_score": 0.9,
                        "glossary_matched": True,
                        "sql_snippet": "arpu >= 80",
                    }
                ],
            },
            "joins": [],
        },
        "exclusions": [],
        "frequency_cap": None,
    },
    "offer": {
        "offer_id": "offer-001",
        "offer_name": "20% loyalty discount",
        "business_description": "20% off first 3 months on any new postpaid plan",
        "offer_type": "discount",
        "eligibility_rules": [],
        "known_conflicts": [],
    },
    "channels": {
        "preferred_channels": ["sms"],
        "channel_sequence": [
            {
                "sequence_order": 1,
                "channel": "sms",
                "timing": {"send_on_day": 1, "send_time": "10:00", "send_time_zone": "Australia/Sydney"},
                "rationale": "Primary contact channel",
            }
        ],
        "channel_constraints": [],
    },
    "qc_flags": {
        "ambiguities": [],
        "contradictions": [],
        "low_confidence_rules": [],
        "glossary_misses": [],
    },
    "execution_readiness": {
        "completeness_score": 95,
        "overall_confidence": 0.9,
        "qa_passed": True,
        "ready_to_execute": True,
        "blockers": [],
        "warnings": [],
    },
    "downstream_tool_inputs": {
        "sizing_tool": {
            "status": "ready",
            "primary_segment_ref": "audience.primary_segment",
            "exclusions_ref": "audience.exclusions",
            "instructions": "Execute SQL from audience.primary_segment.data_rules.conditions",
        },
        "qa_checklist": {
            "status": "ready",
            "flags_ref": "qc_flags",
            "instructions": "Review all flags before execution",
        },
        "frequency_governor": {
            "status": "ready",
            "frequency_cap_ref": "audience.frequency_cap",
            "instructions": "No frequency cap specified — use default policy",
        },
        "channel_sequencer": {
            "status": "ready",
            "channel_sequence_ref": "channels.channel_sequence",
            "instructions": "Execute SMS on day 1",
        },
        "data_brief_assistant": {
            "status": "reserved — future tool",
            "instructions": "Vibe Briefing will power this widget in a future phase",
        },
    },
}


def test_valid_output_passes():
    output = VibeBriefingOutput.model_validate(MINIMAL_VALID)
    assert output.brief_metadata.brief_id == "test-001"
    assert output.execution_readiness.ready_to_execute is True


def test_confidence_score_bounds():
    bad = json.loads(json.dumps(MINIMAL_VALID))
    bad["audience"]["primary_segment"]["data_rules"]["conditions"][0]["confidence_score"] = 1.5
    with pytest.raises(ValidationError):
        VibeBriefingOutput.model_validate(bad)


def test_invalid_objective_rejected():
    bad = json.loads(json.dumps(MINIMAL_VALID))
    bad["brief_metadata"]["campaign_objective"] = "awareness"
    with pytest.raises(ValidationError):
        VibeBriefingOutput.model_validate(bad)


def test_readiness_summary():
    output = VibeBriefingOutput.model_validate(MINIMAL_VALID)
    summary = output.readiness_summary()
    assert "95%" in summary
    assert "0.90" in summary
    assert "True" in summary
