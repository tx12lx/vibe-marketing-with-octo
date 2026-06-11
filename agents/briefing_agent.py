"""
Vibe OCTO Briefing — BriefingAgent Worker
Worker ID: briefing_v1

Generates structured campaign intelligence briefs via the Fuel iX API.

GOLD path: few-shot inference anchored to a verified GoldCampaignRecord.
  System prompt and historical campaign context are frozen via
  anthropic-beta prompt-caching headers.
  Confidence: 0.90 (brief_text present) or 0.70 (brief_text absent).

BRONZE path: zero-shot inference grounded in the live INFORMATION_SCHEMA
  snapshot injected by the orchestrator.
  Confidence: 0.60 (>= 80% filter coverage in schema) or 0.40 (lower coverage).

Output: structured BriefingOutput with a mandatory 6-section Markdown brief.
Strictly banned from output: SQL syntax, database view labels, column names,
schema identifiers, or any technical execution detail.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import requests
from dotenv import load_dotenv
from pydantic import BaseModel
from core.resilience import resilient_post

if TYPE_CHECKING:
    from core.knowledge_context import KnowledgeContext

_AGENTS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _AGENTS_DIR.parent
_BRIEFING_DIR = _ROOT_DIR / "Vibe OCTO Briefing"  # original subdirectory for .env loading

for _p in [str(_ROOT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

load_dotenv(_BRIEFING_DIR / ".env")

from pydantic_schemas import BriefingOutput, UniversalJSONSpec  # noqa: E402
from core.base_agent import BaseAgent  # noqa: E402
from knowledge_base.tier_index import GoldTierIndex  # noqa: E402
from core.thought_display import ThoughtDisplay  # noqa: E402

_FUELIX_BASE = "https://api.fuelix.ai"

# ---------------------------------------------------------------------------
# System prompt — identity anchor is immutable
# ---------------------------------------------------------------------------

_BRIEFING_SYSTEM = (
    "You are Vibe OCTO Briefing, a senior telecom marketing strategist "
    "for TELUS and Koodo. You generate structured campaign intelligence "
    "briefs combining data brief content, targeting logic, and predictive "
    "strategic recommendations. Output must be valid Markdown matching the "
    "prescribed section structure exactly. Strategic recommendations must be "
    "grounded in Canadian telecom industry dynamics: rate plan migration "
    "trends, ARPU optimization, device lifecycle economics, and omni-channel "
    "contact sequencing. Never reference SQL, BigQuery, or technical "
    "execution details in the brief output."
)

# ---------------------------------------------------------------------------
# Output section template — campaign name, tier, timestamp, confidence,
# and medium are interpolated at request time; everything else is structural.
# ---------------------------------------------------------------------------

_SECTION_TEMPLATE = """\
# Campaign Brief: {campaign_name}
**Tier**: {tier} | **Generated**: {timestamp} | **Confidence**: {confidence}

## Campaign Overview
[2-3 sentence executive summary grounded in the campaign's actual targeting \
logic and business objective — identify the customer lifecycle state, operational \
mechanic (cross-sell / retention / upgrade / win-back), and revenue impact]

## Data Brief Reference
[Summary of the source documentation content, or "No data brief available" if none was provided]

## Targeting Logic
[Structured description of audience inclusion and exclusion criteria entirely in \
business language — customer tenure, product eligibility state, geographic scope, \
behavioral indicators, contact channel eligibility; no technical identifiers]

## Audience Segmentation
[Breakdown of key audience sub-segments: provincial or regional distribution, \
product eligibility tiers, customer lifecycle cohorts, behavioral cohorts; \
include relative sizing commentary where the context supports it]

## Strategic Recommendations
1. [Rate plan migration or ARPU growth recommendation grounded in the campaign's \
target lifecycle state and product eligibility profile]
2. [Device lifecycle, handset tenure, or upgrade pathway recommendation]
3. [Cross-sell or bundle attachment recommendation aligned to the campaign's \
primary product vertical]
4. [Omni-channel contact sequencing or channel prioritization recommendation \
matched to the campaign medium and customer reachability profile]
5. [Competitive positioning, retention defence, or segment-specific recommendation \
— omit this line entirely if not strongly indicated by the brief context]

## Execution Checklist
- [ ] GCH recency suppression applied (confirm lookback window)
- [ ] DNC channel flags verified for {medium}
- [ ] Control group flag excluded (final audience filter step)
- [ ] Quebec province codes cover both historical billing codes
- [ ] Product eligibility pair logic confirmed (ownership exclusion paired with \
eligibility inclusion)\
"""

_OUTPUT_RULES = (
    "\n\nSTRICT OUTPUT RULES:\n"
    "- Output only the Markdown brief — no preamble, no explanation, no text outside the structure\n"
    "- Never reference SQL syntax, database queries, table names, column names, view names, "
    "schema labels, BigQuery, or any technical execution detail anywhere in the brief text\n"
    "- Do not hardcode or assume a fixed product line; derive the line of business, customer "
    "segment, and strategic vertical organically from the campaign metadata provided\n"
    "- Evaluate the campaign's target customer base, operational mechanics, and strategic "
    "vertical from the filters, exclusions, campaign purpose, and brief content — then tailor "
    "the narrative to that specific context\n"
    "- All recommendations must address Canadian telecom industry dynamics specific to the "
    "campaign's derived vertical: wireless mobility, fixed internet, TV, bundled FFH, or "
    "business mobility as indicated by the campaign context\n"
    "- Strategic Recommendations section: include items 1-4 always; include item 5 only if "
    "strongly warranted by the campaign context; otherwise omit it\n"
)

# Regex to extract column-like tokens preceding SQL comparison operators.
# Used for BRONZE schema coverage scoring — never applied to brief output.
_COL_BEFORE_OP = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?:=|!=|<>|<=|>=|<|>|(?:NOT\s+)?IN\b|LIKE\b|IS\b)",
    re.IGNORECASE,
)

# Strips SQL function wrappers (e.g. UPPER, DATE) that appear before the real column name.
_FUNC_WRAPPER = re.compile(r"^(?:UPPER|LOWER|DATE|TRIM|CAST)\b", re.IGNORECASE)


class BriefingAgent(BaseAgent):
    """Pillar 4 Worker B — structured campaign brief generation.

    Implements the BaseAgent contract:
      subscribe(spec)  -> stores the UniversalJSONSpec
      execute()        -> generates and returns BriefingOutput

    Never raises to the orchestrator; wraps all failures in a minimal
    BriefingOutput with empty brief_markdown and confidence_score=0.0.
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

    # ------------------------------------------------------------------
    # Injection points called by the orchestrator
    # ------------------------------------------------------------------

    def set_knowledge_context(self, ctx: "KnowledgeContext") -> None:
        """Bind the centralised KnowledgeContext built at startup."""
        self._knowledge_ctx = ctx

    def set_gold_index(self, gold_index: GoldTierIndex) -> None:
        """Bind the in-memory GoldTierIndex for GOLD path record lookup."""
        self._gold_index = gold_index

    def set_runtime_schema(self, schema_str: str) -> None:
        """Receive the live INFORMATION_SCHEMA snapshot (Pillar 2) for BRONZE path."""
        self._runtime_schema = schema_str

    def set_session_context(self, context: str) -> None:
        """No-op: briefing agent does not consume session glossary context."""

    # ------------------------------------------------------------------
    # BaseAgent contract
    # ------------------------------------------------------------------

    def subscribe(self, spec: UniversalJSONSpec) -> None:
        """Store the UniversalJSONSpec for this execution cycle."""
        self._spec = spec

    def execute(self) -> BriefingOutput:
        """Generate the campaign brief.

        GOLD path: few-shot inference from GoldCampaignRecord with prompt caching.
        BRONZE path: zero-shot inference from live schema context.
        Returns BriefingOutput; never raises.
        """
        if self._spec is None:
            return self._minimal_error_output("subscribe() must be called before execute()")

        spec = self._spec
        try:
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
    # GOLD path — few-shot with prompt-cached historical context
    # ------------------------------------------------------------------

    def _execute_gold(self, spec: UniversalJSONSpec, gold_record) -> BriefingOutput:
        has_brief = bool(
            gold_record is not None and getattr(gold_record, "brief_text", "")
        )
        confidence = 0.90 if has_brief else 0.70

        gold_context = self._build_gold_context_block(spec, gold_record)
        request_prompt = self._build_request_prompt(spec, confidence)

        # GOLD API call: structured content blocks with cache_control on the
        # stable historical context so Fuel iX / Claude can freeze it in cache.
        system_blocks = [
            {
                "type": "text",
                "text": _BRIEFING_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        # When KnowledgeContext is available, prepend all GOLD campaign briefs as
        # context so the briefing agent can draw on similar campaigns as templates.
        if self._knowledge_ctx is not None:
            kb_block = {
                "type": "text",
                "text": (
                    "VIBE OCTO BRIEF TEMPLATES (All GOLD campaigns for reference)\n\n"
                    + self._knowledge_ctx.briefing_context
                ),
                "cache_control": {"type": "ephemeral"},
            }
            campaign_block = {
                "type": "text",
                "text": gold_context,
                "cache_control": {"type": "ephemeral"},
            }
            user_content = [
                kb_block,
                campaign_block,
                {"type": "text", "text": request_prompt},
            ]
        else:
            user_content = [
                {
                    "type": "text",
                    "text": gold_context,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": request_prompt,
                },
            ]

        brief_markdown = self._call_with_caching(system_blocks, user_content)
        return self._assemble_output(spec, brief_markdown, confidence)

    def _build_gold_context_block(self, spec: UniversalJSONSpec, gold_record) -> str:
        parts = ["=== VERIFIED CAMPAIGN INTELLIGENCE (GOLD TIER) ===\n\n"]

        if gold_record is not None:
            parts.append(f"Campaign: {gold_record.campaign_name}\n")
            ts = getattr(gold_record, "targeting_summary", "")
            ss = getattr(gold_record, "segment_summary", "")
            bt = getattr(gold_record, "brief_text", "")
            pp = getattr(gold_record, "primary_products", "")
            cp = getattr(gold_record, "campaign_purpose", "")
            med = getattr(gold_record, "medium", spec.medium)
            cad = getattr(gold_record, "cadence", spec.cadence)

            if ts:
                parts.append(f"\nTargeting Summary (verified):\n{ts}\n")
            if ss:
                parts.append(f"\nAudience Segment Summary (verified):\n{ss}\n")
            if bt:
                parts.append(f"\nData Brief Content:\n{bt[:6000]}\n")
            parts.append(
                f"\nPrimary Products: {pp or 'Not specified'}\n"
                f"Campaign Purpose: {cp or 'Not specified'}\n"
                f"Medium: {med} | Cadence: {cad}\n"
            )
        else:
            parts.append(
                "No verified record found for this campaign key. "
                "Generating from spec context only.\n"
            )

        parts.append(
            "\nUse the verified intelligence above as the primary source of truth "
            "for the campaign's targeting logic and audience characteristics. "
            "Where the data brief provides richer context, let it inform the "
            "strategic recommendations section.\n"
        )
        return "".join(parts)

    # ------------------------------------------------------------------
    # BRONZE path — zero-shot with live schema context
    # ------------------------------------------------------------------

    def _execute_bronze(self, spec: UniversalJSONSpec) -> BriefingOutput:
        coverage = self._compute_schema_coverage(spec)
        confidence = 0.60 if coverage >= 0.80 else 0.40

        schema_block = (
            f"=== LIVE DATA ENVIRONMENT SCHEMA CONTEXT ===\n\n{self._runtime_schema[:4000]}"
            if self._runtime_schema
            else "=== LIVE DATA ENVIRONMENT SCHEMA CONTEXT ===\n\n(Schema context unavailable)"
        )

        combined_prompt = "\n\n".join([
            schema_block,
            self._build_request_prompt(spec, confidence),
        ])

        brief_markdown = self._call_standard(_BRIEFING_SYSTEM, combined_prompt)
        return self._assemble_output(spec, brief_markdown, confidence)

    def _compute_schema_coverage(self, spec: UniversalJSONSpec) -> float:
        """Fraction of filter-referenced column tokens found in the runtime schema."""
        if not self._runtime_schema:
            return 0.0

        all_filters = list(spec.filters) + list(spec.exclusion_layers or [])
        filter_text = " ".join(all_filters)

        # Extract candidate column names — strip SQL function wrappers
        raw_tokens = {m.group(1) for m in _COL_BEFORE_OP.finditer(filter_text)}
        bare_cols = {
            tok.lower()
            for tok in raw_tokens
            if not _FUNC_WRAPPER.match(tok)
        }

        if not bare_cols:
            return 1.0

        schema_lower = self._runtime_schema.lower()
        found = sum(1 for col in bare_cols if col in schema_lower)
        return found / len(bare_cols)

    # ------------------------------------------------------------------
    # Shared request prompt builder
    # ------------------------------------------------------------------

    def _build_request_prompt(self, spec: UniversalJSONSpec, confidence: float) -> str:
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        filters_text = "\n".join(f"  - {f}" for f in spec.filters)
        exclusions_text = (
            "\n".join(f"  - {e}" for e in spec.exclusion_layers)
            if spec.exclusion_layers
            else "  None specified"
        )

        raw_context = ""
        if spec.brief_agent_inputs:
            raw_prompt = spec.brief_agent_inputs.get("raw_prompt", "")
            if raw_prompt:
                raw_context = f"\nOriginal Request Context:\n{raw_prompt}\n"

        structure = _SECTION_TEMPLATE.format(
            campaign_name=spec.campaign_name,
            tier=spec.campaign_tier,
            timestamp=timestamp,
            confidence=f"{confidence:.2f}",
            medium=spec.medium,
        )

        return (
            f"Campaign: {spec.campaign_name}\n"
            f"Code: {spec.campaign_code} / {spec.campaign_sub_code}\n"
            f"Tier: {spec.campaign_tier}\n"
            f"Medium: {spec.medium} | Cadence: {spec.cadence}\n"
            f"Target Population: {spec.target_population}\n"
            f"\nAudience Filters:\n{filters_text}\n"
            f"\nExclusion Layers:\n{exclusions_text}\n"
            f"{raw_context}"
            f"\n{structure}"
            f"{_OUTPUT_RULES}"
        )

    # ------------------------------------------------------------------
    # API callers
    # ------------------------------------------------------------------

    def _call_with_caching(
        self,
        system_blocks: list[dict],
        user_content: list[dict],
    ) -> str:
        """GOLD path: Anthropic Messages API format with prompt-caching header.

        Posts to /v1/chat/completions with structured content arrays.
        The anthropic-beta header signals Fuel iX to forward caching semantics
        to the upstream Claude API, freezing the historical context block.
        """
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
        """BRONZE path: standard OpenAI-compatible chat completions call."""
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
        """Extract the text payload from either OpenAI or Anthropic response shapes."""
        # OpenAI chat completions format
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

        # Anthropic Messages API format fallback
        content = resp_json.get("content") or []
        if isinstance(content, list):
            return "".join(
                block.get("text", "")
                for block in content
                if block.get("type") == "text"
            ).strip()

        return ""

    # ------------------------------------------------------------------
    # Output assembly and section parsing
    # ------------------------------------------------------------------

    def _assemble_output(
        self, spec: UniversalJSONSpec, brief_markdown: str, confidence: float
    ) -> BriefingOutput:
        return BriefingOutput(
            campaign_name=spec.campaign_name,
            tier=spec.campaign_tier,
            brief_markdown=brief_markdown,
            executive_summary=self._extract_section(brief_markdown, "Campaign Overview"),
            targeting_logic_summary=self._extract_section(brief_markdown, "Targeting Logic"),
            strategic_recommendations=self._extract_recommendations(brief_markdown),
            data_sources_cited=self._extract_data_sources(brief_markdown, spec),
            confidence_score=confidence,
            generated_at=datetime.now(tz=timezone.utc).isoformat(),
        )

    @staticmethod
    def _extract_section(markdown: str, section_name: str) -> str:
        pattern = re.compile(
            r"##\s+" + re.escape(section_name) + r"\s*\n(.*?)(?=\n##\s|\Z)",
            re.DOTALL | re.IGNORECASE,
        )
        m = pattern.search(markdown)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _extract_recommendations(markdown: str) -> list[str]:
        section = BriefingAgent._extract_section(markdown, "Strategic Recommendations")
        recs: list[str] = []
        for line in section.splitlines():
            stripped = line.strip()
            if re.match(r"^\d+\.\s+", stripped):
                recs.append(re.sub(r"^\d+\.\s+", "", stripped))
        return recs

    @staticmethod
    def _extract_data_sources(markdown: str, spec: UniversalJSONSpec) -> list[str]:
        sources: list[str] = []
        if spec.campaign_tier == "GOLD":
            sources.append("GOLD tier verified campaign record")
        ref_text = BriefingAgent._extract_section(markdown, "Data Brief Reference")
        if ref_text and "no data brief available" not in ref_text.lower():
            sources.append("campaign data brief document")
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
        )
