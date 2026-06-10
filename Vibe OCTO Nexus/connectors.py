"""connectors.py — Brief enrichment: wrap brief JSON in the master contract.

Computes sizing_tool, brief_regeneration, and brief_assistant outputs inline
and returns the full master contract dict. No class hierarchy required.
"""
from __future__ import annotations

from datetime import datetime, timezone

SCHEMA_VERSION = "1.0"

# --- brief_assistant helpers ---

_REQUIRED_BASE = [
    ("campaign.brand",    lambda b: bool(b.get("campaign", {}).get("brand"))),
    ("campaign.medium",   lambda b: bool(b.get("campaign", {}).get("medium"))),
    ("campaign.cadence",  lambda b: bool(b.get("campaign", {}).get("cadence"))),
    ("targeting.include", lambda b: len(b.get("targeting", {}).get("include", [])) > 0),
    ("offer.product",     lambda b: bool(b.get("offer", {}).get("product"))),
    ("segmentation.base", lambda b: bool(b.get("segmentation", {}).get("base"))),
]
_REQUIRED_OB  = [("vendor.name", lambda b: bool(b.get("vendor", {}).get("name")))]
_REQUIRED_CSS = [
    ("vendor.css_campaign_id",    lambda b: bool(b.get("vendor", {}).get("css_campaign_id"))),
    ("vendor.css_measurement_id", lambda b: bool(b.get("vendor", {}).get("css_measurement_id"))),
]
_OB_KEYWORDS  = {"ob", "outbound_call", "outbound"}
_CSS_KEYWORDS = {"css"}


def _brief_assistant(brief_json: dict) -> dict:
    medium      = brief_json.get("campaign", {}).get("medium", [])
    vendor_name = (brief_json.get("vendor", {}).get("name") or "").lower()
    is_ob  = any(m.lower().strip() in _OB_KEYWORDS for m in medium)
    is_css = any(k in vendor_name for k in _CSS_KEYWORDS)

    checks: dict[str, bool] = {name: fn(brief_json) for name, fn in _REQUIRED_BASE}
    if is_ob:
        checks.update({name: fn(brief_json) for name, fn in _REQUIRED_OB})
    if is_css:
        checks.update({name: fn(brief_json) for name, fn in _REQUIRED_CSS})

    failed = [k for k, v in checks.items() if not v]
    completeness_score = round(((len(checks) - len(failed)) / len(checks)) * 100) if checks else 0

    flags    = brief_json.get("data_brief_flags", {})
    guidance = [f"Required field missing: {f}" for f in failed]
    guidance += [f"Incomplete: {f}" for f in flags.get("missing_required_fields", []) if f not in guidance]
    if flags.get("ob_vendor_missing"):
        guidance.append("OB campaign: specify the vendor/dialing site.")
    if flags.get("css_ids_missing"):
        guidance.append("CSS vendor: provide both CSS campaign ID and CSS measurement ID.")
    guidance += [f"Clarify: {a}" for a in flags.get("ambiguities", [])[:5]]

    return {
        "status": "ready",
        "completeness_score": completeness_score,
        "required_fields_check": checks,
        "failed_checks": failed,
        "include_criteria_count": len(brief_json.get("targeting", {}).get("include", [])),
        "exclude_criteria_count": len(brief_json.get("targeting", {}).get("exclude", [])),
        "ambiguities": list(flags.get("ambiguities", [])),
        "missing_required_fields": list(flags.get("missing_required_fields", [])),
        "guidance": guidance,
    }


def _brief_regeneration(brief_json: dict) -> dict:
    campaign  = brief_json.get("campaign", {})
    targeting = brief_json.get("targeting", {})
    defaults  = brief_json.get("default_exclusions", {})
    return {
        "status": "ready",
        "structured_sections": {
            "campaign_overview": {
                "brand": campaign.get("brand"),
                "portfolio": campaign.get("portfolio"),
                "portfolio_description": campaign.get("portfolio_description"),
                "purpose": campaign.get("purpose"),
                "medium": campaign.get("medium", []),
                "cadence": campaign.get("cadence"),
            },
            "offer":        brief_json.get("offer", {}),
            "segmentation": brief_json.get("segmentation", {}),
            "vendor":       brief_json.get("vendor", {}),
            "channel_rules": brief_json.get("channel", {}),
            "target_audience": [
                {"criterion": c.get("term"), "description": c.get("logic"),
                 "threshold": c.get("threshold"), "confidence_score": c.get("confidence_score")}
                for c in targeting.get("include", [])
            ],
            "campaign_exclusions": [
                {"criterion": c.get("term"), "description": c.get("logic"),
                 "threshold": c.get("threshold"), "lookback_days": c.get("lookback_days"),
                 "confidence_score": c.get("confidence_score")}
                for c in targeting.get("exclude", [])
            ],
            "default_exclusions": {
                "dnc": defaults.get("dnc", {}),
                "non_primary_subscriber": defaults.get("non_primary_subscriber", True),
                "stop_sell": defaults.get("stop_sell", True),
                "castl_compliance": defaults.get("castl_compliance", True),
            },
            "quality_flags": brief_json.get("data_brief_flags", {}),
        },
        "campaign_summary": brief_json.get("campaign_summary", ""),
    }


def _sizing_tool(brief_json: dict) -> dict:
    return {
        "status": "ready",
        "include_criteria_count": len(brief_json.get("targeting", {}).get("include", [])),
        "exclude_criteria_count": len(brief_json.get("targeting", {}).get("exclude", [])),
    }


def enrich(brief_json: dict, glossary_data: dict, glossary_version: str = "unknown") -> dict:
    """Wrap brief JSON in the master contract and compute all connector outputs."""
    connector_outputs: dict[str, dict] = {}
    for name, fn in [
        ("sizing_tool",        _sizing_tool),
        ("brief_regeneration", _brief_regeneration),
        ("brief_assistant",    _brief_assistant),
    ]:
        try:
            connector_outputs[name] = fn(brief_json)
        except Exception as exc:
            connector_outputs[name] = {"status": "error", "error": str(exc)}

    return {
        "_meta": {
            "schema_version": SCHEMA_VERSION,
            "tool": "vibe-briefing",
            "portfolio": brief_json.get("campaign", {}).get("portfolio", ""),
            "glossary_version": glossary_version,
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        },
        **brief_json,
        "connectors": connector_outputs,
        "feedback_log": brief_json.get("feedback_log", []),
    }
