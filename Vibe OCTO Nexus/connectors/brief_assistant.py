from __future__ import annotations

from .base import BaseConnector

# Fields required for every campaign type
_REQUIRED_BASE = [
    ("campaign.brand", lambda b: bool(b.get("campaign", {}).get("brand"))),
    ("campaign.medium", lambda b: bool(b.get("campaign", {}).get("medium"))),
    ("campaign.cadence", lambda b: bool(b.get("campaign", {}).get("cadence"))),
    ("targeting.include", lambda b: len(b.get("targeting", {}).get("include", [])) > 0),
    ("offer.product", lambda b: bool(b.get("offer", {}).get("product"))),
    ("segmentation.base", lambda b: bool(b.get("segmentation", {}).get("base"))),
]

# Additional fields required for OB campaigns
_REQUIRED_OB = [
    ("vendor.name", lambda b: bool(b.get("vendor", {}).get("name"))),
]

# Additional fields required when vendor is CSS
_REQUIRED_CSS = [
    ("vendor.css_campaign_id", lambda b: bool(b.get("vendor", {}).get("css_campaign_id"))),
    ("vendor.css_measurement_id", lambda b: bool(b.get("vendor", {}).get("css_measurement_id"))),
]

_OB_KEYWORDS = {"ob", "outbound_call", "outbound"}
_CSS_KEYWORDS = {"css"}


def _is_ob(brief_json: dict) -> bool:
    medium = brief_json.get("campaign", {}).get("medium", [])
    return any(m.lower().strip() in _OB_KEYWORDS for m in medium)


def _is_css(brief_json: dict) -> bool:
    vendor_name = (brief_json.get("vendor", {}).get("name") or "").lower()
    return any(k in vendor_name for k in _CSS_KEYWORDS)


class BriefAssistantConnector(BaseConnector):
    """Provides completeness scoring and authoring guidance for business stakeholders.

    Powers a data brief assistant that guides users through creating complete,
    standardized data briefs before submission. Flags missing required fields,
    surfaces ambiguities, and provides specific actionable guidance.

    Always 'ready' — does not depend on BQ mappings.
    """

    name = "brief_assistant"

    def generate(self, brief_json: dict, glossary_data: dict) -> dict:
        flags = brief_json.get("data_brief_flags", {})
        ambiguities = list(flags.get("ambiguities", []))
        missing_fields = list(flags.get("missing_required_fields", []))

        # Evaluate required field checks
        checks: dict[str, bool] = {}
        for field_name, check_fn in _REQUIRED_BASE:
            checks[field_name] = check_fn(brief_json)

        if _is_ob(brief_json):
            for field_name, check_fn in _REQUIRED_OB:
                checks[field_name] = check_fn(brief_json)

        if _is_css(brief_json):
            for field_name, check_fn in _REQUIRED_CSS:
                checks[field_name] = check_fn(brief_json)

        failed_checks = [k for k, v in checks.items() if not v]
        passed = len(checks) - len(failed_checks)
        completeness_score = round((passed / len(checks)) * 100) if checks else 0

        # Build prioritized guidance
        guidance: list[str] = []
        for field in failed_checks:
            guidance.append(f"Required field missing: {field}")
        for field in missing_fields:
            if field not in guidance:
                guidance.append(f"Incomplete: {field}")
        if flags.get("ob_vendor_missing"):
            guidance.append("OB campaign: specify the vendor/dialing site.")
        if flags.get("css_ids_missing"):
            guidance.append("CSS vendor: provide both CSS campaign ID and CSS measurement ID.")
        for amb in ambiguities[:5]:
            guidance.append(f"Clarify: {amb}")

        return {
            "status": "ready",
            "completeness_score": completeness_score,
            "required_fields_check": checks,
            "failed_checks": failed_checks,
            "include_criteria_count": len(brief_json.get("targeting", {}).get("include", [])),
            "exclude_criteria_count": len(brief_json.get("targeting", {}).get("exclude", [])),
            "ambiguities": ambiguities,
            "missing_required_fields": missing_fields,
            "guidance": guidance,
        }
