"""
core/knowledge_context.py -- Centralised knowledge context builder for all Vibe OCTO agents.

v4 design: compact XML base context (always cached) + per-request RAG retrieval of top-3
relevant campaigns.  Total per-request injection ~15K tokens instead of injecting all
campaigns on every call.

Knowledge sources:
  1. semantic_knowledge_index.json  -- campaign records with brief_extraction + conflict_notes
  2. adobe_schema.json              -- views (table names only in base; columns retrieved per-request)
  3. campaign_data_schema.json      -- campaign_data dataset column schemas
  4. gch_current_schema.json        -- GCH suppression table schemas
  5. view_domain_catalog.json       -- domain classifications for 200+ views
  6. cross_campaign_patterns.json   -- telecom standard patterns
  7. glossary.json                  -- business term definitions
  8. business_rules.json            -- human-verified corrections
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import xml.sax.saxutils as sax

_log = logging.getLogger(__name__)

# Freshness threshold: warn if any campaign was ingested more than this many days ago
_FRESHNESS_WARN_DAYS = 7


def _xml_escape(s: str) -> str:
    return sax.escape(str(s or ""))


# ---------------------------------------------------------------------------
# Sparse TF-IDF helpers (pure Python — no numpy dependency)
# ---------------------------------------------------------------------------

import re as _re
from collections import Counter as _Counter


def _tokenize(text: str) -> list[str]:
    return _re.findall(r"[a-z]{3,}", text.lower())


def _tf_vector(text: str) -> dict[str, float]:
    tokens = _tokenize(text)
    if not tokens:
        return {}
    counts = _Counter(tokens)
    total = len(tokens)
    return {t: count / total for t, count in counts.items()}


def _cosine_sim(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a.get(t, 0.0) * w for t, w in b.items())
    mag_a = math.sqrt(sum(v * v for v in a.values()))
    mag_b = math.sqrt(sum(v * v for v in b.values()))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


class KnowledgeContext:
    """Pre-built, per-agent knowledge context strings for Vibe OCTO.

    Compact base context is built once at startup and cached.
    Per-request campaign context is retrieved via RAG (top-3 by TF-IDF cosine similarity).
    Full column schemas are retrieved per-request via retrieve_schema_for_domains().
    """

    def __init__(self, artifacts_dir: Path, root_dir: Path) -> None:
        self._artifacts_dir = artifacts_dir
        self._root_dir = root_dir

        # Load all knowledge assets
        self._campaigns: list[dict] = self._load_campaigns()
        self._patterns: dict = self._load_json_safe(artifacts_dir / "cross_campaign_patterns.json") or {}
        self._glossary: dict = self._load_json_safe(root_dir / "glossary.json") or {}
        self._business_rules: dict = self._load_json_safe(root_dir / "business_rules.json") or {}
        self._adobe_table_names: list[str] = self._load_adobe_table_names()
        self._camp_data_schema: dict = self._load_json_safe(artifacts_dir / "campaign_data_schema.json") or {}
        self._gch_schema: dict = self._load_json_safe(artifacts_dir / "gch_current_schema.json") or {}
        self._domain_catalog: dict = self._load_json_safe(artifacts_dir / "view_domain_catalog.json") or {}
        self._schema_annotations: dict = self._load_json_safe(artifacts_dir / "schema_annotations.json") or {}

        # Build compact base context strings (cached for the session)
        self._nexus_context: str = self._build_nexus_context()
        self._quant_context: str = self._build_quant_context()
        self._briefing_context: str = self._build_briefing_context()
        self._feedback_context: str = self._build_feedback_context()

        # Freshness check
        self._check_freshness()

        # Pre-compute TF-IDF vectors for in-memory RAG fallback
        self._campaign_vectors: list[tuple[dict, dict]] = self._build_campaign_vectors()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def domain_catalog_xml(self) -> str:
        """Compact XML listing of all available data domains, for injecting into AI prompts."""
        return self._format_domain_catalog_xml()

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

    # ------------------------------------------------------------------
    # Per-request RAG retrieval
    # ------------------------------------------------------------------

    def retrieve_campaigns(self, query: str, top_k: int = 3) -> list[dict]:
        """Return top-k campaign records most relevant to the query.

        Tries SQLite embeddings DB first; falls back to in-memory TF-IDF cosine similarity.
        """
        db_path = self._artifacts_dir / "campaign_embeddings.db"
        if db_path.exists():
            try:
                return self._retrieve_from_db(query, top_k, db_path)
            except Exception as exc:
                _log.debug("SQLite retrieval failed, using in-memory fallback: %s", exc)

        return self._retrieve_in_memory(query, top_k)

    def retrieve_campaigns_xml(self, query: str, top_k: int = 3) -> str:
        """Return top-k relevant campaigns formatted as XML for agent prompt injection."""
        campaigns = self.retrieve_campaigns(query, top_k)
        if not campaigns:
            return "<campaigns/>"
        parts = ["<campaigns>"]
        for c in campaigns:
            parts.append(self._campaign_to_xml(c))
        parts.append("</campaigns>")
        return "\n".join(parts)

    def retrieve_schema_for_domains(self, domains: list[str]) -> str:
        """Return XML column schemas for views matching the requested domains.

        Typically 3-5 views, ~800-1600 tokens total.
        Only views whose domain tag appears in `domains` are returned.
        """
        if not domains or not self._domain_catalog:
            return "<execution_schemas/>"

        domain_set = {d.lower() for d in domains}
        catalog_views = self._domain_catalog.get("views", {})

        matching: list[tuple[str, str, dict]] = []  # (view_name, dataset, view_data)
        for view_name, meta in catalog_views.items():
            if meta.get("domain", "").lower() in domain_set:
                dataset = meta.get("dataset", "adobe")
                schema = self._get_view_schema(view_name, dataset)
                if schema:
                    matching.append((view_name, dataset, schema))

        if not matching:
            return "<execution_schemas/>"

        parts = ["<execution_schemas>"]
        annotations = self._schema_annotations
        for view_name, dataset, view_data in matching:
            cols = view_data.get("columns", [])
            view_annots = annotations.get(view_name, {})
            domain = catalog_views.get(view_name, {}).get("domain", "")
            parts.append(
                f'  <view name="{_xml_escape(view_name)}" '
                f'dataset="{_xml_escape(dataset)}" '
                f'domain="{_xml_escape(domain)}">'
            )
            for col in cols:
                col_name = col.get("name", "")
                col_type = col.get("type", "")
                nullable = col.get("nullable", True)
                note = view_annots.get(col_name, "")
                null_attr = ' nullable="false"' if not nullable else ""
                note_attr = f' note="{_xml_escape(note)}"' if note else ""
                parts.append(
                    f'    <col name="{_xml_escape(col_name)}" '
                    f'type="{_xml_escape(col_type)}"{null_attr}{note_attr}/>'
                )
            parts.append("  </view>")
        parts.append("</execution_schemas>")
        return "\n".join(parts)

    def get_domain_for_query(self, query: str) -> list[str]:
        """Infer which schema domains are relevant for a query (best-effort keyword match)."""
        lower = query.lower()
        domains = []
        if any(k in lower for k in ("mobility", "wireless", "mobile", "ban", "postpaid")):
            domains.append("mobility_spine")
        if any(k in lower for k in ("ffh", "home", "internet", "bacct", "home solutions")):
            domains.append("ffh_profile")
        if any(k in lower for k in ("gch", "recency", "suppression", "recent contact")):
            domains.append("gch_suppression")
        if any(k in lower for k in ("model", "score", "propensity", "nba")):
            domains.append("scoring")
        if any(k in lower for k in ("dnc", "opt-out", "consent", "unsubscribe")):
            domains.append("channel_governance")
        if any(k in lower for k in ("eligible", "eligibility", "product")):
            domains.append("product_eligibility")
        # Default: always include mobility_spine for sizing requests
        if not domains:
            domains.append("mobility_spine")
        return domains

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
        """Compact base context for intent understanding and routing.

        Does NOT include all campaigns — those are injected per-request via retrieve_campaigns_xml().
        Total: ~2K tokens (glossary + patterns + rules + domain catalog).
        """
        parts: list[str] = ["<knowledge_base>\n"]

        parts.append("  <metadata>\n")
        parts.append(f"    <total_campaigns>{len(self._campaigns)}</total_campaigns>\n")
        parts.append("    <note>Per-request: top-3 relevant campaigns injected via RAG retrieval</note>\n")
        parts.append("  </metadata>\n\n")

        parts.append("  <gold_insights>\n")
        parts.append("    <standard_rules>\n")
        for rule in ("active_subscriber_filter", "dnc_suppression", "control_group_exclusion"):
            parts.append(f'      <rule name="{rule}" scope="universal"/>\n')
        parts.append("    </standard_rules>\n")
        parts.append(self._format_patterns_xml())
        parts.append("  </gold_insights>\n\n")

        parts.append("  <glossary>\n")
        parts.append(self._format_glossary_xml())
        parts.append("  </glossary>\n\n")

        parts.append("  <business_rules>\n")
        parts.append(self._format_business_rules_xml())
        parts.append("  </business_rules>\n\n")

        parts.append("  <view_domain_catalog>\n")
        parts.append(self._format_domain_catalog_xml())
        parts.append("  </view_domain_catalog>\n")

        parts.append("</knowledge_base>")
        return "".join(parts)

    def _build_quant_context(self) -> str:
        """Compact SQL context: business rules + view domain catalog.

        Column schemas injected per-request via retrieve_schema_for_domains().
        """
        parts: list[str] = ["<quant_knowledge>\n"]
        parts.append("  <business_rules>\n")
        parts.append(self._format_business_rules_xml())
        parts.append("  </business_rules>\n\n")
        parts.append("  <view_domain_catalog>\n")
        parts.append(self._format_domain_catalog_xml())
        parts.append("  </view_domain_catalog>\n")
        parts.append("</quant_knowledge>")
        return "".join(parts)

    def _build_briefing_context(self) -> str:
        """Compact briefing context: patterns + glossary.

        Campaign-specific context injected per-request via retrieve_campaigns_xml().
        """
        parts: list[str] = ["<briefing_knowledge>\n"]
        parts.append("  <patterns>\n")
        parts.append(self._format_patterns_xml())
        parts.append("  </patterns>\n\n")
        parts.append("  <glossary>\n")
        parts.append(self._format_glossary_xml())
        parts.append("  </glossary>\n")
        parts.append("</briefing_knowledge>")
        return "".join(parts)

    def _build_feedback_context(self) -> str:
        """Correction-interpretation context: rules + glossary + compact schema."""
        parts: list[str] = ["<feedback_knowledge>\n"]
        parts.append("  <note>Do not duplicate any rule already listed here.</note>\n\n")
        parts.append("  <existing_business_rules>\n")
        parts.append(self._format_business_rules_xml())
        parts.append("  </existing_business_rules>\n\n")
        parts.append("  <glossary>\n")
        parts.append(self._format_glossary_xml())
        parts.append("  </glossary>\n\n")
        parts.append("  <key_tables>\n")
        parts.append(self._format_compact_schema_xml())
        parts.append("  </key_tables>\n")
        parts.append("</feedback_knowledge>")
        return "".join(parts)

    # ------------------------------------------------------------------
    # Freshness check
    # ------------------------------------------------------------------

    def _check_freshness(self) -> None:
        stale_count = 0
        now = datetime.now(tz=timezone.utc)
        for c in self._campaigns:
            ts = c.get("last_ingested_at") or c.get("ingested_at", "")
            if not ts:
                continue
            try:
                ingested = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if ingested.tzinfo is None:
                    ingested = ingested.replace(tzinfo=timezone.utc)
                age_days = (now - ingested).days
                if age_days > _FRESHNESS_WARN_DAYS:
                    stale_count += 1
            except Exception:
                pass
        if stale_count > 0:
            _log.debug(
                "%d campaign(s) have stale knowledge (>%d days)",
                stale_count, _FRESHNESS_WARN_DAYS,
            )

    # ------------------------------------------------------------------
    # RAG retrieval helpers
    # ------------------------------------------------------------------

    def _build_campaign_vectors(self) -> list[tuple[dict, dict]]:
        """Pre-compute in-memory TF-IDF vectors for all campaigns."""
        result: list[tuple[dict, dict]] = []
        for c in self._campaigns:
            acc = c.get("acc_summaries") or {}
            be = c.get("brief_extraction") or {}
            text = " ".join(filter(None, [
                c.get("campaign_name", ""),
                c.get("campaign_purpose", ""),
                acc.get("targeting_summary", ""),
                acc.get("segment_summary", ""),
                be.get("campaign_strategy_summary", ""),
                " ".join(be.get("exclusion_rules") or []),
            ]))
            vec = _tf_vector(text)
            result.append((c, vec))
        return result

    def _retrieve_in_memory(self, query: str, top_k: int) -> list[dict]:
        q_vec = _tf_vector(query)
        scored = [
            (_cosine_sim(q_vec, vec), c)
            for c, vec in self._campaign_vectors
        ]
        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:top_k]]

    def _retrieve_from_db(self, query: str, top_k: int, db_path: Path) -> list[dict]:
        import sqlite3
        q_vec = _tf_vector(query)
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT campaign_key, embedding_json FROM campaign_embeddings"
            ).fetchall()
        finally:
            conn.close()

        # Build campaign lookup by deployment key (matches embeddings DB key format)
        camp_lookup = {
            f"{c.get('camp_id', '')}::{c.get('sub_camp_id', '')}::{c.get('medium', '')}::{c.get('cadence', '')}": c
            for c in self._campaigns
        }

        scored: list[tuple[float, dict]] = []
        for campaign_key, emb_json in rows:
            c = camp_lookup.get(campaign_key)
            if c is None:
                continue
            try:
                c_vec: dict[str, float] = json.loads(emb_json)
                sim = _cosine_sim(q_vec, c_vec)
                scored.append((sim, c))
            except Exception:
                continue

        scored.sort(key=lambda x: -x[0])
        return [c for _, c in scored[:top_k]]

    # ------------------------------------------------------------------
    # XML formatting helpers
    # ------------------------------------------------------------------

    def _campaign_to_xml(self, c: dict) -> str:
        acc = c.get("acc_summaries") or {}
        be = c.get("brief_extraction") or {}
        conflict_notes = c.get("conflict_notes") or []
        camp_id = _xml_escape(c.get("camp_id", ""))
        parts = [f'  <campaign id="{camp_id}">']
        parts.append(f'    <name>{_xml_escape(c.get("campaign_name", ""))}</name>')
        parts.append(f'    <medium>{_xml_escape(c.get("medium", ""))}</medium>')
        parts.append(f'    <tier>{_xml_escape(c.get("tier", "GOLD"))}</tier>')

        ts = acc.get("targeting_summary", "")
        if ts:
            parts.append(f'    <targeting>{_xml_escape(ts[:800])}</targeting>')

        ss = acc.get("segment_summary", "")
        if ss:
            parts.append(f'    <segmentation>{_xml_escape(ss[:400])}</segmentation>')

        strategy = be.get("campaign_strategy_summary", "")
        if strategy:
            parts.append(f'    <strategy>{_xml_escape(strategy)}</strategy>')

        excl_rules = be.get("exclusion_rules", [])
        if excl_rules:
            parts.append("    <exclusions>")
            for rule in excl_rules[:8]:
                parts.append(f'      <rule>{_xml_escape(rule)}</rule>')
            parts.append("    </exclusions>")

        if conflict_notes:
            parts.append("    <conflict_notes>")
            for note in conflict_notes:
                parts.append(f'      <note>{_xml_escape(note)}</note>')
            parts.append("    </conflict_notes>")

        parts.append("  </campaign>")
        return "\n".join(parts)

    def _get_view_schema(self, view_name: str, dataset: str) -> Optional[dict]:
        """Return the schema dict for a view from the appropriate schema artifact."""
        for schema_data in [self._camp_data_schema, self._gch_schema]:
            views = schema_data.get("views", {})
            if view_name in views:
                return views[view_name]
        # Also check adobe schema (legacy)
        adobe_path = self._artifacts_dir / "adobe_schema.json"
        adobe_data = self._load_json_safe(adobe_path) or {}
        views = adobe_data.get("views", {})
        if view_name in views:
            return views[view_name]
        return None

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

    def _format_business_rules_xml(self) -> str:
        rules = (self._business_rules or {}).get("rules", [])
        if not rules:
            return "    <!-- No business rules saved yet -->\n"
        parts: list[str] = []
        for rule in rules:
            scope = _xml_escape(rule.get("scope", "?").upper())
            desc = _xml_escape(rule.get("rule_description", ""))
            rt = _xml_escape(rule.get("rule_type", "?"))
            sv = rule.get("structured_value") or {}
            note = _xml_escape(str(sv.get("note") or sv.get("sql") or sv.get("filter", ""))[:200])
            detail = f' detail="{note}"' if note else ""
            parts.append(f'    <rule scope="{scope}" type="{rt}"{detail}>{desc}</rule>\n')
        return "".join(parts)

    def _format_glossary_xml(self) -> str:
        parts: list[str] = []
        glossary = self._glossary or {}
        for term, defn in (glossary.get("acronyms") or {}).items():
            meaning = (defn or {}).get("business_meaning", "")
            if meaning:
                parts.append(f'    <term name="{_xml_escape(term)}"><meaning>{_xml_escape(meaning)}</meaning></term>\n')
        for term, defn in (glossary.get("business_terms") or {}).items():
            meaning = (defn or {}).get("business_meaning", "")
            if meaning:
                parts.append(f'    <term name="{_xml_escape(term)}"><meaning>{_xml_escape(meaning)}</meaning></term>\n')
        for term, defn in (glossary.get("user_defined_terms") or {}).items():
            definition = (defn or {}).get("definition", "")
            if definition:
                parts.append(f'    <term name="{_xml_escape(term)}"><meaning>{_xml_escape(definition)}</meaning></term>\n')
        return "".join(parts) if parts else "    <!-- Glossary empty -->\n"

    def _format_patterns_xml(self) -> str:
        insights = (self._patterns or {}).get("insights", {})
        parts: list[str] = []
        for p in (insights.get("targeting_patterns") or [])[:12]:
            pattern = _xml_escape(p.get("pattern", ""))
            freq = p.get("frequency", 0)
            parts.append(f'    <targeting_pattern frequency="{freq}">{pattern}</targeting_pattern>\n')
        for p in (insights.get("exclusion_patterns") or [])[:6]:
            pattern = _xml_escape(p.get("pattern", ""))
            freq = p.get("frequency", 0)
            parts.append(f'    <exclusion_pattern frequency="{freq}">{pattern}</exclusion_pattern>\n')
        return "".join(parts) if parts else "    <!-- No patterns -->\n"

    def _format_domain_catalog_xml(self) -> str:
        """Compact view domain catalog for base context (~800 tokens for 200+ views)."""
        views = (self._domain_catalog or {}).get("views", {})
        if not views:
            return "    <!-- Domain catalog not yet generated — run --refresh-schema-only -->\n"
        parts: list[str] = []
        for view_name, meta in sorted(views.items()):
            domain = _xml_escape(meta.get("domain", "general"))
            desc = _xml_escape(meta.get("description", ""))
            dataset = _xml_escape(meta.get("dataset", ""))
            parts.append(
                f'    <view name="{_xml_escape(view_name)}" dataset="{dataset}" '
                f'domain="{domain}">{desc}</view>\n'
            )
        return "".join(parts)

    def _format_compact_schema_xml(self) -> str:
        return (
            '    <view name="bq_fda_mob_mobility_base" domain="mobility_spine">'
            'Primary mobility subscriber base</view>\n'
            '    <view name="bq_dly_dbm_customer_profl" domain="ffh_profile">'
            'Home Solutions customer profile</view>\n'
            '    <view name="bq_fda_current_model_score_master_view" domain="scoring">'
            'NBA and propensity model scores</view>\n'
            '    <view name="bq_campaign_segment" domain="gch_suppression">'
            'GCH recency suppression</view>\n'
        )

    # ------------------------------------------------------------------
    # Legacy text formatters (retained for backward compatibility with
    # any code that calls them directly)
    # ------------------------------------------------------------------

    def _format_business_rules(self) -> str:
        return self._format_business_rules_xml()

    def _format_glossary(self) -> str:
        return self._format_glossary_xml()

    def _format_patterns(self) -> str:
        return self._format_patterns_xml()

    def _format_compact_schema(self) -> str:
        return self._format_compact_schema_xml()
