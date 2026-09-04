"""knowledge/context.py -- the one front door every agent goes through to
reach the knowledge layer.

KnowledgeContext implements exactly the methods agents/nexus_agent.py,
agents/quant_agent.py, and vibe_orchestrator.py already call defensively
(they were written against the old, now-deleted knowledge layer, but every
call site is guarded with "if knowledge_ctx is not None"). Passing a real
instance of this class into those same setters is what turns the sizing
pipeline's knowledge back on -- nothing about the agents themselves needs to
change.

The same instance also fills the runtime's rules_registry slot -- rather than
reviving two separate legacy classes, one object is the single front door for
both "knowledge to read" and "rules to persist", consistent with the plan's
"one clear front door" goal.

Nothing here caches in memory: every call reads straight from the SQLite
store, which is fast enough at this corpus size and means reload_rules() has
nothing to do -- a correction is visible to the very next query, restart or
not, with no separate cache-invalidation step to get wrong.
"""
from __future__ import annotations

import logging
from typing import Optional

from knowledge import retrieve
from knowledge.store import add_business_rule, connect, record_feedback_event

_log = logging.getLogger(__name__)

_DOMAIN_SCHEMA_FILTERING_NOTE = (
    "Domain-based schema filtering across multiple tables is deferred to the full "
    "knowledge-layer cutover (Step 4) -- with only one table synced today it has "
    "nothing to narrow down yet."
)


class KnowledgeContext:
    """The single front door to the knowledge layer, for both agents to read
    from and the web/Slack front ends to persist confirmed feedback through."""

    # -- read side, used by NexusAgent and QuantAgent ------------------------

    def retrieve_campaigns_xml(self, query: str, top_k: int = 5) -> str:
        matches = retrieve.find_similar_campaigns(query, top_k=top_k)
        if not matches:
            return "<campaigns>(no past campaigns stored yet)</campaigns>"
        items = "\n".join(
            f'  <campaign code="{m["campaign_code"]}" similarity="{m["similarity"]:.2f}">'
            f'{m["summary_text"]}</campaign>'
            for m in matches
        )
        return f"<campaigns>\n{items}\n</campaigns>"

    @property
    def campaign_count(self) -> int:
        with connect() as conn:
            return len({s["campaign_code"] for s in retrieve.get_all_campaign_summaries(conn)})

    @property
    def nexus_context(self) -> str:
        return (
            "GLOSSARY\n" + retrieve.get_glossary_summary() + "\n\n"
            "CONFIRMED BUSINESS RULES\n" + retrieve.get_active_rules_text()
        )

    @property
    def quant_context(self) -> str:
        return (
            "CONFIRMED BUSINESS RULES (apply these when writing SQL)\n"
            + retrieve.get_active_rules_text()
        )

    def get_dynamic_context(self, query: str, session_corrections: Optional[list] = None) -> str:
        lines = []
        if session_corrections:
            lines.append("CORRECTIONS FROM THIS SESSION:")
            lines.extend(f"- {c}" for c in session_corrections)
        return "\n".join(lines)

    def retrieve_schema_for_domains(self, domains: list[str]) -> str:
        _log.debug(_DOMAIN_SCHEMA_FILTERING_NOTE)
        return ""

    def reload_rules(self) -> None:
        pass  # nothing is cached -- every read already goes straight to the database

    # -- write side, used by api/web_app.py's HITL/correction persistence ----

    def add_rule(self, rule) -> None:
        with connect() as conn:
            rule_id = add_business_rule(
                conn,
                rule_text=rule.rule_description,
                scope=rule.scope,
                campaign_code=rule.campaign_code,
                added_by=rule.verified_by,
            )
            record_feedback_event(
                conn,
                event_type="correction",
                campaign_code=rule.campaign_code,
                raw_text=rule.raw_correction,
                structured_rule_id=rule_id,
                user_identity=rule.verified_by,
            )

    def record_confirmation(self, campaign_code: Optional[str], user_identity: str = "unknown") -> None:
        """Permanently record a 'looks good' confirmation -- called from
        api/web_app.py's _handle_hitl_yes_sync, shared by the web and Slack
        front ends."""
        with connect() as conn:
            record_feedback_event(
                conn,
                event_type="confirm",
                campaign_code=campaign_code,
                user_identity=user_identity,
            )

    def _load(self) -> None:
        pass  # nothing is cached in memory to reload

    # -- rules-registry-shaped surface, used by vibe_orchestrator.route_by_intent --
    # Applying rule *effects* to a spec (filters/exclusions) is real business-rule
    # engine work, deferred to the full Step 4 cutover -- these keep every
    # currently-guarded call site safe rather than crashing now that this
    # object is no longer None, without pretending to apply effects that
    # haven't been built yet.

    _rules: list = []
    last_applied_ids: list = []

    def get_rules_for_execution(self, camp_id=None, medium=None, cadence=None, **_kwargs) -> list:
        return []

    def get_display_summary(self, rules: list) -> str:
        return ""

    def apply_rules_to_spec(self, spec, rules: list):
        return spec

    def record_rules_applied(self, rules: list) -> None:
        pass

    def record_hitl_yes(self, rule_ids: list) -> None:
        pass
