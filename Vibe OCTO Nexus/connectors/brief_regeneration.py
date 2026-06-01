from __future__ import annotations

from .base import BaseConnector


class BriefRegenerationConnector(BaseConnector):
    """Structures brief content for clean brief regeneration.

    Produces a normalized, section-by-section view of the campaign brief
    that a regeneration tool can use to produce a clean, standardized
    data brief document — stripping noise, fixing inconsistencies, and
    applying standard language across all campaigns.

    Always 'ready' — does not depend on BQ mappings.
    """

    name = "brief_regeneration"

    def generate(self, brief_json: dict, glossary_data: dict) -> dict:
        campaign = brief_json.get("campaign", {})
        targeting = brief_json.get("targeting", {})
        defaults = brief_json.get("default_exclusions", {})

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
                "offer": brief_json.get("offer", {}),
                "segmentation": brief_json.get("segmentation", {}),
                "vendor": brief_json.get("vendor", {}),
                "channel_rules": brief_json.get("channel", {}),
                "target_audience": [
                    {
                        "criterion": c.get("term"),
                        "description": c.get("logic"),
                        "threshold": c.get("threshold"),
                        "confidence_score": c.get("confidence_score"),
                    }
                    for c in targeting.get("include", [])
                ],
                "campaign_exclusions": [
                    {
                        "criterion": c.get("term"),
                        "description": c.get("logic"),
                        "threshold": c.get("threshold"),
                        "lookback_days": c.get("lookback_days"),
                        "confidence_score": c.get("confidence_score"),
                    }
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
