"""knowledge/context.py -- the one front door every agent and API layer goes
through to reach the knowledge layer.

One KnowledgeContext instance, built once in vibe_orchestrator.build_runtime()
and bound into every agent via set_knowledge_context(), is the single object
for both "knowledge to read" (schema, glossary, business rules, campaigns) and
"feedback to persist" (confirmations, corrections, rule approvals) -- see
VibeRuntime.knowledge_ctx.

Nothing here caches in memory: every call reads straight from the SQLite
store, which is fast enough at this corpus size and means reload_rules() has
nothing to do -- a correction is visible to the very next query, restart or
not, with no separate cache-invalidation step to get wrong.
"""
from __future__ import annotations

import logging
from typing import Optional

from knowledge import git_store, retrieve
from knowledge.store import (
    add_business_rule,
    add_glossary_term,
    approve_rule,
    connect,
    get_all_business_rules,
    get_all_campaign_summaries,
    get_feedback_events,
    get_glossary_terms,
    get_pending_rules,
    hydrate_business_rules,
    hydrate_campaign_summaries,
    hydrate_feedback_events,
    hydrate_glossary_terms,
    record_feedback_event,
    reject_rule,
)

# Rules scoped this way govern every future user's results, not just the one
# campaign the submitter is already working on -- they are staged for a
# second person's approval rather than taking effect immediately. A
# 'campaign'-scoped rule stays immediate: its blast radius is the same
# campaign the submitter is already working on right now.
_SCOPES_REQUIRING_REVIEW = frozenset({"universal", "pattern"})

_log = logging.getLogger(__name__)


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
    def glossary_summary(self) -> str:
        """Compact one-line-per-term glossary listing, for prompts that need a
        short reference rather than the full nexus_context/feedback_context block."""
        return retrieve.get_glossary_summary()

    @property
    def campaign_count(self) -> int:
        with connect() as conn:
            return len({s["campaign_code"] for s in retrieve.get_all_campaign_summaries(conn)})

    @property
    def nexus_context(self) -> str:
        return (
            "TABLE SCHEMA (use only these real column names)\n" + retrieve.get_table_schema_text() + "\n\n"
            "GLOSSARY\n" + retrieve.get_glossary_summary() + "\n\n"
            "CONFIRMED BUSINESS RULES\n" + retrieve.get_active_rules_text()
        )

    @property
    def quant_context(self) -> str:
        return (
            "CONFIRMED BUSINESS RULES (apply these when writing SQL)\n"
            + retrieve.get_active_rules_text()
        )

    @property
    def feedback_context(self) -> str:
        return (
            "TABLE SCHEMA (use only these real column names)\n" + retrieve.get_table_schema_text() + "\n\n"
            "GLOSSARY\n" + retrieve.get_glossary_summary() + "\n\n"
            "EXISTING CONFIRMED BUSINESS RULES\n" + retrieve.get_active_rules_text()
        )

    def get_dynamic_context(
        self,
        query: str,
        session_corrections: Optional[list] = None,
        transcript: Optional[list] = None,
    ) -> str:
        """Everything about THIS session (not the permanent knowledge base) that the
        current turn should be aware of: what's been asked and answered so far, and
        any corrections confirmed along the way. Injected into Nexus/Quant's prompts
        via set_session_context() -- see vibe_orchestrator.process_core_request()."""
        lines = []
        if transcript:
            lines.append(
                "RECENT CONVERSATION IN THIS SESSION (most recent last) -- use this to resolve "
                "a follow-up question that refers back to an earlier one (e.g. \"what about "
                "Quebec instead?\", \"same thing but for email\") instead of treating it as a "
                "cold, standalone request:"
            )
            for turn in transcript:
                lines.append(f'- Asked: "{turn["query"]}"')
                lines.append(f"  Answered: {turn['answer']}")
        if session_corrections:
            lines.append("CORRECTIONS FROM THIS SESSION:")
            lines.extend(f"- {c}" for c in session_corrections)
        return "\n".join(lines)

    def reload_rules(self) -> None:
        pass  # nothing is cached -- every read already goes straight to the database

    def hydrate_from_github(self) -> None:
        """Rebuild the local cache from the durable GitHub-backed copy -- call once at
        startup. A no-op if GITHUB_TOKEN isn't set (see knowledge/git_store.py), so this
        is always safe to call regardless of environment. Safe to call repeatedly."""
        if not git_store.is_configured():
            _log.info("GITHUB_TOKEN not set -- knowledge layer is local-only this run.")
            return
        try:
            data = git_store.read_all()
        except Exception:
            _log.exception("Could not read knowledge from GitHub -- starting with an empty local cache.")
            return
        with connect() as conn:
            hydrate_business_rules(conn, data.get("business_rules", []))
            hydrate_glossary_terms(conn, data.get("glossary_terms", []))
            hydrate_feedback_events(conn, data.get("feedback_events", []))
            hydrate_campaign_summaries(conn, data.get("campaign_summaries", []))
        _log.info(
            "Knowledge layer hydrated from GitHub: %d business rule(s), %d glossary term(s), "
            "%d feedback event(s), %d campaign summary/summaries.",
            len(data.get("business_rules", [])), len(data.get("glossary_terms", [])),
            len(data.get("feedback_events", [])), len(data.get("campaign_summaries", [])),
        )

    def _sync_business_rules_and_feedback(self, conn, message: str) -> None:
        """Queue a background commit of the current, full business_rules and
        feedback_events tables -- called after any write to either, since add_rule()
        touches both (see below)."""
        git_store.queue_sync("business_rules", [dict(r) for r in get_all_business_rules(conn)], message)
        git_store.queue_sync("feedback_events", [dict(r) for r in get_feedback_events(conn)], message)

    # -- write side, used by api/web_app.py's HITL/correction persistence ----

    def add_rule(self, rule) -> dict:
        """Save a rule extracted from a HITL correction.

        Returns {"rule_id": int, "status": "active" | "pending_review"} so the
        caller can tell the submitter which happened -- a 'campaign'-scoped
        rule (contained to the one campaign they're already working on) goes
        live immediately; a 'pattern' or 'universal' rule (governs every
        future user's results) is staged as pending_review until a different
        person calls approve_rule() on it.
        """
        status = "pending_review" if rule.scope in _SCOPES_REQUIRING_REVIEW else "active"
        with connect() as conn:
            rule_id = add_business_rule(
                conn,
                rule_text=rule.rule_description,
                scope=rule.scope,
                campaign_code=rule.campaign_code,
                added_by=rule.verified_by,
                status=status,
            )
            record_feedback_event(
                conn,
                event_type="correction",
                campaign_code=rule.campaign_code,
                raw_text=rule.raw_correction,
                structured_rule_id=rule_id,
                user_identity=rule.verified_by,
            )
            self._sync_business_rules_and_feedback(conn, f"New rule #{rule_id} ({rule.scope}) from {rule.verified_by}")
        return {"rule_id": rule_id, "status": status}

    def get_pending_rules(self) -> list[dict]:
        """Every rule awaiting a second reviewer's approval, as plain dicts."""
        with connect() as conn:
            return [dict(r) for r in get_pending_rules(conn)]

    def approve_rule(self, rule_id: int, approver_identity: str, note: str = "") -> None:
        """Move a pending rule to active. Raises knowledge.store.MakerCheckerViolation
        if approver_identity is the same person who submitted it -- this is the
        maker-checker gate, enforced here in code rather than left to convention."""
        with connect() as conn:
            approve_rule(conn, rule_id, approved_by=approver_identity, note=note)
            self._sync_business_rules_and_feedback(conn, f"Approve rule #{rule_id} (by {approver_identity})")

    def reject_rule(self, rule_id: int, approver_identity: str, note: str = "") -> None:
        with connect() as conn:
            reject_rule(conn, rule_id, approved_by=approver_identity, note=note)
            self._sync_business_rules_and_feedback(conn, f"Reject rule #{rule_id} (by {approver_identity})")

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
            git_store.queue_sync(
                "feedback_events", [dict(r) for r in get_feedback_events(conn)],
                f"Confirmation from {user_identity}" + (f" on {campaign_code}" if campaign_code else ""),
            )

    def add_glossary_term(self, term: str, definition: str, added_by: str = "unknown", source: str = "hitl") -> None:
        """Add a glossary term and durably persist it. Not currently called from any
        HITL flow (glossary is seeded, not learned, today) -- added so the write path
        exists once the tool starts learning new terms from conversation, per this
        knowledge layer being a first-class, growing component rather than a fixed seed."""
        with connect() as conn:
            add_glossary_term(conn, term=term, definition=definition, added_by=added_by, source=source)
            git_store.queue_sync(
                "glossary_terms", [dict(r) for r in get_glossary_terms(conn)],
                f"New glossary term '{term}' from {added_by}",
            )

    def _load(self) -> None:
        pass  # nothing is cached in memory to reload
