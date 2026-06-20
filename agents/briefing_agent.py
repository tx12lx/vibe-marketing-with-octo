"""
Vibe OCTO Briefing — BriefingAgent Worker
Worker ID: briefing_v1

Generates structured data brief targeting criteria by analyzing all related
campaigns in the knowledge layer, then producing:

  Phase 1 — Targeting Criteria:
    An initial universe sentence + an ordered list of exclusion criteria.
    GOLD path: few-shot inference from all verified GoldCampaignRecords.
    BRONZE path: zero-shot inference from live schema context.

  Phase 2 — Refinement (optional):
    User can request changes to the targeting criteria. The agent updates
    and returns revised criteria within the same session.

  Phase 3 — Segmentation (optional):
    User requests segmentation criteria. The agent generates named,
    strictly mutually exclusive segments with no new exclusions.

Output: BriefingOutput with structured_universe, structured_exclusions,
and optionally structured_segments populated.
"""
from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import requests
from dotenv import load_dotenv
from core.resilience import resilient_post

if TYPE_CHECKING:
    from core.knowledge_context import KnowledgeContext

_AGENTS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _AGENTS_DIR.parent
_BRIEFING_DIR = _ROOT_DIR / "Vibe OCTO Briefing"

for _p in [str(_ROOT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

load_dotenv(_BRIEFING_DIR / ".env")

from pydantic_schemas import BriefingOutput, SegmentCriterion, UniversalJSONSpec  # noqa: E402
from core.base_agent import BaseAgent  # noqa: E402
from knowledge_base.tier_index import GoldTierIndex, GoldCampaignRecord  # noqa: E402
from core.thought_display import ThoughtDisplay  # noqa: E402

_FUELIX_BASE = "https://api.fuelix.ai"

# ---------------------------------------------------------------------------
# System prompt — identity anchor
# ---------------------------------------------------------------------------

_BRIEFING_SYSTEM = (
    "You are Vibe OCTO Briefing, a senior telecom data analyst for TELUS and Koodo. "
    "You generate precise, structured data brief targeting criteria by studying the full "
    "knowledge library of existing campaigns. Your output must be grounded in patterns "
    "observed across all related campaigns — not invented, not generic. "
    "Never reference SQL, BigQuery, table names, column names, or any technical "
    "execution details anywhere in your output."
)

# ---------------------------------------------------------------------------
# Structured output delimiters — parsed by the agent after each API call
# ---------------------------------------------------------------------------

_TC_BEGIN = "---TARGETING_CRITERIA_BEGIN---"
_TC_END = "---TARGETING_CRITERIA_END---"
_SEG_BEGIN = "---SEGMENTATION_CRITERIA_BEGIN---"
_SEG_END = "---SEGMENTATION_CRITERIA_END---"

_TC_BLOCK_RE = re.compile(
    r"---TARGETING_CRITERIA_BEGIN---\n(.*?)\n---TARGETING_CRITERIA_END---",
    re.DOTALL,
)
_SEG_BLOCK_RE = re.compile(
    r"---SEGMENTATION_CRITERIA_BEGIN---\n(.*?)\n---SEGMENTATION_CRITERIA_END---",
    re.DOTALL,
)
_KNOWLEDGE_BLOCK_RE = re.compile(
    r"---KNOWLEDGE_SOURCES_BEGIN---\n(.*?)\n---KNOWLEDGE_SOURCES_END---\n?",
    re.DOTALL,
)

# ---------------------------------------------------------------------------
# Targeting criteria output rules — injected into every generation call
# ---------------------------------------------------------------------------

_TC_OUTPUT_RULES = (
    "\n\nSTRICT OUTPUT RULES:\n"
    "1. Begin with a knowledge sources block in EXACTLY this format:\n"
    "---KNOWLEDGE_SOURCES_BEGIN---\n"
    "- [campaign name or source]: [one sentence why this source was relevant]\n"
    "---KNOWLEDGE_SOURCES_END---\n\n"
    "2. Then output the targeting criteria in EXACTLY this format — no other text:\n"
    f"{_TC_BEGIN}\n"
    "UNIVERSE: [One precise sentence describing the initial customer population before any exclusions. "
    "Name the product line, customer type, geography, and key eligibility condition.]\n"
    "EXCLUSION 1: [One precise sentence. Start with 'Exclude'.]\n"
    "EXCLUSION 2: [One precise sentence. Start with 'Exclude'.]\n"
    "EXCLUSION N: [Continue numbering until all exclusions are listed.]\n"
    f"{_TC_END}\n\n"
    "3. Rules:\n"
    "- The UNIVERSE sentence must stand alone — it must fully describe who qualifies without "
    "referencing the exclusions.\n"
    "- Each EXCLUSION must be a complete, self-contained sentence. Never combine two exclusions "
    "into one line.\n"
    "- Always include standard telecom exclusions: DNC for the channel, control group, and GCH "
    "recency suppression (with the lookback window if known).\n"
    "- Never reference SQL, BigQuery, table names, column names, or any technical execution detail.\n"
    "- Do not write any text outside the two blocks above.\n"
)

# ---------------------------------------------------------------------------
# Segmentation output rules
# ---------------------------------------------------------------------------

_SEG_OUTPUT_RULES = (
    "\n\nSTRICT SEGMENTATION RULES:\n"
    "1. Begin with a knowledge sources block:\n"
    "---KNOWLEDGE_SOURCES_BEGIN---\n"
    "- [source]: [relevance]\n"
    "---KNOWLEDGE_SOURCES_END---\n\n"
    "2. Then output segments in EXACTLY this format:\n"
    f"{_SEG_BEGIN}\n"
    "SEGMENT 1 - [Segment Name]: [One precise sentence describing exactly who qualifies for this segment.]\n"
    "SEGMENT 2 - [Segment Name]: [One precise sentence.]\n"
    "SEGMENT N - [Segment Name]: [Continue until all segments are listed.]\n"
    f"{_SEG_END}\n\n"
    "3. Rules:\n"
    "- CRITICAL: No customer can fall into more than one segment. All segments must be strictly "
    "mutually exclusive. If criteria could overlap, rewrite until they do not.\n"
    "- CRITICAL: Do NOT add any new exclusion criteria. Segments may only subdivide the "
    "already-confirmed targeting universe. If the user's request implies a new exclusion, "
    "flag it explicitly and refuse to apply it.\n"
    "- Every customer in the confirmed universe must belong to exactly one segment — "
    "add a catch-all segment (e.g. 'All Others') if needed to ensure complete coverage.\n"
    "- Never reference SQL, BigQuery, table names, or column names.\n"
    "- Do not write any text outside the two blocks above.\n"
)


class BriefingAgent(BaseAgent):
    """Pillar 4 Worker B — structured data brief targeting criteria generation.

    Implements the BaseAgent contract:
      subscribe(spec)  -> stores the UniversalJSONSpec
      execute()        -> generates targeting criteria and returns BriefingOutput

    Additional interactive methods:
      refine_targeting(universe, exclusions, correction) -> updated BriefingOutput
      generate_segments(universe, exclusions, basis)     -> BriefingOutput with segments
    """

    WORKER_ID = "briefing_v1"
    HANDLED_INTENTS: frozenset[str] = frozenset({"brief_generation", "brief_qa", "campaign_execution"})
    INPUT_SCHEMA = UniversalJSONSpec
    OUTPUT_SCHEMA = BriefingOutput

    def __init__(self) -> None:
        self._api_key: Optional[str] = os.getenv("FUELIX_API_KEY")
        self._model: str = os.getenv("FUELIX_MODEL", "claude-sonnet-4")
        self._spec: Optional[UniversalJSONSpec] = None
        self._runtime_schema: str = ""
        self._gold_index: Optional[GoldTierIndex] = None
        self._knowledge_ctx: Optional["KnowledgeContext"] = None
        self._non_interactive: bool = False

    # ------------------------------------------------------------------
    # Injection points called by the orchestrator
    # ------------------------------------------------------------------

    def set_knowledge_context(self, ctx: "KnowledgeContext") -> None:
        self._knowledge_ctx = ctx

    def set_gold_index(self, gold_index: GoldTierIndex) -> None:
        self._gold_index = gold_index

    def set_runtime_schema(self, schema_str: str) -> None:
        self._runtime_schema = schema_str

    def set_non_interactive(self) -> None:
        """Skip all terminal display calls -- used for web/API mode."""
        self._non_interactive = True

    def set_session_context(self, context: str) -> None:
        pass  # briefing agent does not consume session glossary context

    # ------------------------------------------------------------------
    # BaseAgent contract
    # ------------------------------------------------------------------

    def subscribe(self, spec: UniversalJSONSpec) -> None:
        self._spec = spec

    def execute(self) -> BriefingOutput:
        """Generate targeting criteria for the subscribed campaign.

        GOLD path: analysis of all GOLD campaign records + this campaign's verified record.
        BRONZE path: zero-shot inference from live schema context.
        Returns BriefingOutput with structured_universe and structured_exclusions populated.
        Never raises to the orchestrator.
        """
        if self._spec is None:
            return self._minimal_error_output("subscribe() must be called before execute()")

        spec = self._spec
        try:
            if not self._non_interactive:
                ThoughtDisplay.brief_generating(spec.campaign_name, spec.campaign_tier)
            if spec.campaign_tier == "GOLD" and self._gold_index is not None:
                gold_record = self._gold_index.lookup(
                    spec.campaign_code, spec.campaign_sub_code,
                    medium=spec.medium, cadence=spec.cadence,
                )
                return self._execute_gold(spec, gold_record)
            return self._execute_bronze(spec)
        except Exception as exc:
            return self._minimal_error_output(str(exc)[:400])

    # ------------------------------------------------------------------
    # Interactive refinement — called by orchestrator when user requests changes
    # ------------------------------------------------------------------

    def refine_targeting(
        self,
        universe: str,
        exclusions: list[str],
        user_correction: str,
    ) -> BriefingOutput:
        """Update targeting criteria based on user's requested change.

        Takes the current confirmed universe and exclusion list plus the user's
        plain-language correction and returns a new BriefingOutput with updated
        structured_universe and structured_exclusions.
        Never raises.
        """
        spec = self._spec
        if spec is None:
            return self._minimal_error_output("No active spec — subscribe() must be called first")
        try:
            current_block = self._format_criteria_for_refinement(universe, exclusions)
            prompt = (
                f"Campaign: {spec.campaign_name}\n\n"
                "Current targeting criteria:\n"
                f"{current_block}\n\n"
                f"User's requested change: {user_correction}\n\n"
                "Apply the user's change and output the revised targeting criteria. "
                "Keep all other criteria unchanged unless the correction specifically affects them."
                + _TC_OUTPUT_RULES
            )
            raw = self._call_standard(_BRIEFING_SYSTEM, prompt)
            return self._assemble_output(spec, raw, confidence=0.85)
        except Exception as exc:
            return self._minimal_error_output(str(exc)[:400])

    def generate_segments(
        self,
        universe: str,
        exclusions: list[str],
        segmentation_basis: str,
    ) -> BriefingOutput:
        """Generate mutually exclusive segmentation criteria for the confirmed targeting universe.

        Takes the finalized universe and exclusions (for context) plus the user's
        segmentation basis and returns a BriefingOutput with structured_segments populated.
        Enforces: no new exclusions, strictly mutually exclusive, complete coverage.
        Never raises.
        """
        spec = self._spec
        if spec is None:
            return self._minimal_error_output("No active spec — subscribe() must be called first")
        try:
            all_campaigns_block = self._build_all_campaigns_context(
                focus_query=f"{spec.campaign_name} {segmentation_basis}"
            )
            criteria_block = self._format_criteria_for_refinement(universe, exclusions)
            prompt = (
                f"Campaign: {spec.campaign_name}\n\n"
                "Knowledge library of existing campaign segmentation patterns:\n"
                f"{all_campaigns_block}\n\n"
                "Confirmed targeting criteria for this campaign:\n"
                f"{criteria_block}\n\n"
                f"User's segmentation basis: {segmentation_basis}\n\n"
                "Generate the segmentation criteria. Every segment must subdivide the confirmed "
                "targeting universe only — do not add any new exclusions."
                + _SEG_OUTPUT_RULES
            )
            raw = self._call_standard(_BRIEFING_SYSTEM, prompt)
            return self._assemble_output(spec, raw, confidence=0.85, is_segmentation=True)
        except Exception as exc:
            return self._minimal_error_output(str(exc)[:400])

    # ------------------------------------------------------------------
    # GOLD path
    # ------------------------------------------------------------------

    def _execute_gold(self, spec: UniversalJSONSpec, gold_record: Optional[GoldCampaignRecord]) -> BriefingOutput:
        has_brief = bool(gold_record is not None and getattr(gold_record, "brief_text", ""))
        confidence = 0.90 if has_brief else 0.70

        # Build the all-campaigns analysis block (cached — same for every request)
        all_campaigns_block = self._build_all_campaigns_context(
            focus_query=f"{spec.campaign_name} {spec.target_population}"
        )

        # Build this campaign's specific context
        campaign_block = self._build_specific_campaign_block(spec, gold_record)

        request_prompt = self._build_targeting_request_prompt(spec, confidence)

        system_blocks = [
            {"type": "text", "text": _BRIEFING_SYSTEM, "cache_control": {"type": "ephemeral"}},
        ]

        user_content: list[dict] = [
            {
                "type": "text",
                "text": (
                    "FULL KNOWLEDGE LIBRARY — all campaigns for pattern analysis:\n\n"
                    + all_campaigns_block
                ),
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": campaign_block,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": request_prompt},
        ]

        raw = self._call_with_caching(system_blocks, user_content)
        return self._assemble_output(spec, raw, confidence)

    def _build_specific_campaign_block(
        self, spec: UniversalJSONSpec, gold_record: Optional[GoldCampaignRecord]
    ) -> str:
        parts = ["=== THIS CAMPAIGN (primary source of truth) ===\n\n"]
        parts.append(f"Campaign: {spec.campaign_name}\n")
        parts.append(f"Code: {spec.campaign_code} / {spec.campaign_sub_code}\n")
        parts.append(f"Medium: {spec.medium} | Cadence: {spec.cadence}\n")
        parts.append(f"Target Population (from spec): {spec.target_population}\n")

        if gold_record is not None:
            ts = getattr(gold_record, "targeting_summary", "")
            ss = getattr(gold_record, "segment_summary", "")
            bt = getattr(gold_record, "brief_text", "")
            pp = getattr(gold_record, "primary_products", "")
            cp = getattr(gold_record, "campaign_purpose", "")
            be = getattr(gold_record, "brief_extraction", None) or {}

            if ts:
                parts.append(f"\nVerified Targeting Summary:\n{ts}\n")
            if ss:
                parts.append(f"\nVerified Segment Summary:\n{ss}\n")
            if bt:
                parts.append(f"\nOriginal Data Brief:\n{bt[:6000]}\n")
            if pp:
                parts.append(f"\nPrimary Products: {pp}\n")
            if cp:
                parts.append(f"Campaign Purpose: {cp}\n")
            excl = be.get("exclusion_rules", [])
            if excl:
                parts.append("\nExtracted Exclusion Rules:\n")
                for e in excl:
                    parts.append(f"  - {e}\n")
            targeting_filters = be.get("targeting_filters", [])
            if targeting_filters:
                parts.append("\nExtracted Targeting Filters:\n")
                for f in targeting_filters:
                    parts.append(f"  - {f}\n")
        else:
            parts.append("\nNo verified record found — generating from spec context only.\n")

        if spec.filters:
            parts.append("\nSpec Filters:\n")
            for f in spec.filters:
                parts.append(f"  - {f}\n")
        if spec.exclusion_layers:
            parts.append("\nSpec Exclusion Layers:\n")
            for e in spec.exclusion_layers:
                parts.append(f"  - {e}\n")

        raw_prompt = (spec.brief_agent_inputs or {}).get("raw_prompt", "")
        if raw_prompt:
            parts.append(f"\nOriginal Request: {raw_prompt}\n")

        dep_history = (spec.brief_agent_inputs or {}).get("deployment_history", [])
        if dep_history:
            parts.append(f"\nHistorical Deployments ({len(dep_history)} records):\n")
            for dep in dep_history:
                parts.append(f"  - {dep}\n")

        related = (spec.brief_agent_inputs or {}).get("related_campaigns", [])
        if related:
            parts.append(
                f"\nAI-Identified Related Campaigns ({len(related)} found via semantic search):\n"
            )
            for r in related[:10]:
                name = r.get("campaign_name", r.get("camp_id", "Unknown"))
                acc = r.get("acc_summaries") or {}
                ts = (acc.get("targeting_summary") or "")[:300]
                medium = r.get("medium", "")
                cadence = r.get("cadence", "")
                channel = f" [{medium}/{cadence}]" if (medium or cadence) else ""
                if ts:
                    parts.append(f"  - {name}{channel}: {ts}\n")
                else:
                    parts.append(f"  - {name}{channel}\n")

        return "".join(parts)

    # ------------------------------------------------------------------
    # BRONZE path
    # ------------------------------------------------------------------

    def _execute_bronze(self, spec: UniversalJSONSpec) -> BriefingOutput:
        coverage = self._compute_schema_coverage(spec)
        confidence = 0.60 if coverage >= 0.80 else 0.40

        schema_block = (
            f"=== LIVE SCHEMA CONTEXT ===\n\n{self._runtime_schema[:4000]}"
            if self._runtime_schema
            else "=== LIVE SCHEMA CONTEXT ===\n\n(Schema context unavailable)"
        )

        prompt = "\n\n".join([
            schema_block,
            self._build_targeting_request_prompt(spec, confidence),
        ])
        raw = self._call_standard(_BRIEFING_SYSTEM, prompt)
        return self._assemble_output(spec, raw, confidence)

    def _compute_schema_coverage(self, spec: UniversalJSONSpec) -> float:
        if not self._runtime_schema:
            return 0.0
        _COL_BEFORE_OP = re.compile(
            r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|!=|<>|<=|>=|<|>|(?:NOT\s+)?IN\b|LIKE\b|IS\b)",
            re.IGNORECASE,
        )
        _FUNC_WRAPPER = re.compile(r"^(?:UPPER|LOWER|DATE|TRIM|CAST)\b", re.IGNORECASE)
        all_filters = list(spec.filters) + list(spec.exclusion_layers or [])
        filter_text = " ".join(all_filters)
        raw_tokens = {m.group(1) for m in _COL_BEFORE_OP.finditer(filter_text)}
        bare_cols = {tok.lower() for tok in raw_tokens if not _FUNC_WRAPPER.match(tok)}
        if not bare_cols:
            return 1.0
        schema_lower = self._runtime_schema.lower()
        found = sum(1 for col in bare_cols if col in schema_lower)
        return found / len(bare_cols)

    # ------------------------------------------------------------------
    # All-campaigns context builder (for knowledge layer analysis)
    # ------------------------------------------------------------------

    def _build_all_campaigns_context(self, focus_query: str = "") -> str:
        """Return a compact block containing ALL campaigns from the gold index.

        Every campaign's targeting summary, segment summary, key exclusion rules,
        and brief extraction strategy are included so the AI can analyze patterns
        across the full knowledge library before making recommendations.
        """
        if self._gold_index is None:
            if self._knowledge_ctx is not None:
                return self._knowledge_ctx.briefing_context
            return "(No campaign knowledge available)"

        records = list(self._gold_index._index.values())
        if not records:
            return "(Knowledge library is empty)"

        # Group all deployment records by campaign identity (camp_id + sub_camp_id).
        # Deployments of the same campaign at different channels or cadences are merged
        # into one unified profile so the AI sees complete per-campaign intelligence
        # rather than one arbitrarily-chosen deployment variant per campaign.
        campaign_groups: dict[str, list] = defaultdict(list)
        for rec in records:
            key = f"{rec.camp_id}::{rec.sub_camp_id}"
            campaign_groups[key].append(rec)

        # Order campaigns by semantic relevance to the focus query when possible.
        # This puts the most relevant campaigns first in the AI's context window.
        group_order: list[str] = list(campaign_groups.keys())
        if focus_query and self._knowledge_ctx is not None:
            try:
                ranked = self._knowledge_ctx.retrieve_campaigns(
                    focus_query, top_k=len(campaign_groups)
                )
                ranked_keys = [
                    f"{r.get('camp_id', '')}::{r.get('sub_camp_id', '')}"
                    for r in ranked
                ]
                seen_ranked: set[str] = set(ranked_keys)
                group_order = ranked_keys + [k for k in campaign_groups if k not in seen_ranked]
            except Exception:
                pass

        parts = [
            f"KNOWLEDGE LIBRARY — {len(campaign_groups)} campaigns "
            f"({len(records)} deployment records)\n"
            "Study ALL of these before generating targeting criteria.\n\n"
        ]

        for key in group_order:
            group_recs = campaign_groups.get(key)
            if not group_recs:
                continue
            primary = group_recs[0]
            parts.append(
                f"--- {primary.campaign_name} ({primary.camp_id} / {primary.sub_camp_id}) ---\n"
            )

            dep_variants = [
                f"{r.medium}/{r.cadence}" for r in group_recs if r.medium or r.cadence
            ]
            if dep_variants:
                parts.append("Deployments: " + " | ".join(dep_variants) + "\n")

            if primary.campaign_purpose:
                parts.append(f"Purpose: {primary.campaign_purpose}\n")

            # Merge targeting summaries: include each unique summary across all deployments
            seen_targeting: set[str] = set()
            for r in group_recs:
                ts = (r.targeting_summary or "").strip()
                if ts and ts not in seen_targeting:
                    seen_targeting.add(ts)
                    label = f"{r.medium}/{r.cadence}" if (r.medium or r.cadence) else "all"
                    parts.append(f"Targeting [{label}]: {ts[:600]}\n")

            best_ss = max((r.segment_summary or "" for r in group_recs), key=len)
            if best_ss:
                parts.append(f"Segmentation: {best_ss[:300]}\n")

            best_bt = max((r.brief_text or "" for r in group_recs), key=len)
            if best_bt:
                parts.append(f"Data Brief: {best_bt[:1500]}\n")

            # Merge exclusion rules across all deployments (deduplicate by content)
            all_excls: list[str] = []
            seen_excls: set[str] = set()
            for r in group_recs:
                be = r.brief_extraction or {}
                for e in be.get("exclusion_rules", []):
                    e_str = str(e)
                    if e_str not in seen_excls:
                        seen_excls.add(e_str)
                        all_excls.append(e_str)
            if all_excls:
                parts.append("Exclusions: " + " | ".join(all_excls[:8]) + "\n")

            # Merge targeting filters across all deployments (deduplicate by content)
            all_tf: list[str] = []
            seen_tf: set[str] = set()
            for r in group_recs:
                be = r.brief_extraction or {}
                for f in be.get("targeting_filters", []):
                    f_str = str(f)
                    if f_str not in seen_tf:
                        seen_tf.add(f_str)
                        all_tf.append(f_str)
            if all_tf:
                parts.append("Key Filters: " + " | ".join(all_tf[:6]) + "\n")

            parts.append("\n")

        return "".join(parts)

    # ------------------------------------------------------------------
    # Request prompt builder
    # ------------------------------------------------------------------

    def _build_targeting_request_prompt(self, spec: UniversalJSONSpec, confidence: float) -> str:
        return (
            f"Now generate targeting criteria for: {spec.campaign_name}\n"
            f"Tier: {spec.campaign_tier} | Medium: {spec.medium} | Cadence: {spec.cadence}\n\n"
            "Instructions:\n"
            "1. Study the full knowledge library above to understand what targeting and exclusion "
            "patterns are standard across all related campaigns.\n"
            "2. Read the verified data for this specific campaign (targeting summary, segment summary, "
            "data brief, extracted filters).\n"
            "3. Synthesize both to produce targeting criteria that reflect this campaign's specific "
            "audience while adhering to the patterns common across the knowledge library.\n"
            "4. The UNIVERSE must describe the starting population precisely — product line, customer "
            "type, geography, and primary eligibility condition.\n"
            "5. Each EXCLUSION must be a complete sentence starting with 'Exclude'. Always include "
            "DNC for the channel, control group exclusion, and GCH recency suppression.\n"
            + _TC_OUTPUT_RULES
        )

    # ------------------------------------------------------------------
    # Criteria formatting helper (for refinement prompts)
    # ------------------------------------------------------------------

    @staticmethod
    def _format_criteria_for_refinement(universe: str, exclusions: list[str]) -> str:
        lines = [f"{_TC_BEGIN}", f"UNIVERSE: {universe}"]
        for i, excl in enumerate(exclusions, start=1):
            lines.append(f"EXCLUSION {i}: {excl}")
        lines.append(_TC_END)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # API callers
    # ------------------------------------------------------------------

    def _call_with_caching(self, system_blocks: list[dict], user_content: list[dict]) -> str:
        payload: dict = {
            "model": self._model,
            "system": system_blocks,
            "messages": [{"role": "user", "content": user_content}],
            "max_tokens": 4096,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "anthropic-beta": "prompt-caching-2024-07-31",
        }
        resp = resilient_post(
            f"{_FUELIX_BASE}/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=180,
        )
        return self._extract_text(resp.json())

    def _call_standard(self, system: str, user_prompt: str) -> str:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 4096,
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        resp = resilient_post(
            f"{_FUELIX_BASE}/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=180,
        )
        return self._extract_text(resp.json())

    @staticmethod
    def _extract_text(resp_json: dict) -> str:
        choices = resp_json.get("choices") or []
        if choices:
            msg = choices[0].get("message") or {}
            content = msg.get("content") or ""
            if isinstance(content, list):
                return "".join(
                    block.get("text", "")
                    for block in content
                    if block.get("type") == "text"
                ).strip()
            return str(content).strip()
        content = resp_json.get("content") or []
        if isinstance(content, list):
            return "".join(
                block.get("text", "")
                for block in content
                if block.get("type") == "text"
            ).strip()
        return ""

    # ------------------------------------------------------------------
    # Output assembly and parsing
    # ------------------------------------------------------------------

    def _assemble_output(
        self,
        spec: UniversalJSONSpec,
        raw: str,
        confidence: float,
        is_segmentation: bool = False,
    ) -> BriefingOutput:
        knowledge_sources, clean = self._extract_knowledge_block(raw)

        if is_segmentation:
            segments = self._parse_segments(clean)
            return BriefingOutput(
                campaign_name=spec.campaign_name,
                tier=spec.campaign_tier,
                brief_markdown=clean,
                executive_summary="",
                targeting_logic_summary="",
                strategic_recommendations=[],
                data_sources_cited=self._build_data_sources(spec),
                confidence_score=confidence,
                generated_at=datetime.now(tz=timezone.utc).isoformat(),
                knowledge_sources_used=knowledge_sources or None,
                structured_segments=segments,
            )

        universe, exclusions = self._parse_targeting_criteria(clean)

        # brief_markdown holds the clean raw text for audit/display
        return BriefingOutput(
            campaign_name=spec.campaign_name,
            tier=spec.campaign_tier,
            brief_markdown=clean,
            executive_summary="",
            targeting_logic_summary=f"Universe: {universe}" if universe else "",
            strategic_recommendations=[],
            data_sources_cited=self._build_data_sources(spec),
            confidence_score=confidence,
            generated_at=datetime.now(tz=timezone.utc).isoformat(),
            knowledge_sources_used=knowledge_sources or None,
            structured_universe=universe,
            structured_exclusions=exclusions if exclusions else None,
        )

    @staticmethod
    def _extract_knowledge_block(raw: str) -> tuple[list[str], str]:
        m = _KNOWLEDGE_BLOCK_RE.search(raw)
        if not m:
            return [], raw
        sources = [
            line.lstrip("- ").strip()
            for line in m.group(1).splitlines()
            if line.strip()
        ]
        clean = raw[:m.start()] + raw[m.end():]
        return sources, clean.lstrip("\n")

    @staticmethod
    def _parse_targeting_criteria(text: str) -> tuple[str, list[str]]:
        """Extract universe sentence and ordered exclusion list from the structured block."""
        m = _TC_BLOCK_RE.search(text)
        if not m:
            return "", []
        block = m.group(1)
        universe = ""
        exclusions: list[str] = []
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("UNIVERSE:"):
                universe = line[len("UNIVERSE:"):].strip()
            elif re.match(r"EXCLUSION\s+\d+:", line, re.IGNORECASE):
                val = re.sub(r"^EXCLUSION\s+\d+:\s*", "", line, flags=re.IGNORECASE).strip()
                if val:
                    exclusions.append(val)
        return universe, exclusions

    @staticmethod
    def _parse_segments(text: str) -> list[SegmentCriterion]:
        """Extract named mutually exclusive segments from the segmentation block."""
        m = _SEG_BLOCK_RE.search(text)
        if not m:
            return []
        block = m.group(1)
        segments: list[SegmentCriterion] = []
        for line in block.splitlines():
            line = line.strip()
            seg_m = re.match(r"SEGMENT\s+\d+\s*-\s*(.+?):\s*(.+)", line, re.IGNORECASE)
            if seg_m:
                name = seg_m.group(1).strip()
                description = seg_m.group(2).strip()
                if name and description:
                    segments.append(SegmentCriterion(name=name, description=description))
        return segments

    @staticmethod
    def _build_data_sources(spec: UniversalJSONSpec) -> list[str]:
        sources = []
        if spec.campaign_tier == "GOLD":
            sources.append("GOLD tier verified campaign records (full knowledge library)")
        return sources

    def _minimal_error_output(self, reason: str) -> BriefingOutput:
        return BriefingOutput(
            campaign_name=self._spec.campaign_name if self._spec else "Unknown",
            tier=self._spec.campaign_tier if self._spec else "BRONZE",
            brief_markdown="",
            executive_summary="",
            targeting_logic_summary="",
            strategic_recommendations=[],
            data_sources_cited=[],
            confidence_score=0.0,
            generated_at=datetime.now(tz=timezone.utc).isoformat(),
            error_reason=reason,
        )
