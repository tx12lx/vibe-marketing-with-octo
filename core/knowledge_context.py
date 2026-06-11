"""
core/knowledge_context.py -- Centralised knowledge context builder for all Vibe OCTO agents.

Loads all five knowledge assets at construction time and pre-builds stable context strings
that each agent pins as an ephemeral cached block.  The per-agent strings are only rebuilt
when a KnowledgeContext instance is constructed (once at startup).

Knowledge sources:
  1. semantic_knowledge_index.json  -- 35 GOLD campaigns with ACC-verified summaries
  2. adobe_schema.json              -- 240 views (table names only; columns in agent prompts)
  3. cross_campaign_patterns.json   -- telecom standard patterns
  4. glossary.json                  -- business term definitions
  5. business_rules.json            -- human-verified corrections
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


class KnowledgeContext:
    """Pre-built, per-agent knowledge context strings for Vibe OCTO.

    Loaded once at startup.  All four context strings are built at init time
    and exposed as read-only properties so agents can pin them as cached blocks.
    """

    def __init__(self, artifacts_dir: Path, root_dir: Path) -> None:
        self._artifacts_dir = artifacts_dir
        self._root_dir = root_dir

        # Load all five knowledge assets
        self._campaigns: list[dict] = self._load_campaigns()
        self._patterns: dict = self._load_json_safe(artifacts_dir / "cross_campaign_patterns.json") or {}
        self._glossary: dict = self._load_json_safe(root_dir / "glossary.json") or {}
        self._business_rules: dict = self._load_json_safe(root_dir / "business_rules.json") or {}
        self._adobe_table_names: list[str] = self._load_adobe_table_names()

        # Pre-build cached context strings (one time at startup)
        self._nexus_context: str = self._build_nexus_context()
        self._quant_context: str = self._build_quant_context()
        self._briefing_context: str = self._build_briefing_context()
        self._feedback_context: str = self._build_feedback_context()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def nexus_context(self) -> str:
        return self._nexus_context

    @property
    def quant_context(self) -> str:
        return self._quant_context

    @property
    def briefing_context(self) -> str:
        return self._briefing_context

    @property
    def feedback_context(self) -> str:
        return self._feedback_context

    @property
    def campaign_count(self) -> int:
        return len(self._campaigns)

    def reload_rules(self) -> None:
        """Reload business_rules.json and rebuild agent contexts that depend on it.

        Called by the orchestrator after FeedbackAgent saves a new rule so all
        subsequent calls get the updated rule set without restarting.
        """
        self._business_rules = self._load_json_safe(self._root_dir / "business_rules.json") or {}
        self._nexus_context = self._build_nexus_context()
        self._quant_context = self._build_quant_context()
        self._feedback_context = self._build_feedback_context()

    def get_dynamic_context(
        self,
        query: str,
        camp_id: Optional[str] = None,
        session_corrections: Optional[list] = None,
    ) -> str:
        """Per-request dynamic context (not cached -- changes every call).

        Includes:
          - The user query
          - Session corrections accumulated so far
          - Full campaign context when a specific campaign is matched
        """
        parts: list[str] = [f"User Query: {query}\n"]

        if session_corrections:
            parts.append("\nSession Corrections Applied This Session:\n")
            for correction in session_corrections:
                parts.append(f"  - {correction}\n")

        if camp_id:
            matched = [
                c for c in self._campaigns
                if c.get("camp_id", "").upper() == camp_id.upper()
                or c.get("sub_camp_id", "").upper() == camp_id.upper()
            ]
            if matched:
                parts.append(f"\nCampaign-Specific Context for {camp_id}:\n")
                parts.append(self._format_campaign(matched[0], include_requirements=True))

        return "".join(parts)

    # ------------------------------------------------------------------
    # Context builders -- one per agent role
    # ------------------------------------------------------------------

    def _build_nexus_context(self) -> str:
        """Full knowledge for intent understanding and routing."""
        parts: list[str] = [
            "=== SECTION 1: GOLD CAMPAIGN KNOWLEDGE ===\n\n",
            f"All {len(self._campaigns)} GOLD campaigns with ACC-verified summaries "
            "and extracted brief requirements.\n\n",
        ]

        for campaign in self._campaigns:
            parts.append(self._format_campaign(campaign, include_requirements=True))
            parts.append("\n")

        parts.append("\n=== SECTION 2: BUSINESS RULES ===\n\n")
        parts.append(self._format_business_rules())

        parts.append("\n=== SECTION 3: GLOSSARY ===\n\n")
        parts.append(self._format_glossary())

        parts.append("\n=== SECTION 4: CROSS-CAMPAIGN PATTERNS ===\n\n")
        parts.append(self._format_patterns())

        parts.append("\n=== SECTION 5: ADOBE DATA SCHEMA TABLE NAMES ===\n\n")
        parts.append(f"({len(self._adobe_table_names)} views available in bi-srv-hsmdet-pr-7b9def.adobe)\n")
        for name in self._adobe_table_names:
            parts.append(f"  {name}\n")

        return "".join(parts)

    def _build_quant_context(self) -> str:
        """SQL-generation context: proven column patterns + business rules."""
        parts: list[str] = [
            "=== SECTION 1: PROVEN SQL PATTERNS FROM GOLD CAMPAIGNS ===\n\n",
            "The targeting_summary for each campaign below shows confirmed table/column/filter\n",
            "patterns validated by the ACC team.  Use these as primary SQL generation guidance.\n\n",
        ]

        for campaign in self._campaigns:
            name = campaign.get("campaign_name", campaign.get("camp_id", ""))
            acc = campaign.get("acc_summaries") or {}
            ts = acc.get("targeting_summary", "")

            req_targeting: list[str] = []
            for dep in campaign.get("deployments", []):
                req_targeting.extend(
                    dep.get("extracted_requirements", {}).get("targeting", [])
                )

            if not ts and not req_targeting:
                continue

            parts.append(f"Campaign: {name}\n")
            if ts:
                # First 600 chars contain the key table/column references
                parts.append(f"  Verified SQL Pattern:\n  {ts[:600].replace(chr(10), chr(10) + '  ')}\n")
            if req_targeting:
                parts.append("  Extracted Filter Criteria:\n")
                seen: set[str] = set()
                for f in req_targeting:
                    f = (f or "").strip()[:140]
                    if f and f not in seen:
                        seen.add(f)
                        parts.append(f"    - {f}\n")
                    if len(seen) >= 6:
                        break
            parts.append("\n")

        parts.append("\n=== SECTION 2: BUSINESS RULES FOR SQL GENERATION ===\n\n")
        parts.append(self._format_business_rules())

        return "".join(parts)

    def _build_briefing_context(self) -> str:
        """Brief-generation context: all deployment requirements as templates."""
        parts: list[str] = [
            "=== SECTION 1: GOLD BRIEF TEMPLATES ===\n\n",
            f"All {len(self._campaigns)} GOLD campaigns with full brief requirements.\n",
            "Use these as templates when generating briefs for similar campaigns.\n\n",
        ]

        for campaign in self._campaigns:
            parts.append(self._format_campaign(campaign, include_requirements=True))
            parts.append("\n")

        parts.append("\n=== SECTION 2: CROSS-CAMPAIGN PATTERNS ===\n\n")
        parts.append(self._format_patterns())

        return "".join(parts)

    def _build_feedback_context(self) -> str:
        """Correction-interpretation context: rules + glossary + compact schema."""
        parts: list[str] = [
            "=== SECTION 1: EXISTING BUSINESS RULES ===\n\n",
            "Do not duplicate any rule already listed here.\n\n",
        ]
        parts.append(self._format_business_rules())

        parts.append("\n=== SECTION 2: GLOSSARY ===\n\n")
        parts.append(self._format_glossary())

        parts.append("\n=== SECTION 3: KEY TABLE SCHEMA ===\n\n")
        parts.append(self._format_compact_schema())

        return "".join(parts)

    # ------------------------------------------------------------------
    # Loaders
    # ------------------------------------------------------------------

    def _load_campaigns(self) -> list[dict]:
        path = self._artifacts_dir / "semantic_knowledge_index.json"
        data = self._load_json_safe(path) or {}
        return data.get("campaigns", [])

    def _load_adobe_table_names(self) -> list[str]:
        path = self._artifacts_dir / "adobe_schema.json"
        data = self._load_json_safe(path) or {}
        views = data.get("views", {})
        if isinstance(views, dict):
            return sorted(views.keys())
        # If views is a list of dicts (alternate schema format)
        return sorted(
            v.get("view_name") or v.get("table_name", "")
            for v in views
            if isinstance(v, dict)
        )

    @staticmethod
    def _load_json_safe(path: Path) -> Optional[dict]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Formatters
    # ------------------------------------------------------------------

    def _format_campaign(self, c: dict, include_requirements: bool = True) -> str:
        parts: list[str] = []
        name = c.get("campaign_name", c.get("camp_id", ""))
        parts.append(f"-- Campaign: {name} --\n")
        parts.append(f"Code: {c.get('camp_id', '')} / {c.get('sub_camp_id', '')}\n")
        parts.append(
            f"Medium: {c.get('medium', '')} | Cadence: {c.get('cadence', '')} | "
            f"Tier: {c.get('tier', 'GOLD')}\n"
        )
        purpose = c.get("campaign_purpose", "")
        if purpose:
            parts.append(f"Purpose: {purpose}\n")

        acc = c.get("acc_summaries") or {}
        ts = acc.get("targeting_summary", "")
        ss = acc.get("segment_summary", "")
        if ts:
            parts.append(f"\nACC-Verified Targeting:\n{ts}\n")
        if ss:
            parts.append(f"\nACC-Verified Segmentation:\n{ss}\n")

        if include_requirements:
            for dep in c.get("deployments", []):
                req = dep.get("extracted_requirements") or {}
                dep_name = dep.get("deployment_name") or dep.get("deployment_id", "")
                targeting = [t for t in (req.get("targeting") or []) if (t or "").strip()]
                exclusions = [e for e in (req.get("exclusions") or []) if (e or "").strip()]
                objectives = [o for o in (req.get("business_objectives") or []) if (o or "")]
                ch = req.get("channel_rules") or {}

                if not (targeting or exclusions or objectives):
                    continue

                parts.append(f"\n  Deployment: {dep_name}\n")
                if targeting:
                    parts.append("  Targeting Requirements:\n")
                    for t in targeting[:8]:
                        parts.append(f"    - {str(t)[:150]}\n")
                if exclusions:
                    parts.append("  Exclusion Requirements:\n")
                    for e in exclusions[:5]:
                        parts.append(f"    - {str(e)[:120]}\n")
                if objectives:
                    parts.append("  Business Objectives:\n")
                    for o in objectives[:3]:
                        parts.append(f"    - {str(o)[:100]}\n")
                dnc = ch.get("dnc_flags")
                if dnc:
                    parts.append(f"  DNC Flags: {dnc}\n")

        return "".join(parts)

    def _format_business_rules(self) -> str:
        rules = (self._business_rules or {}).get("rules", [])
        if not rules:
            return "(No business rules saved yet)\n"
        parts: list[str] = []
        for rule in rules:
            scope = rule.get("scope", "?").upper()
            desc = rule.get("rule_description", "")
            rt = rule.get("rule_type", "?")
            sv = rule.get("structured_value") or {}
            parts.append(f"[{scope}] {desc} (type: {rt})\n")
            note = sv.get("note") or sv.get("sql") or sv.get("filter", "")
            if note:
                parts.append(f"  Detail: {str(note)[:200]}\n")
        return "".join(parts)

    def _format_glossary(self) -> str:
        parts: list[str] = []
        glossary = self._glossary or {}

        for term, defn in (glossary.get("acronyms") or {}).items():
            meaning = (defn or {}).get("business_meaning", "")
            if meaning:
                parts.append(f"  {term}: {meaning}\n")

        for term, defn in (glossary.get("business_terms") or {}).items():
            meaning = (defn or {}).get("business_meaning", "")
            if meaning:
                parts.append(f"  {term}: {meaning}\n")

        for term, defn in (glossary.get("user_defined_terms") or {}).items():
            definition = (defn or {}).get("definition", "")
            if definition:
                parts.append(f"  {term}: {definition}\n")

        return "".join(parts) if parts else "(Glossary empty)\n"

    def _format_patterns(self) -> str:
        insights = (self._patterns or {}).get("insights", {})
        parts: list[str] = []

        targeting_patterns = insights.get("targeting_patterns", [])
        if targeting_patterns:
            parts.append("Common targeting patterns across GOLD campaigns:\n")
            for p in targeting_patterns[:12]:
                pattern = p.get("pattern", "")
                freq = p.get("frequency", 0)
                examples = p.get("gold_examples", [])[:2]
                parts.append(f"  '{pattern}' (seen in {freq} campaigns): {', '.join(examples)}\n")

        exclusion_patterns = insights.get("exclusion_patterns", [])
        if exclusion_patterns:
            parts.append("Common exclusion patterns:\n")
            for p in exclusion_patterns[:6]:
                parts.append(
                    f"  '{p.get('pattern', '')}': {p.get('frequency', 0)} campaigns\n"
                )

        return "".join(parts) if parts else "(No patterns)\n"

    def _format_compact_schema(self) -> str:
        parts: list[str] = [
            "Key tables (confirmed columns are in the agent system prompt):\n",
            "  bq_fda_mob_mobility_base            -- primary mobility spine (wireless/mobile)\n",
            "  bq_dly_dbm_customer_profl           -- FFH/Home Solutions customer profile\n",
            "  bq_fda_current_model_score_master_view -- NBA/propensity model scores\n",
            "  gch_current.bq_campaign_segment     -- GCH recency suppression\n",
            "  gch_current.bq_campaign_communication -- GCH recency suppression\n",
            "  gch_current.bq_campaign_description -- GCH recency suppression\n",
        ]
        if self._adobe_table_names:
            parts.append(f"\nAll {len(self._adobe_table_names)} views available in adobe dataset.\n")
        return "".join(parts)
