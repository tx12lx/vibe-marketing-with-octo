"""
Vibe OCTO Nexus — Ingestion & Taxonomy Agent

Step 1: On startup, pull 5 campaign briefs from BigQuery (or local glossary
        as fallback), send them to Claude via Fuel iX, and build a strategic
        taxonomy matrix. The matrix is pinned as an ephemeral cached block at
        the top of every subsequent prompt, targeting a ~90% token discount on
        repeated queries.

Step 2: Path 1 — Given a loaded brief dict or multi-deployment matrix from
        BQ, resolve to a validated AudienceSizingRequest for Quant. When
        multiple deployment records are returned, runs deployment variance
        analysis and strategy synthesis before compiling Quant instructions.

        Path 2 — Parse a natural language phrase, map it against the taxonomy,
        append strategic feedback, and emit a validated AudienceSizingRequest.

Step 4: Error loop — If Quant returns a NexusErrorPayload, attempt exactly ONE
        automated correction using the cached taxonomy. On second failure, print
        the terminal error and return None.
"""
from __future__ import annotations

import json
import os
import re
import sys
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

import requests
from dotenv import load_dotenv
from pydantic import ValidationError
from core.ai_client import ask_ai

_AGENTS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _AGENTS_DIR.parent
# Original subdirectory -- referenced by the (now-unreachable, briefing-only)
# taxonomy-brief helpers below. Kept only so those methods don't hit a
# NameError if something still calls them; slated for removal alongside the
# rest of the briefing machinery.
_NEXUS_DIR = _ROOT_DIR / "Vibe OCTO Nexus"

for _p in [str(_ROOT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pydantic_schemas import (
    AdHocSizingRequest,
    AudienceSizingRequest,
    IntentClassification,
    NexusErrorPayload,
    QuantAuditLog,
    UniversalJSONSpec,
)
from core.base_agent import BaseAgent  # noqa: E402
from core.thought_display import ThoughtDisplay  # noqa: E402

if TYPE_CHECKING:
    from quant_agent import QuantAgent

_MAX_TOKENS_BUILD = 4096
_MAX_TOKENS_QUERY = 8192

_NEXUS_SYSTEM = (
    "You are Vibe OCTO Nexus, a senior management consulting AI embedded in a "
    "Canadian telecom marketing team (TELUS / Koodo). "
    "You classify what a user is asking for, extract structured targeting parameters "
    "from natural language, and emit validated JSON payloads for downstream audience "
    "sizing. "
    "Return ONLY valid JSON — no explanation, no markdown, no trailing text.\n\n"
    "All table names, column meanings, and business rules are provided separately, from "
    "the knowledge layer -- not hardcoded here. Treat anything marked '(confirmed)' as "
    "ground truth. Treat anything marked '(unconfirmed guess)' as tentative -- you may "
    "still use it, but say so plainly if your classification depends on an unconfirmed "
    "guess, so a human can verify it."
)

_TAXONOMY_BQ_QUERY = """
SELECT DISTINCT
    campaign     AS campaign_name,
    camp_id      AS campaign_code,
    sub_camp_id  AS campaign_sub_code,
    cadence,
    medium,
    campaign_purpose,
    primary_products
FROM `{table}`
WHERE current_ind = 1
  AND closed_ind = 0
  AND UPPER(target_base) <> 'EPP'
  AND databrief_link IS NOT NULL
  AND databrief_link != ''
ORDER BY list_pull_date DESC
LIMIT 5
"""

_TAXONOMY_BUILD_PROMPT = """You are analyzing {n} historical campaign data briefs from a Canadian telecom (TELUS/Koodo).

Campaign Briefs:
{briefs_json}

Build a strategic taxonomy matrix that captures the patterns, classifications, and business
purposes embedded in these campaigns. This matrix will serve as the authoritative baseline
for all future campaign evaluations in this session.

Return exactly this JSON structure (no other text):
{{
  "taxonomy_version": "{date}",
  "campaign_classifications": {{
    "<campaign_code>": {{
      "full_name": "<descriptive human-readable name>",
      "purpose": "<business purpose: Cross-Sell | Winback | Acquisition | Retention | Upsell>",
      "typical_cadence": "<e.g. monthly | weekly | bi-weekly>",
      "typical_medium": "<e.g. EM | OB | SMS>",
      "typical_products": ["<product>"],
      "strategic_note": "<one-sentence insight about this campaign type>"
    }}
  }},
  "historical_strategies": [
    {{
      "campaign_code": "<code>",
      "cadence": "<frequency>",
      "medium": "<channel>",
      "purpose": "<purpose>",
      "optimization_notes": "<strategic insight: when this combination works and why>"
    }}
  ],
  "known_targeting_patterns": [
    "<pattern — e.g. 'TELUS Postpaid primary active subs: lob_desc = Telus Postpaid AND primary_sub = 1 AND sub_status = A'>"
  ],
  "medium_effectiveness_notes": {{
    "<medium_code>": "<insight about when/why this channel is deployed>"
  }},
  "standard_exclusions": [
    "<e.g. DNC / opt-out scrub>",
    "<e.g. Recent campaign contact within N days>",
    "<e.g. High-churn decile suppression>"
  ]
}}"""

_SIZE_CAMPAIGN_PROMPT = """Using the taxonomy matrix above as your authoritative reference:

A campaign brief has been selected with these parameters:
  Campaign Name : {campaign_name}
  Campaign Code : {campaign_code}
  Sub-Code      : {campaign_sub_code}
  Cadence       : {cadence}
  Medium        : {medium}
  Purpose       : {campaign_purpose}
  Products      : {primary_products}

Based on the campaign's classification in the taxonomy, its known targeting patterns,
and standard exclusion layers for this campaign type, build a complete AudienceSizingRequest.

Return exactly this JSON (no markdown):
{{
  "campaign_name": "{campaign_name}",
  "campaign_code": "{campaign_code}",
  "campaign_sub_code": "{campaign_sub_code}",
  "cadence": "{cadence}",
  "medium": "{medium}",
  "target_population": "<plain-English description of who qualifies — be specific>",
  "filters": ["<BQ-interpretable filter criterion 1>", "<filter criterion 2>"],
  "exclusion_layers": ["<standard exclusion 1>", "<exclusion 2>"],
  "optimization_context": "<one sentence: strategic context for the Quant audit>",
  "bq_project": "bi-srv-hsmdet-pr-7b9def",
  "bq_dataset": "campaign_data"
}}"""

_NL_PARSE_PROMPT = """A consultant has submitted an ad-hoc audience sizing request:
  "{query}"

COGNITIVE FORK — AD-HOC MODE:
This is NOT a pre-loaded campaign. Do NOT assign a historical campaign code from the taxonomy.
Do NOT copy filters, exclusion layers, or column references from known_targeting_patterns or
standard_exclusions in the taxonomy matrix.

Use the taxonomy ONLY as a field-naming reference — to translate the consultant's stated
criteria into the correct BigQuery column names visible in known_targeting_patterns.

Instructions:
1. Extract filters DIRECTLY and EXCLUSIVELY from the criteria the consultant explicitly stated.
   Translate their terms into BQ-interpretable filter strings using the schema column names
   visible in the taxonomy's known_targeting_patterns as a naming guide only.
   Include NO filters that are not derivable word-for-word from the consultant's input.
2. If cadence or medium are not stated, use empty strings — do not infer from taxonomy.
3. Leave exclusion_layers as an empty list — do not inherit standard exclusions from the taxonomy.
4. Write optimization_context as exactly 3 concise sentences focused solely on cadence risk,
   channel suitability, or send-timing safety for this specific input.
   Do not reference historical campaigns, AAL data, or taxonomy benchmark patterns.

Return exactly this JSON (no markdown):
{{
  "campaign_name": "<short descriptive label for this ad-hoc audience segment>",
  "campaign_code": "ADHOC",
  "campaign_sub_code": "ADHOC-001",
  "cadence": "<cadence if explicitly stated by consultant, else 'ad-hoc'>",
  "medium": "<medium if explicitly stated by consultant, else 'unspecified'>",
  "target_population": "<precise plain-English restatement of who qualifies>",
  "filters": ["<filter derived strictly from the consultant's stated criteria>"],
  "exclusion_layers": [],
  "optimization_context": "<3 sentences on cadence or channel safety — no historical references>",
  "bq_project": "bi-srv-hsmdet-pr-7b9def",
  "bq_dataset": "campaign_data"
}}"""

_RETRY_PROMPT = """Using the taxonomy matrix above as your authoritative reference:

A downstream sizing audit failed. Correct the request using your taxonomy knowledge.

  Error Type   : {error_type}
  Error Summary: {error_summary}
  Retry Hint   : {retry_hint}

Failed Request:
{original_request_json}

Produce a corrected AudienceSizingRequest JSON. Required fields:
  campaign_name, campaign_code, campaign_sub_code, cadence, medium,
  target_population (str), filters (list of strings — at least one entry),
  bq_project, bq_dataset.
Optional: exclusion_layers (list of strings), optimization_context (str).

If no valid correction can be determined, return exactly: {{"correctable": false}}

Return the corrected JSON only — no markdown, no explanation."""

_INTENT_CLASSIFY_V2_PROMPT = """\
A consultant submitted the following request to a Canadian telecom marketing AI:
  "{query}"

Knowledge context loaded:
  Known glossary terms: {glossary_summary}
  Known campaign codes (fallback): {campaign_codes}

Campaigns retrieved from the knowledge layer most relevant to this request:
{retrieved_campaigns}

Classify this request into exactly one intent type:

  "sizing_request"     -- User wants an audience count or headcount.
                         May reference a named campaign or a generic audience.

  "brief_generation"   -- User wants to create or generate a new campaign brief.

  "brief_qa"           -- User wants to review, question, or validate an
                         existing campaign brief.

  "campaign_execution" -- User wants a full campaign run: audience sizing,
                         campaign brief, and audit together. Requests that use
                         action verbs (run, execute, size, pull the playbook)
                         targeting a named campaign fall here.

  "general_question"   -- User has a question about campaigns, data, or
                         strategy that does not require SQL execution or
                         brief generation.

Campaign identification rules:
  - Read the retrieved campaign records above carefully.
  - If any retrieved campaign matches what the user is asking about — whether
    by product name, campaign type, marketing objective, or customer action
    described in the strategy summary — set campaign_identified to true and
    return that campaign's camp_id as campaign_code.
  - Do not require an exact code match. Use the strategy and description text
    to reason about whether the user's words refer to a campaign in the records.
  - If no retrieved campaign matches the user's request, set campaign_identified
    to false and campaign_code to null.

Priority routing rules:
  1. Identified campaign + execution verb (run, execute, size, pull) -> "campaign_execution".
  2. Identified campaign + count/size question -> "sizing_request".
  3. Count or how-many question without identified campaign -> "sizing_request".
  4. Question about what campaigns exist or how something works -> "general_question".

Return exactly this JSON (no markdown, no explanation):
{{
  "intent_type": "<one of the 5 types above>",
  "confidence": <0.0 to 1.0>,
  "campaign_identified": <true or false>,
  "campaign_code": "<camp_id from retrieved records if identified, else null>",
  "knowledge_sources_consulted": ["glossary", "campaign_index"],
  "business_rules_applied": [],
  "reasoning": "<one sentence explaining the classification>"
}}"""


_DEPLOYMENT_ANALYSIS_PROMPT = """You are analyzing {n} deployment record(s) retrieved from the master Data Brief registry
(`bq_plan_camp_deploy_mdc`) for the AAL Monthly Email portfolio initiative. Each row is the
direct functional business requirement for a distinct list pull execution of this campaign.
Study ALL fields — including the databrief_link column, which points to the authoritative
source brief document for that run — to build global strategic context: macro campaign
architecture, channel assignments, lifecycle cadence, and suppression configurations.

Deployment Records (sorted most-recent first):
{deployments_json}

=== STEP 1 — GLOBAL STRATEGIC CONTEXT ===
Analyze all returned fields to understand the full campaign architecture: which LOB(s) are
in scope, what lifecycle stage governs eligibility, which channel(s) are active, what
propensity model drives selection, how the GCH recency suppression rules are configured,
and any behavioral exclusion windows. The databrief_link column is the pointer to the
complete parameter set for each run — treat it as the authoritative reference.

=== STEP 2 — TARGETING CRITERIA ISOLATION (THE SIEVE) ===
Before building any sizing instructions, classify every brief field as Targeting Criteria
or Segmentation. Only Targeting Criteria flow into the compiled_instructions output.

TARGETING CRITERIA — extract these exclusively into compiled_instructions:
  Inclusions : province scope (UPPER(province) IN / NOT IN), NBA propensity decile tiers
               (seg_nm IN ('reco_N', ...)), LOB scope (lob_desc values), product ownership /
               eligibility pairs (X_ind = 0 AND X_elig = 1), lifecycle windows.
  Exclusions : GCH recency suppression (30-day lookback via AAL/AALBAU matrix by default),
               behavioral line age limits (init_activation_date windows).
  Channel    : em_dnc = 0 for email channel governance.

SEGMENTATION CRITERIA — discard entirely, do NOT translate into filter criteria:
  These describe post-sizing list operations and have zero mathematical impact on audience
  volume. Recognised patterns — treat any of these as inert:
    Language split ratios (e.g. '60% EN / 40% FR') or EN/FR sub-segment headcounts,
    A/B or multivariate test splits, creative version matrices (Version A / B / C),
    copy version specifications or sub-allocation percentages,
    control group split percentages (handled exclusively by control_group_flg = 'N' in CTE 7).

=== STEP 3 — DISCREPANCY RESOLUTION ===
If you detect conflicting targeting rules across the active deployment records (e.g. different
province scopes, different propensity tier ranges, different lookback windows), synthesize a
single unified 'Final Recommended Targeting Criteria' set. The most recent record (first in
the list) takes precedence on any ambiguous parameter. Note the discrepancy and the adopted
resolution in the optimization_context field.

Tasks:
1. SCAN for divergence across deployments — examine propensity tier access, province scope,
   lifecycle windows, and eligibility criteria for signals of targeting evolution specific to
   the email channel. Reference databrief_link values as the source documentation for each
   run's complete parameter set.
2. SYNTHESIZE a strategic summary focused exclusively on the monthly email deployment:
   identify the target audience (LOB, lifecycle stage, propensity tier or behavioral trigger),
   explain what the email send is designed to achieve, and name any concrete parameter shifts
   (e.g. "widened NBA decile access from reco_1-5 to reco_1-6", "narrowed to BC/AB only").
   If only one deployment is present, describe its email send logic and audience intent.
3. SELECT the target deployment: the most recent record (first in the list).
4. COMPILE UNIFIED TARGETING INSTRUCTIONS: apply the Step 2 sieve and extract only
   Targeting Criteria into BQ-interpretable filter strings for Quant's 7-stage waterfall.
   Include: lob_desc values, propensity model IDs and decile tiers, province codes, lifecycle
   windows, em_dnc = 0 channel governance, and GCH recency suppression entries where present.
   Completely exclude all Segmentation Criteria from the compiled output.

Return exactly this JSON (no markdown, no explanation). deployment_deltas must contain at most
3 entries; each entry must be a single concise line focused on measurable data changes
(date-run differences, channel variations, decile-range shifts) — no narrative paragraphs
or parenthetical notes.
{{
  "strategy_summary": "<MAXIMUM 2 SENTENCES, high-level only. Sentence 1: state the deployment type, channel, cadence, and target audience (LOB, lifecycle stage, propensity tier or behavioral trigger). Sentence 2: state the business objective and any cohort consolidation logic (e.g. re-unifying prior variant splits). Do NOT include Quant instructions, waterfall details, SQL references, or technical filter parameters.>",
  "deployment_deltas": [
    "<single-line data delta — e.g. 'Nov vs Oct: NBA decile access expanded reco_1-5 to reco_1-6'>",
    "<single-line channel or date-run variation, or omit if only one deployment>",
    "<third single-line delta if present, otherwise omit>"
  ],
  "target_deployment": {{
    "campaign_name": "<name>",
    "campaign_code": "<code>",
    "campaign_sub_code": "<sub_code>",
    "cadence": "<cadence>",
    "medium": "EM",
    "campaign_purpose": "<purpose>",
    "primary_products": "<products>"
  }},
  "compiled_instructions": {{
    "target_population": "<precise plain-English description of who qualifies — Targeting Criteria only: LOB scope, lifecycle stage, propensity tier or behavioral criteria, em_dnc eligibility. Exclude all segmentation details.>",
    "filters": ["<explicit BQ-interpretable Targeting Criterion — inclusions: propensity deciles, province scope, lifecycle windows, product pairs>"],
    "exclusion_layers": ["<em_dnc = 0 — email channel governance>", "<GCH recency suppression: exclude BANs contacted via AAL/AALBAU within 30 days — include whenever the deployment records imply a contact recency gate or lookback window>"],
    "optimization_context": "<one sentence: strategic context for the Quant audit. If cross-record discrepancies were found and resolved, name the unified parameter adopted and which record took precedence.>"
  }}
}}"""


_LOGIC_DRIFT_PROMPT = """\
You are a targeting logic auditor for a Canadian telecom marketing platform.
Compare the CURRENT FILTER LIST against the HISTORICAL GOLD BLUEPRINT SUMMARY.
Respond with exactly one word: ALIGNED or DRIFTED.

HISTORICAL GOLD BLUEPRINT SUMMARY:
{targeting_summary}

CURRENT FILTER LIST:
{filters_text}

Rules:
- DRIFTED: structural intent has fundamentally changed. Examples: different LOB scope,
  opposite eligibility direction, removed a core exclusion layer, switched campaign type.
- ALIGNED: minor parameter adjustments only. Examples: different propensity decile range,
  province additions or removals, adjusted lookback window, reordered filter list.
- When uncertain, respond ALIGNED.

Respond with exactly one word — no explanation, no JSON, no punctuation."""

# Column tokens that appear in filter strings but are NOT physical DB column names.
# Skipped during the unknown_column discrepancy check.
_SCHEMA_AUDIT_IGNORE = frozenset({
    # SQL keywords and operators
    "select", "from", "where", "and", "or", "not", "in", "is", "null",
    "true", "false", "case", "when", "then", "else", "end", "like", "as",
    "exists", "between", "distinct",
    # GCH table aliases (immutable frozen contract)
    "a_gch", "b_gch", "c_gch",
    # Common table aliases
    "t", "t1", "t2", "t3", "inner_t2",
    # SQL functions (in case function-stripping regex misses an edge case)
    "upper", "lower", "trim", "date", "current_date", "max", "min", "count",
    "coalesce", "cast", "substr", "length",
    # BQ date/interval keywords
    "interval", "day", "month", "year",
    # Campaign codes that appear in GCH filter text but are not columns
    "aal", "aalbau",
})


class NexusAgent(BaseAgent):
    WORKER_ID = "nexus_v1"
    HANDLED_INTENTS: frozenset[str] = frozenset({"general_question"})
    INPUT_SCHEMA = UniversalJSONSpec
    OUTPUT_SCHEMA = IntentClassification

    def subscribe(self, spec: UniversalJSONSpec) -> None:
        """Not used — NexusAgent is invoked via classify_intent(), not subscribe/execute."""

    def execute(self) -> IntentClassification:
        """Not used — NexusAgent is invoked via classify_intent(), not subscribe/execute."""
        raise NotImplementedError(
            "NexusAgent does not use the subscribe/execute interface. "
            "Call classify_intent() directly."
        )

    def __init__(self) -> None:
        self._bq_project = os.getenv("BQ_PROJECT", "bi-srv-hsmdet-pr-7b9def")
        self._bq_table = os.getenv(
            "BQ_TABLE",
            f"{self._bq_project}.campaign_data.bq_plan_camp_deploy_mdc",
        )
        self._taxonomy: dict = {}
        self.briefs: list[dict] = []
        self._session_context: str = ""
        self._runtime_schema_snapshot: dict = {}
        self._knowledge_ctx: Optional["KnowledgeContext"] = None

    def set_knowledge_context(self, ctx: "KnowledgeContext") -> None:
        """Bind the centralised KnowledgeContext built at startup."""
        self._knowledge_ctx = ctx

    def retrieve_related_campaigns(self, query: str, top_k: int = 10) -> list[dict]:
        """Return campaign records semantically related to the query.

        Uses embedding similarity so relevance is determined by meaning rather
        than name matching. Returns an empty list when no knowledge context is
        available or if retrieval fails.
        """
        if self._knowledge_ctx is None:
            return []
        try:
            return self._knowledge_ctx.retrieve_campaigns(query, top_k=top_k)
        except Exception:
            return []

    def set_session_context(self, context: str) -> None:
        """Receive dynamic glossary/catalog context from the orchestrator for prompt injection."""
        self._session_context = context

    def set_runtime_schema_snapshot(self, snapshot_dict: dict) -> None:
        """Receive the live INFORMATION_SCHEMA snapshot injected by the orchestrator (Pillar 2).

        Stored for use by _run_discrepancy_audit() to validate that filter column
        names referenced in targeting summaries still exist in the current schema.
        """
        self._runtime_schema_snapshot = snapshot_dict

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_sizing_request_from_brief(self, brief: dict) -> Optional[AudienceSizingRequest]:
        """Path 1 — resolve a loaded campaign brief to a validated sizing request.

        If brief contains a 'deployments' key (multi-deployment matrix returned from BQ),
        runs the deployment variance analysis and strategy synthesis path before building
        the request. Falls back to single-brief construction for plain dicts (local
        glossary or legacy callers).
        """
        deployments = brief.get("deployments")
        if deployments:
            return self._build_from_deployment_matrix(deployments)

        # Single-brief fallback (local glossary or legacy caller)
        prompt = _SIZE_CAMPAIGN_PROMPT.format(
            campaign_name=brief.get("campaign_name", ""),
            campaign_code=brief.get("campaign_code", ""),
            campaign_sub_code=brief.get("campaign_sub_code", ""),
            cadence=brief.get("cadence", ""),
            medium=brief.get("medium", ""),
            campaign_purpose=brief.get("campaign_purpose", ""),
            primary_products=brief.get("primary_products", ""),
        )
        return self._parse_to_sizing_request(prompt)

    def build_brief_context_from_bq(
        self, brief: dict, gold_index: "GoldTierIndex", query: str,
    ) -> Optional[UniversalJSONSpec]:
        """Build a brief-oriented spec from BQ deployment metadata.

        Does NOT extract SQL filters -- assembles campaign identity and narrative
        context only. Called by _build_briefing_context when the gold index misses
        but BQ deployment records exist.
        """
        deployments = brief.get("deployments", [])
        if not deployments:
            return None
        # Most recent deployment (ORDER BY list_pull_date DESC from BQ) as primary anchor
        dep = deployments[0]
        camp_id = str(dep.get("camp_id", "")).strip()
        sub_camp_id = str(dep.get("sub_camp_id", "")).strip()
        campaign_name = str(dep.get("campaign_name", camp_id)).strip() or camp_id
        cadence = str(dep.get("cadence", "")).strip() or "ad-hoc"
        medium = str(dep.get("medium", "")).strip() or "unspecified"
        if not camp_id:
            return None
        gold_record = gold_index.lookup(camp_id, sub_camp_id, medium=medium, cadence=cadence)
        if gold_record is None:
            gold_record = gold_index.search(camp_id)
        campaign_tier = "GOLD" if gold_record else "BRONZE"

        # Gather channel+cadence history from ALL deployment records so the BriefingAgent
        # can reason about the full execution history of this campaign.
        dep_history: list[str] = []
        seen_dep_keys: set[str] = set()
        for d in deployments:
            d_medium = str(d.get("medium", "")).strip() or "unspecified"
            d_cadence = str(d.get("cadence", "")).strip() or "ad-hoc"
            d_date = str(d.get("list_pull_date", "")).strip()
            dep_key = f"{d_medium}::{d_cadence}"
            if dep_key not in seen_dep_keys:
                seen_dep_keys.add(dep_key)
                dep_history.append(
                    f"{d_medium} / {d_cadence}" + (f" (as of {d_date})" if d_date else "")
                )

        try:
            return UniversalJSONSpec(
                campaign_name=campaign_name,
                campaign_code=camp_id,
                campaign_sub_code=sub_camp_id,
                cadence=cadence,
                medium=medium,
                campaign_tier=campaign_tier,
                knowledge_source="bq_metadata",
                gold_blueprint_id=(
                    f"{camp_id}::{sub_camp_id}::{medium}::{cadence}" if gold_record else None
                ),
                target_population=(
                    gold_record.campaign_purpose if gold_record else campaign_name
                ),
                brief_agent_inputs={
                    "raw_prompt": query,
                    "campaign_purpose": gold_record.campaign_purpose if gold_record else "",
                    "targeting_summary": gold_record.targeting_summary if gold_record else "",
                    "brief_text": gold_record.brief_text if gold_record else "",
                    "deployment_history": dep_history,
                },
            )
        except Exception as exc:
            print(f"  [Nexus] Brief context build failed: {exc.__class__.__name__}: {exc}")
            return None

    def build_sizing_request_from_nl(self, query: str) -> Optional[AdHocSizingRequest]:
        """Path 2 — parse a natural language audience description into an ad-hoc sizing request."""
        prompt = _NL_PARSE_PROMPT.format(query=query)
        request = self._parse_to_adhoc_request(prompt)
        return request

    def classify_intent(self, query: str) -> IntentClassification:
        """Classify user intent by consulting the knowledge layer.

        Queries the knowledge layer semantically for campaigns relevant to the
        user's request so the AI can identify campaigns by natural language
        description rather than exact code. Falls back to the static code list
        when the knowledge layer is unavailable.
        Returns an IntentClassification with 5 possible intent types, confidence,
        campaign identification, and which knowledge sources were consulted.
        """
        glossary_summary = self._get_glossary_summary()
        campaign_codes = self._get_known_campaign_codes()
        sources_consulted = ["glossary", "campaign_index"]
        if self._taxonomy.get("known_targeting_patterns"):
            sources_consulted.append("gold_patterns")

        # Query the knowledge layer for campaigns relevant to this specific request.
        # This gives the AI real campaign intelligence to reason over rather than
        # bare code labels, enabling natural-language campaign identification.
        retrieved_campaigns_xml = "(knowledge layer not available — using code list only)"
        if self._knowledge_ctx is not None:
            try:
                retrieved_campaigns_xml = self._knowledge_ctx.retrieve_campaigns_xml(query, top_k=5)
                sources_consulted.append("knowledge_layer")
            except Exception:
                pass

        prompt = _INTENT_CLASSIFY_V2_PROMPT.format(
            query=query,
            glossary_summary=glossary_summary,
            campaign_codes=campaign_codes,
            retrieved_campaigns=retrieved_campaigns_xml,
        )
        try:
            raw = self._call_simple(prompt)
            data = self._extract_json(raw)
            if not data.get("knowledge_sources_consulted"):
                data["knowledge_sources_consulted"] = sources_consulted
            classification = IntentClassification(**data)
        except Exception:
            classification = IntentClassification(
                intent_type="sizing_request",
                confidence=0.5,
                campaign_identified=False,
                knowledge_sources_consulted=sources_consulted,
                reasoning="Classification failed; defaulting to sizing_request",
            )

        ThoughtDisplay.intent_classified(
            classification.intent_type,
            query,
            classification.campaign_code,
            confidence=classification.confidence,
            knowledge_sources=classification.knowledge_sources_consulted,
        )
        return classification

    def answer_general_question(self, query: str) -> str:
        """Answer a knowledge-layer question without triggering SQL or brief generation.

        Loads cross-campaign patterns and glossary context, then calls the LLM
        to provide a direct, business-focused answer in 2-4 sentences.
        """
        glossary_summary = self._get_glossary_summary()
        campaign_codes = self._get_known_campaign_codes()
        patterns_summary = self._get_cross_campaign_patterns_summary()

        prompt = (
            f'A consultant asked: "{query}"\n\n'
            "Answer this question using the knowledge context below. "
            "Be concise and business-focused (2-4 sentences). "
            "Do not mention SQL, database columns, or technical identifiers.\n\n"
            "KNOWLEDGE CONTEXT:\n"
            f"  Known glossary terms: {glossary_summary}\n"
            f"  Known campaign codes: {campaign_codes}\n"
            f"  Cross-campaign patterns: {patterns_summary}\n"
        )
        try:
            return self._call_simple(prompt)
        except Exception:
            return (
                "I don't have enough context to answer that question directly. "
                "Please try rephrasing or contact the OCTO team."
            )

    def explain_stuck_request(
        self,
        original_query: str,
        intent_type: str,
        knowledge_sources: list,
        error_details: str,
        campaign_identified: bool = False,
    ) -> str:
        """Generate a warm, conversational explanation for a request the tool could not complete.

        Tells the user what was understood, what specifically blocked progress, and
        asks one focused follow-up question. Fully agnostic to request type.
        """
        glossary_summary = self._get_glossary_summary()
        campaign_codes = self._get_known_campaign_codes()
        patterns_summary = self._get_cross_campaign_patterns_summary()

        sources_text = ", ".join(knowledge_sources) if knowledge_sources else "none"
        intent_label = intent_type.replace("_", " ") if intent_type else "unknown"

        # When no campaign was identified, explicitly prevent the AI from borrowing
        # campaign vocabulary from its background context.
        scope_instruction = (
            "IMPORTANT: This is NOT a campaign-specific request. The user did not name "
            "a specific campaign. Do not use the word 'campaign' in your response. "
            "Treat this as a general audience or data question.\n\n"
            if not campaign_identified else ""
        )

        prompt = (
            f'A user asked: "{original_query}"\n\n'
            f"The tool classified this as a {intent_label} and consulted these knowledge sources: {sources_text}.\n"
            + (f"The following issue was encountered: {error_details}\n\n" if error_details else "\n")
            + scope_instruction
            + "Write a short response (3-5 sentences) that:\n"
            "1. Acknowledges what you understood the user was asking for, in warm and plain language.\n"
            "2. Explains specifically what piece of information or context is missing or unclear.\n"
            "3. Asks one focused, direct question that the user could answer to help you proceed.\n\n"
            "Do not use bullet points. Write in a warm, friendly, conversational tone. "
            "Do not mention SQL, database columns, or technical identifiers. "
            "Do not say you are an AI. Do not apologize excessively.\n\n"
            "KNOWLEDGE CONTEXT:\n"
            f"  Known glossary terms: {glossary_summary}\n"
            f"  Cross-campaign patterns: {patterns_summary}\n"
        )
        try:
            return self._call_simple(prompt)
        except Exception:
            return (
                "I understood your request but I wasn't able to generate a result with the "
                "information I currently have. Could you share any additional context that "
                "might help me proceed? For example, any specific criteria, timeframes, or "
                "definitions that apply to this request."
            )

    def build_universal_spec(
        self,
        brief: dict,
        gold_index: "GoldTierIndex",
        schema_snapshot: dict,
    ) -> Optional[UniversalJSONSpec]:
        """Build the UniversalJSONSpec from a campaign brief dict.

        0. Checks verified_app_registry.json for a manual operator override. If one
           exists for this camp_id/sub_camp_id, its correction_text is prepended to
           the session context so the LLM prioritizes those instructions absolutely
           over any zero-shot reasoning.
        1. Derives targeting data via the existing deployment matrix logic.
        2. Extracts camp_id/sub_camp_id and calls gold_index.lookup().
        3. Runs _run_discrepancy_audit() to populate discrepancy_flags.
        4. Assembles brief_agent_inputs for BriefingAgent (Pillar 4).
        5. Returns a Pydantic-validated UniversalJSONSpec, or None on failure.
        """
        # Step 0: Check for a previously captured operator override in the registry.
        # Scans ALL deployment records (not just the most recent) so that operator
        # corrections captured for any earlier run of this campaign are not missed.
        deployments = brief.get("deployments", [])
        if deployments:
            seen_override_pairs: set[str] = set()
            for dep_row in deployments:
                peek_camp_id = str(dep_row.get("camp_id", "")).strip()
                peek_sub_camp_id = str(dep_row.get("sub_camp_id", "")).strip()
                pair_key = f"{peek_camp_id}::{peek_sub_camp_id}"
                if not peek_camp_id or not peek_sub_camp_id:
                    continue
                if pair_key in seen_override_pairs:
                    continue
                seen_override_pairs.add(pair_key)
                override_text = self._load_override_from_registry(
                    peek_camp_id, peek_sub_camp_id
                )
                if override_text:
                    print(
                        f"\n  [NEXUS AGENT] -> Manual operator override found for "
                        f"{peek_camp_id}/{peek_sub_camp_id}. Applying as absolute priority.\n"
                    )
                    override_block = (
                        "=== OPERATOR MANUAL OVERRIDE - APPLY THESE INSTRUCTIONS EXACTLY ===\n"
                        f"{override_text}\n"
                        "=== END OVERRIDE (this takes absolute precedence over any inferred logic) ===\n\n"
                    )
                    self._session_context = override_block + self._session_context
                    break  # First override found takes precedence; do not stack multiple

        # Step 1: Derive the AudienceSizingRequest via existing brief logic
        ThoughtDisplay.progress("Translating your campaign brief into targeting rules...")
        sizing_request = self.build_sizing_request_from_brief(brief)
        if sizing_request is None:
            return None

        # Step 2: Gold tier lookup
        camp_id = sizing_request.campaign_code
        sub_camp_id = sizing_request.campaign_sub_code
        gold_record = gold_index.lookup(
            camp_id, sub_camp_id,
            medium=sizing_request.medium,
            cadence=sizing_request.cadence,
        )

        if gold_record is not None:
            campaign_tier = "GOLD"
            gold_blueprint_id = (
                f"{camp_id}::{sub_camp_id}::{sizing_request.medium}::{sizing_request.cadence}"
            )
            knowledge_source = "brief_text" if gold_record.brief_text else "bq_metadata"
        else:
            campaign_tier = "BRONZE"
            gold_blueprint_id = None
            knowledge_source = "bq_metadata" if brief.get("deployments") else "nl_only"

        ThoughtDisplay.knowledge_lookup(
            sizing_request.campaign_name,
            campaign_tier,
            0.90 if campaign_tier == "GOLD" else 0.60,
        )

        # Step 3: Discrepancy audit (non-blocking; populates advisory flags)
        filters = list(sizing_request.filters or [])
        exclusion_layers = list(sizing_request.exclusion_layers or [])
        discrepancy_flags = self._run_discrepancy_audit(
            filters=filters,
            exclusion_layers=exclusion_layers,
            schema_snapshot=schema_snapshot,
            gold_record=gold_record,
            campaign_code=camp_id,
        )

        # Step 4: Brief agent inputs for BriefingAgent (Pillar 4 consumer)
        brief_agent_inputs = {
            "raw_prompt": brief.get("raw_prompt", ""),
            "campaign_name": sizing_request.campaign_name,
            "targeting_summary": gold_record.targeting_summary if gold_record else "",
            "segment_summary": gold_record.segment_summary if gold_record else "",
            "brief_text": gold_record.brief_text if gold_record else "",
        }

        # Derive which DNC column governs this channel
        _dnc_col_map = {"EM": "em_dnc", "SMS": "sms_dnc", "OB": "ob_dnc", "DM": "dm_dnc"}
        medium_upper = sizing_request.medium.upper().strip()
        dnc_channels = [_dnc_col_map[medium_upper]] if medium_upper in _dnc_col_map else []

        # Step 5: Validate and return the UniversalJSONSpec
        try:
            return UniversalJSONSpec(
                campaign_name=sizing_request.campaign_name,
                campaign_code=camp_id,
                campaign_sub_code=sub_camp_id,
                cadence=sizing_request.cadence,
                medium=sizing_request.medium,
                campaign_tier=campaign_tier,
                knowledge_source=knowledge_source,
                gold_blueprint_id=gold_blueprint_id,
                target_population=sizing_request.target_population,
                filters=sizing_request.filters,
                exclusion_layers=sizing_request.exclusion_layers,
                optimization_context=sizing_request.optimization_context,
                bq_project=sizing_request.bq_project,
                bq_dataset=sizing_request.bq_dataset,
                discrepancy_flags=discrepancy_flags,
                runtime_schema_snapshot=schema_snapshot if schema_snapshot else None,
                brief_agent_inputs=brief_agent_inputs,
                require_gch_suppression=any(
                    "gch" in e.lower() for e in exclusion_layers
                ),
                dnc_channels=dnc_channels,
            )
        except ValidationError as exc:
            print(
                f"  [Nexus] UniversalJSONSpec validation failed"
                f" ({exc.error_count()} field error(s))"
            )
            return None
        except Exception as exc:
            print(f"  [Nexus] Spec build error: {exc.__class__.__name__}: {exc}")
            return None

    def route_with_retry(
        self,
        request: AudienceSizingRequest,
        quant: "QuantAgent",
    ) -> Optional[QuantAuditLog]:
        """Send request to Quant. On failure, attempt exactly ONE correction pass."""
        result = quant.audit(request.model_dump())

        if isinstance(result, QuantAuditLog):
            return result

        # First attempt failed — try once with taxonomy-guided correction
        print("  [Nexus] Audit returned an error. Running one correction pass...")
        corrected = self._correct_sizing_request(result)
        if corrected is None:
            _print_terminal_error()
            return None

        result2 = quant.audit(corrected.model_dump())
        if isinstance(result2, QuantAuditLog):
            return result2

        _print_terminal_error()
        return None

    # ------------------------------------------------------------------
    # Knowledge layer helpers — used by classify_intent
    # ------------------------------------------------------------------

    def _get_glossary_summary(self) -> str:
        """Return a compact list of known glossary terms for classification context."""
        try:
            data = json.loads((_ROOT_DIR / "glossary.json").read_text(encoding="utf-8"))
            acronyms = list(data.get("acronyms", {}).keys())
            campaigns_in_glossary = list(data.get("campaigns", {}).keys())
            user_terms = list(data.get("user_defined_terms", {}).keys())
            all_terms = acronyms + campaigns_in_glossary + user_terms
            return ", ".join(all_terms[:30]) if all_terms else "(none)"
        except Exception:
            return "(glossary unavailable)"

    def _get_known_campaign_codes(self) -> str:
        """Return known campaign codes from taxonomy and knowledge index."""
        codes: list[str] = []
        if self._taxonomy:
            codes.extend(list(self._taxonomy.get("campaign_classifications", {}).keys())[:10])
        # Use KnowledgeContext if available (avoids re-reading the file)
        if self._knowledge_ctx is not None:
            # KnowledgeContext already has all campaigns loaded
            try:
                from core.knowledge_context import KnowledgeContext  # noqa: F401
                for camp in self._knowledge_ctx._campaigns[:20]:
                    code = camp.get("camp_id", "")
                    if code and code not in codes:
                        codes.append(code)
            except Exception:
                pass
        else:
            try:
                data = json.loads(
                    (_ROOT_DIR / "knowledge_base" / "artifacts" / "semantic_knowledge_index.json")
                    .read_text(encoding="utf-8")
                )
                for rec in data.get("campaigns", [])[:20]:
                    code = rec.get("camp_id", "")
                    if code and code not in codes:
                        codes.append(code)
            except Exception:
                pass
        return ", ".join(codes) if codes else "(none loaded)"

    def _get_cross_campaign_patterns_summary(self) -> str:
        """Return a compact summary of cross-campaign targeting patterns."""
        try:
            data = json.loads(
                (_ROOT_DIR / "knowledge_base" / "artifacts" / "cross_campaign_patterns.json")
                .read_text(encoding="utf-8")
            )
            patterns = data.get("insights", {}).get("targeting_patterns", [])
            top = [p.get("pattern", "") for p in patterns[:8] if p.get("pattern")]
            return ", ".join(top) if top else "(none)"
        except Exception:
            return "(patterns unavailable)"

    # ------------------------------------------------------------------
    # Deployment variance analysis — multi-deployment synthesis (Path 1)
    # ------------------------------------------------------------------

    def _build_from_deployment_matrix(
        self, deployments: list[dict]
    ) -> Optional[AudienceSizingRequest]:
        """Orchestrate the variance analysis and build the audience sizing request."""
        analysis = self._analyze_deployment_variance(deployments)
        if analysis is None:
            # Analysis failed — fall back to treating the most recent deployment as a plain brief
            return self.build_sizing_request_from_brief(deployments[0])

        request = self._build_sizing_request_from_analysis(analysis)
        return request

    def _analyze_deployment_variance(self, deployments: list[dict]) -> Optional[dict]:
        """Call the LLM to identify cross-deployment deltas and synthesize targeting strategy."""
        # Convert BQ-native types (datetime.date, Decimal, etc.) to plain strings
        # so json.dumps does not raise on non-serializable objects.
        safe = [
            {k: str(v) if v is not None else "" for k, v in row.items()}
            for row in deployments
        ]
        prompt = _DEPLOYMENT_ANALYSIS_PROMPT.format(
            n=len(safe),
            deployments_json=json.dumps(safe, indent=2, ensure_ascii=False),
        )
        try:
            raw = self._call_with_cached_taxonomy(prompt)
            return self._extract_json(raw)
        except Exception as exc:
            print(
                f"  [Nexus] Deployment analysis warning: {exc.__class__.__name__} "
                "— falling back to single-brief mode"
            )
            return None

    def _build_sizing_request_from_analysis(
        self, analysis: dict
    ) -> Optional[AudienceSizingRequest]:
        """Merge target_deployment + compiled_instructions into a validated AudienceSizingRequest."""
        target = analysis.get("target_deployment", {})
        compiled = analysis.get("compiled_instructions", {})
        try:
            return AudienceSizingRequest(
                campaign_name=target.get("campaign_name", ""),
                campaign_code=target.get("campaign_code", ""),
                campaign_sub_code=target.get("campaign_sub_code", ""),
                cadence=target.get("cadence", ""),
                medium=target.get("medium", ""),
                target_population=compiled.get("target_population", ""),
                filters=compiled.get("filters") or [],
                exclusion_layers=compiled.get("exclusion_layers") or None,
                optimization_context=compiled.get("optimization_context") or None,
                bq_project="bi-srv-hsmdet-pr-7b9def",
                bq_dataset="campaign_data",
            )
        except ValidationError as exc:
            print(
                f"  [Nexus] Synthesis payload validation failed "
                f"({exc.error_count()} field error(s))"
            )
            return None
        except Exception as exc:
            print(f"  [Nexus] Synthesis build error: {exc.__class__.__name__}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Discrepancy audit — non-blocking, advisory only
    # ------------------------------------------------------------------

    def _run_discrepancy_audit(
        self,
        filters: list[str],
        exclusion_layers: list[str],
        schema_snapshot: dict,
        gold_record: "Optional[GoldCampaignRecord]",
        campaign_code: str = "",
    ) -> list[str]:
        """Non-blocking audit. Returns list of advisory flag strings.

        Execution proceeds regardless of flag content. Four flag types:

          unknown_column
            Fired when a filter string references a column not found in the
            live SchemaSnapshot. Column name extracted by parsing the token
            immediately before =, IN, NOT IN, LIKE, or IS operators.

          missing_standard_exclusion
            Fired when none of the filters or exclusion_layers contain
            standard_exclusions, sub_status, or primary_sub patterns.
            These are applied at CTE steps 2-3 in the waterfall.

          gch_bypass_detected
            Fired for AAL campaigns when no 'GCH' or 'recency suppression'
            string is present in exclusion_layers.

          logic_drift
            Fired for GOLD campaigns when an LLM evaluation detects that the
            new filters have structurally diverged from the gold_record
            targeting summary. Uses _call_simple() with a compact prompt.
        """
        flags: list[str] = []
        all_clauses = list(filters) + list(exclusion_layers)
        blob = " ".join(all_clauses)

        # --- 1. unknown_column ---
        if schema_snapshot:
            known_columns: set[str] = set()
            for col in schema_snapshot.get("columns", []):
                if isinstance(col, dict):
                    known_columns.add(col.get("column_name", "").lower())
                elif hasattr(col, "column_name"):
                    known_columns.add(col.column_name.lower())

            if known_columns:
                # Strip function wrappers so UPPER(province) yields "province"
                clean = re.sub(
                    r"\b(?:UPPER|LOWER|TRIM|DATE)\s*\(([^)]+)\)",
                    r"\1",
                    blob,
                    flags=re.IGNORECASE,
                )
                col_pat = re.compile(
                    r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*"
                    r"(?:=|(?:NOT\s+)?IN\b|LIKE\b|IS(?:\s+NOT)?\b)",
                    re.IGNORECASE,
                )
                flagged_cols: set[str] = set()
                for m in col_pat.finditer(clean):
                    col_name = m.group(1).lower()
                    if (
                        col_name not in _SCHEMA_AUDIT_IGNORE
                        and col_name not in known_columns
                        and col_name not in flagged_cols
                    ):
                        flagged_cols.add(col_name)
                        flags.append(
                            f"unknown_column: '{col_name}' not found in live"
                            " adobe schema -- verify column name"
                        )

        # --- 2. missing_standard_exclusion ---
        _std_patterns = ("standard_exclusions", "sub_status", "primary_sub")
        if not any(p in blob.lower() for p in _std_patterns):
            flags.append(
                "missing_standard_exclusion: no standard suppression pattern"
                " detected (standard_exclusions / sub_status / primary_sub)"
                " -- confirm base filters are applied at CTE steps 2-3"
            )

        # --- 3. gch_bypass_detected — AAL campaigns only ---
        is_aal = (
            campaign_code.upper() == "AAL"
            or (gold_record is not None and gold_record.camp_id.upper() == "AAL")
        )
        if is_aal:
            excl_blob = " ".join(exclusion_layers).lower()
            if "gch" not in excl_blob and "recency suppression" not in excl_blob:
                flags.append(
                    "gch_bypass_detected: AAL campaign has no Global Contact"
                    " History (GCH) recency suppression layer in"
                    " exclusion_layers -- GCH anti-join is required for all"
                    " AAL executions"
                )

        # --- 4. logic_drift — GOLD path only, LLM-evaluated ---
        if gold_record is not None and gold_record.targeting_summary:
            filters_text = (
                "\n".join(f"  - {f}" for f in filters)
                if filters
                else "  (none)"
            )
            drift_prompt = _LOGIC_DRIFT_PROMPT.format(
                targeting_summary=gold_record.targeting_summary[:1200],
                filters_text=filters_text,
            )
            try:
                response = self._call_simple(drift_prompt)
                if "DRIFTED" in response.upper():
                    flags.append(
                        "logic_drift: filter intent has structurally diverged"
                        " from the gold blueprint targeting summary"
                        " -- review side-by-side comparison before execution"
                    )
            except Exception:
                # Non-blocking: skip drift check if the LLM call fails
                pass

        return flags

    # ------------------------------------------------------------------
    # Brief loading — BQ first, local glossary fallback
    # ------------------------------------------------------------------

    def _load_taxonomy_briefs(self) -> list[dict]:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from google.cloud import bigquery  # type: ignore

            client = bigquery.Client(project=self._bq_project)
            query = _TAXONOMY_BQ_QUERY.format(table=self._bq_table)
            from core.resilience import assert_read_only_sql
            assert_read_only_sql(query)
            rows = [dict(r) for r in client.query(query).result()]
            if rows:
                return rows
            print("  [Nexus] BQ returned 0 rows — loading from local glossary")
        except Exception as exc:
            print(
                f"  [Nexus] BQ unavailable ({exc.__class__.__name__})"
                " — loading from local glossary"
            )
        return self._load_local_briefs()

    def _load_local_briefs(self) -> list[dict]:
        briefs_dir = _NEXUS_DIR / "glossary" / "briefs"
        if not briefs_dir.exists():
            return []
        out: list[dict] = []
        for f in sorted(briefs_dir.glob("*.json"))[:5]:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                camp = data.get("campaign", {})
                targeting = data.get("targeting", {})
                medium_raw = camp.get("medium", [])
                medium_str = medium_raw[0] if isinstance(medium_raw, list) and medium_raw else str(medium_raw)
                out.append({
                    "campaign_name": camp.get("id", f.stem),
                    "campaign_code": camp.get("portfolio", "UNKNOWN"),
                    "campaign_sub_code": camp.get("id", ""),
                    "cadence": camp.get("cadence", ""),
                    "medium": medium_str,
                    "campaign_purpose": camp.get("purpose", ""),
                    "primary_products": data.get("offer", {}).get("product", ""),
                    "brand": camp.get("brand", ""),
                    "include_criteria": [
                        i.get("term", "") for i in targeting.get("include", [])
                    ],
                    "exclude_criteria": [
                        e.get("term", "") for e in targeting.get("exclude", [])
                    ],
                })
            except Exception:
                continue
        return out[:5]

    def _find_brief_for_campaign(self, campaign_hint: str) -> "dict | None":
        """Query BQ on-demand for active deployment records matching campaign_hint.

        Searches by exact camp_id or sub_camp_id match (case-insensitive).
        The hint is sanitized to alphanumeric + safe punctuation before use
        in the query string to prevent injection.
        """
        if not campaign_hint:
            return None

        # Restrict to characters that appear in real campaign codes.
        safe_hint = re.sub(r"[^A-Z0-9_\- ]", "", campaign_hint.upper().strip())
        if not safe_hint:
            return None

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from google.cloud import bigquery  # type: ignore

            client = bigquery.Client(project=self._bq_project)
            query_str = f"""
            SELECT *
            FROM `{self._bq_table}`
            WHERE current_ind = 1
              AND closed_ind = 0
              AND UPPER(data_status) <> 'CANCELLED'
              AND (UPPER(camp_id) = '{safe_hint}' OR UPPER(sub_camp_id) = '{safe_hint}')
            ORDER BY list_pull_date DESC
            LIMIT 100
            """
            from core.resilience import assert_read_only_sql
            assert_read_only_sql(query_str)
            rows = [dict(r) for r in client.query(query_str).result()]
            if rows:
                return {"deployments": rows}
        except Exception:
            pass

        return None

    def _load_override_from_registry(
        self, camp_id: str, sub_camp_id: str
    ) -> Optional[str]:
        """Check verified_app_registry.json for a manual correction override.

        Returns the correction_text string if a record exists for this
        camp_id/sub_camp_id with a non-empty correction_text field (written by
        HITLAuditLoop._handle_no()). Returns None if no override is found.

        Silently ignores any read or parse errors so a missing or malformed
        registry never aborts the build_universal_spec() cycle.
        """
        registry_path = _ROOT_DIR / "verified_app_registry.json"
        try:
            data = json.loads(registry_path.read_text(encoding="utf-8"))
            for record in data.get("records", []):
                if (
                    record.get("camp_id", "").upper() == camp_id.upper()
                    and record.get("sub_camp_id", "").upper() == sub_camp_id.upper()
                    and record.get("correction_text")
                ):
                    return str(record["correction_text"])
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Taxonomy build — one-time, no caching needed
    # ------------------------------------------------------------------

    def _build_taxonomy(self) -> dict:
        from datetime import date

        if not self.briefs:
            return {
                "taxonomy_version": str(date.today()),
                "campaign_classifications": {},
                "historical_strategies": [],
                "known_targeting_patterns": [],
                "medium_effectiveness_notes": {},
                "standard_exclusions": [],
            }

        prompt = _TAXONOMY_BUILD_PROMPT.format(
            n=len(self.briefs),
            briefs_json=json.dumps(self.briefs, indent=2, ensure_ascii=False),
            date=str(date.today()),
        )
        try:
            raw = self._call_simple(prompt)
            return self._extract_json(raw)
        except Exception as exc:
            print(f"  [Nexus] Taxonomy build warning: {exc.__class__.__name__} — using minimal taxonomy")
            return {
                "taxonomy_version": str(date.today()),
                "campaign_classifications": {},
                "historical_strategies": [],
                "known_targeting_patterns": [],
                "medium_effectiveness_notes": {},
                "standard_exclusions": [],
            }

    # ------------------------------------------------------------------
    # Request construction and retry
    # ------------------------------------------------------------------

    def _parse_to_sizing_request(self, prompt: str) -> Optional[AudienceSizingRequest]:
        try:
            raw = self._call_with_cached_taxonomy(prompt)
            data = self._extract_json(raw)
            return AudienceSizingRequest(**data)
        except ValidationError as exc:
            print(f"  [Nexus] Payload validation failed ({exc.error_count()} field error(s))")
            return None
        except Exception as exc:
            print(f"  [Nexus] Request build error: {exc.__class__.__name__}: {exc}")
            return None

    def _parse_to_adhoc_request(self, prompt: str) -> Optional[AdHocSizingRequest]:
        try:
            raw = self._call_with_cached_taxonomy(prompt)
            data = self._extract_json(raw)
            return AdHocSizingRequest(**data)
        except ValidationError as exc:
            print(f"  [Nexus] Ad-hoc payload validation failed ({exc.error_count()} field error(s))")
            return None
        except Exception as exc:
            print(f"  [Nexus] Ad-hoc request build error: {exc.__class__.__name__}: {exc}")
            return None

    def _correct_sizing_request(
        self, error: NexusErrorPayload
    ) -> Optional[AudienceSizingRequest]:
        prompt = _RETRY_PROMPT.format(
            error_type=error.error_type,
            error_summary=error.error_summary,
            retry_hint=error.retry_hint,
            original_request_json=json.dumps(error.original_request, indent=2, ensure_ascii=False),
        )
        try:
            raw = self._call_with_cached_taxonomy(prompt)
            data = self._extract_json(raw)
            if data.get("correctable") is False:
                return None
            return AudienceSizingRequest(**data)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    def _call_simple(self, user_prompt: str) -> str:
        """Single call — no caching. Used for the one-time taxonomy build."""
        system = (
            _NEXUS_SYSTEM + "\n\n" + self._session_context
            if self._session_context
            else _NEXUS_SYSTEM
        )
        return ask_ai(user_prompt, system=system, temperature=0, max_tokens=_MAX_TOKENS_BUILD)

    def _call_with_cached_taxonomy(self, user_query: str) -> str:
        """Call Gemini with the full knowledge context prepended to the prompt.

        When a KnowledgeContext is available (startup injection via set_knowledge_context),
        uses it as the knowledge block -- it contains all loaded GOLD campaigns, business
        rules, glossary, and patterns. Falls back to the sparse taxonomy for backward compat.
        """
        if self._knowledge_ctx is not None:
            _campaign_count = getattr(self._knowledge_ctx, "campaign_count", "all")
            cached_text = (
                "VIBE OCTO COMPLETE KNOWLEDGE BASE\n"
                f"(Authoritative reference -- {_campaign_count} GOLD campaigns, business rules, "
                "glossary, and schema)\n\n"
                + self._knowledge_ctx.nexus_context
            )
        else:
            cached_text = (
                "STRATEGIC TAXONOMY MATRIX\n"
                "(Authoritative reference -- cross-check all requests against this context first)\n\n"
                + json.dumps(self._taxonomy, indent=2, ensure_ascii=False)
            )

        # Note: Gemini has no equivalent to Fuel iX/Anthropic's ephemeral
        # prompt-caching, so the knowledge block and query are just
        # concatenated into one prompt rather than sent as separate,
        # cache-tagged content blocks.
        prompt = cached_text + "\n\n" + user_query

        system = (
            _NEXUS_SYSTEM + "\n\n" + self._session_context
            if self._session_context
            else _NEXUS_SYSTEM
        )
        return ask_ai(prompt, system=system, temperature=0, max_tokens=_MAX_TOKENS_QUERY)

    @staticmethod
    def _extract_json(text: str) -> dict:
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass
        match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
        if match:
            return json.loads(match.group(1))
        start, end = text.find("{"), text.rfind("}") + 1
        if start != -1 and end > start:
            return json.loads(text[start:end])
        raise ValueError(f"No valid JSON in response. First 300 chars: {text[:300]!r}")




def _print_terminal_error() -> None:
    print(
        "\n[ERROR]: Unable to locate data points or interpret the business context. "
        "Please reach out to the OCTCO team leads for assistance."
    )
