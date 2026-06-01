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

_NEXUS_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _NEXUS_DIR.parent

for _p in [str(_NEXUS_DIR), str(_ROOT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

load_dotenv(_NEXUS_DIR / ".env")

from pydantic_schemas import AdHocSizingRequest, AudienceSizingRequest, NexusErrorPayload, QuantAuditLog

if TYPE_CHECKING:
    from quant_agent import QuantAgent

_FUELIX_BASE = "https://api.fuelix.ai"
_DEFAULT_MODEL = "claude-sonnet-4"
_MAX_TOKENS_BUILD = 4096
_MAX_TOKENS_QUERY = 8192

_NEXUS_SYSTEM = (
    "You are Vibe OCTO Nexus, a senior management consulting AI embedded in a "
    "Canadian telecom marketing team (TELUS / Koodo). "
    "You analyze campaign briefs with McKinsey-level precision, extract structured "
    "targeting parameters from natural language, classify campaigns against historical "
    "patterns, and emit validated JSON payloads for downstream audience sizing. "
    "Always reference the provided taxonomy matrix before inferring new patterns. "
    "Return ONLY valid JSON — no explanation, no markdown, no trailing text."
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
  "bq_dataset": "adobe"
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
  "bq_dataset": "adobe"
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

_INTENT_CLASSIFY_PROMPT = """\
A consultant submitted the following request to a Canadian telecom marketing AI:
  "{query}"

Classify this into exactly one of two workflows using the priority rules below.
Evaluate each priority in order and stop at the first match.

PRIORITY 1 — CAMPAIGN CODE OVERRIDE (absolute priority — evaluate before all other rules):
  If the request explicitly names a corporate campaign code or sub-campaign code
  — including 'AAL', 'AALBAU', or any other recognized campaign identifier —
  classify immediately as WORKFLOW_B. This overrides ALL other rules without exception,
  including interrogative phrasing such as "What's the size of...", "How many...", or
  "Count...". A named campaign code is always a Structured Campaign Execution Request,
  regardless of how the question is framed.
  Examples: "What's the size of the AALBAU campaign?",
            "How many subscribers are in the AAL monthly email?",
            "Show me the AAL audience count",
            "Pull the AALBAU playbook"

PRIORITY 2 — WORKFLOW_A: Ad-Hoc Exploratory Request (apply only if Priority 1 did not match):
  If the request is phrased as a metric or probing question with NO named campaign code
  — i.e. it opens with or is semantically equivalent to "How many...", "Count...",
  "What is the size of...", "Give me a count of...", or any other interrogative asking
  for a number against a generic audience description — classify as WORKFLOW_A.
  Do NOT trigger a campaign playbook lookup for these.
  Examples: "How many customers in AB or BC?",
            "Count postpaid subscribers with SHS eligible",
            "How many Koodo prepaid customers are MTM?",
            "What is the size of the TELUS postpaid base?"

WORKFLOW_B — Structured Campaign Execution Request:
  All requests that name a specific campaign code (reached via Priority 1), or that use
  action verbs such as "Size the...", "Run...", "Execute...", "Set up...", or
  "Pull the playbook for..." targeting a named campaign.
  Examples: "Size the AAL monthly email campaign",
            "Run the Koodo winback outbound brief",
            "Execute the AAL voice analytics weekly",
            "Pull the playbook for the TELUS AAL internet campaign"

Return exactly this JSON — no markdown, no explanation:
{{
  "workflow": "WORKFLOW_A",
  "campaign_hint": null
}}
or:
{{
  "workflow": "WORKFLOW_B",
  "campaign_hint": "<campaign name or label extracted from the request>"
}}"""


_DEPLOYMENT_ANALYSIS_PROMPT = """You are analyzing {n} deployment record(s) for the AAL Monthly Email (EM) campaign retrieved from a Canadian
telecom marketing data store. Each record represents a distinct list pull or execution run of the
monthly email send (camp_id = 'AAL', sub_camp_id = 'AALBAU', medium = 'EM'). Records may reflect
evolving email targeting cohorts — shifts in NBA propensity tier access, geographic scope, lifecycle
window boundaries, product eligibility criteria, or behavioral exclusion rules. The databrief_link
column identifies the source brief document for each run's full targeting ruleset.

Deployment Records (sorted most-recent first):
{deployments_json}

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
4. COMPILE explicit instructions: extract the target deployment's full parameter set into
   BQ-interpretable filter strings that Quant can directly assemble into a 7-stage waterfall
   CTE for the email channel. Be precise — include lob_desc values, propensity model IDs,
   province codes, lifecycle windows, em_dnc = 0 channel governance, and eligibility pairs
   where relevant.

Return exactly this JSON (no markdown, no explanation). deployment_deltas must contain at most 3 entries;
each entry must be a single concise line focused on measurable data changes (date-run differences,
channel variations, decile-range shifts) — no narrative paragraphs or parenthetical notes.
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
    "target_population": "<precise plain-English description of who qualifies for the email send — include LOB, lifecycle stage, propensity tier or behavioral criteria, and confirm em_dnc eligibility>",
    "filters": ["<explicit BQ-interpretable filter criterion for this email deployment>"],
    "exclusion_layers": ["<explicit exclusion layer — e.g. em_dnc = 0 for email channel eligibility>"],
    "optimization_context": "<one sentence: strategic context for the Quant audit reflecting this monthly email deployment's specific targeting approach and audience scope>"
  }}
}}"""


class NexusAgent:
    def __init__(self) -> None:
        self._api_key = os.getenv("FUELIX_API_KEY")
        if not self._api_key:
            raise RuntimeError("FUELIX_API_KEY not set in Vibe OCTO Nexus/.env")
        self._model = os.getenv("FUELIX_MODEL", _DEFAULT_MODEL)
        self._bq_project = os.getenv("BQ_PROJECT", "bi-srv-hsmdet-pr-7b9def")
        self._bq_table = os.getenv(
            "BQ_TABLE",
            f"{self._bq_project}.campaign_data.bq_plan_camp_deploy_mdc",
        )
        self._taxonomy: dict = {}
        self.briefs: list[dict] = []

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

    def build_sizing_request_from_nl(self, query: str) -> Optional[AdHocSizingRequest]:
        """Path 2 — parse a natural language audience description into an ad-hoc sizing request."""
        prompt = _NL_PARSE_PROMPT.format(query=query)
        request = self._parse_to_adhoc_request(prompt)
        if request and request.optimization_context:
            thin = "-" * 44
            print(f"\n  [Nexus Strategy]\n  {thin}")
            print(f"  {request.optimization_context}\n")
        return request

    def classify_and_route(self, query: str) -> tuple[str, "dict | None"]:
        """Classify user intent and return (workflow, payload).

        Returns ("WORKFLOW_A", None) for ad-hoc exploratory queries.
        Returns ("WORKFLOW_B", brief_dict) for named campaign execution requests,
        or falls back to ("WORKFLOW_A", None) if no matching campaign is located.
        """
        try:
            raw = self._call_simple(_INTENT_CLASSIFY_PROMPT.format(query=query))
            data = self._extract_json(raw)
        except Exception:
            return "WORKFLOW_A", None

        workflow = data.get("workflow", "WORKFLOW_A")

        if workflow == "WORKFLOW_B":
            campaign_hint = (data.get("campaign_hint") or "").strip()
            print(f"  Recognized WORKFLOW B: Structured Campaign Execution Request.")
            print(f"  Searching for campaign: '{campaign_hint}'...\n")
            brief = self._find_brief_for_campaign(campaign_hint)
            if brief:
                return "WORKFLOW_B", brief
            return "WORKFLOW_A", None

        print("  Recognized WORKFLOW A: Ad-Hoc Exploratory Request.\n")
        return "WORKFLOW_A", None

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
    # Deployment variance analysis — multi-deployment synthesis (Path 1)
    # ------------------------------------------------------------------

    def _build_from_deployment_matrix(
        self, deployments: list[dict]
    ) -> Optional[AudienceSizingRequest]:
        """Orchestrate the variance analysis, print strategy headers, build the request."""
        print(
            "  [NEXUS AGENT] -> Analyzing deployment variations and "
            "synthesizing portfolio strategy...\n"
        )

        analysis = self._analyze_deployment_variance(deployments)
        if analysis is None:
            # Analysis failed — fall back to treating the most recent deployment as a plain brief
            return self.build_sizing_request_from_brief(deployments[0])

        summary = analysis.get("strategy_summary", "")
        if summary:
            print(f"  [NEXUS AGENT] -> CAMPAIGN STRATEGY SUMMARY: {summary}\n")

        deltas = [d for d in analysis.get("deployment_deltas", []) if d]
        if deltas and len(deployments) > 1:
            thin = "-" * 44
            print(f"  Deployment Variance Detected Across {len(deployments)} Run(s):")
            print(f"  {thin}")
            for delta in deltas[:3]:
                print(f"    * {delta}")
            print()

        request = self._build_sizing_request_from_analysis(analysis)
        if request is not None:
            _print_criteria_block(request)
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
                bq_dataset="adobe",
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
    # Brief loading — BQ first, local glossary fallback
    # ------------------------------------------------------------------

    def _load_taxonomy_briefs(self) -> list[dict]:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from google.cloud import bigquery  # type: ignore

            client = bigquery.Client(project=self._bq_project)
            query = _TAXONOMY_BQ_QUERY.format(table=self._bq_table)
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
        """Query BQ on-demand for the most recent campaign matching the hint string."""
        if not campaign_hint:
            return None

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from google.cloud import bigquery  # type: ignore

            client = bigquery.Client(project=self._bq_project)
            query_str = f"""
            SELECT
                campaign        AS campaign_name,
                camp_id         AS campaign_code,
                sub_camp_id     AS campaign_sub_code,
                medium,
                cadence,
                campaign_purpose,
                primary_products,
                databrief_link,
                list_pull_date
            FROM `{self._bq_table}`
            WHERE current_ind   = 1
              AND closed_ind    = 0
              AND camp_id       = 'AAL'
              AND sub_camp_id   = 'AALBAU'
              AND UPPER(medium) = 'EM'
            ORDER BY list_pull_date DESC
            LIMIT 5
            """
            rows = [dict(r) for r in client.query(query_str).result()]
            if rows:
                return {"deployments": rows}
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
        resp = requests.post(
            f"{_FUELIX_BASE}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _NEXUS_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": _MAX_TOKENS_BUILD,
                "temperature": 0,
            },
            timeout=180,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    def _call_with_cached_taxonomy(self, user_query: str) -> str:
        """Call Fuel iX with the taxonomy matrix pinned as an ephemeral cached block.

        The first content block carries cache_control: ephemeral per the Anthropic
        prompt-caching spec (beta header activates it). On a cache hit the input
        tokens for the taxonomy block are charged at ~10% of normal cost.
        If the endpoint does not support caching, the call succeeds without it.
        """
        taxonomy_block = {
            "type": "text",
            "text": (
                "STRATEGIC TAXONOMY MATRIX\n"
                "(Authoritative reference — cross-check all requests against this context first)\n\n"
                + json.dumps(self._taxonomy, indent=2, ensure_ascii=False)
            ),
            "cache_control": {"type": "ephemeral"},
        }
        query_block = {"type": "text", "text": user_query}

        resp = requests.post(
            f"{_FUELIX_BASE}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "anthropic-beta": "prompt-caching-2024-07-31",
            },
            json={
                "model": self._model,
                "messages": [
                    {"role": "system", "content": _NEXUS_SYSTEM},
                    {"role": "user", "content": [taxonomy_block, query_block]},
                ],
                "max_tokens": _MAX_TOKENS_QUERY,
                "temperature": 0,
            },
            timeout=180,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

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


def _extract_criteria_fields(request: "AudienceSizingRequest") -> dict:
    """Parse a validated AudienceSizingRequest into display-ready targeting fields."""
    all_filters = list(request.filters or []) + list(request.exclusion_layers or [])
    blob = " ".join(all_filters)

    # Core Product — NBA model classn_nm + predict_modl_id
    m_classn = re.search(r"classn_nm\s*=\s*['\"]([^'\"]+)['\"]", blob, re.IGNORECASE)
    if not m_classn:
        # Fallback: unquoted value (e.g. classn_nm = ADD_A_LINE)
        m_classn = re.search(r"classn_nm\s*=\s*([A-Z][A-Z_0-9]+)", blob, re.IGNORECASE)
    m_modl   = re.search(r"predict_modl_id\s*=\s*(\d+)", blob, re.IGNORECASE)
    if m_classn and m_modl:
        core_product = f"{m_classn.group(1).upper()} (Model {m_modl.group(1)})"
    elif m_classn:
        core_product = m_classn.group(1).upper()
    else:
        core_product = (request.target_population or "N/A").split(".")[0].strip()

    # Propensity tiers — seg_nm IN ('reco_1', ...); fallback: any reco_N reference in blob
    m_seg = re.search(r"seg_nm\s+in\s*\(([^)]+)\)", blob, re.IGNORECASE)
    if m_seg:
        reco_nums = sorted(int(n) for n in re.findall(r"reco_(\d+)", m_seg.group(1), re.IGNORECASE))
    else:
        reco_nums = sorted(set(int(n) for n in re.findall(r"reco_(\d+)", blob, re.IGNORECASE)))
    if reco_nums:
        propensity = (
            f"Deciles {reco_nums[0]}-{reco_nums[-1]} "
            f"(reco_{reco_nums[0]} to reco_{reco_nums[-1]})"
        )
    else:
        propensity = "N/A"

    # Segment Focus — lifecycle and device financing signals
    segment_parts: list[str] = []
    if re.search(r"commit_start_date\s+is\s+null", blob, re.IGNORECASE):
        segment_parts.append("BYOD")
    if re.search(r"\bhp_ind\s*=\s*1\b", blob, re.IGNORECASE):
        segment_parts.append("Hardware Subsidized")
    m_renewal = re.search(
        r"commit_end_date\s*<=\s*DATE_ADD.*?INTERVAL\s+(\d+)\s+MONTH", blob, re.IGNORECASE
    )
    if m_renewal:
        segment_parts.append(f"T-{m_renewal.group(1)} Renewal Window")
    elif re.search(r"commit_end_date\s*<\s*CURRENT_DATE", blob, re.IGNORECASE):
        segment_parts.append("Month-to-Month")
    if segment_parts:
        segment_focus = " + ".join(segment_parts)
    elif request.medium.upper().strip() == "EM":
        segment_focus = "Unified Email Campaign Matrix"
    else:
        segment_focus = "N/A"

    # Primary Channel Guard — medium label + governing DNC flag
    _medium_labels = {"EM": "Email", "SMS": "SMS", "OB": "Outbound Dialing", "DM": "Direct Mail"}
    channel_label = _medium_labels.get(request.medium.upper().strip(), request.medium)
    _dnc_col = {"EM": "em_dnc", "SMS": "sms_dnc", "OB": "ob_dnc", "DM": "dm_dnc"}
    dnc_col = _dnc_col.get(request.medium.upper().strip(), "")
    if dnc_col and re.search(rf"\b{dnc_col}\s*=\s*0\b", blob, re.IGNORECASE):
        channel_guard = f"{channel_label} ({dnc_col} = 0)"
    else:
        channel_guard = channel_label

    # Exclusivity Sieve — presence and value of all four DNC flags
    flag_vals: dict[str, str] = {}
    for flag in ("em_dnc", "sms_dnc", "ob_dnc", "dm_dnc"):
        m = re.search(rf"\b{flag}\s*=\s*([01])\b", blob, re.IGNORECASE)
        if m:
            flag_vals[flag] = m.group(1)

    if flag_vals:
        allowed    = [f for f, v in flag_vals.items() if v == "0"]
        suppressed = [f for f, v in flag_vals.items() if v == "1"]
        if allowed and suppressed:
            parts = [f"{f} = 0" for f in allowed] + [f"{f} = 1" for f in suppressed]
            exclusivity_sieve = " & ".join(parts)
        elif allowed:
            exclusivity_sieve = " & ".join(f"{f} = 0" for f in allowed)
        else:
            exclusivity_sieve = "None"
    else:
        exclusivity_sieve = "None"

    # Dynamic Exclusions — behavioral self-join lookback windows and non-DNC exclusion layers
    dynamic_parts: list[str] = []
    m_aal = re.search(
        r"init_activation_date.*?INTERVAL\s+(\d+)\s+MONTH",
        blob,
        re.IGNORECASE | re.DOTALL,
    )
    if m_aal:
        dynamic_parts.append(
            f"Exclude secondary line activations within trailing {m_aal.group(1)} months."
        )
    if request.exclusion_layers:
        for excl in request.exclusion_layers:
            if not excl:
                continue
            lower = excl.lower()
            if "dnc" not in lower and "init_activation_date" not in lower:
                clean = excl.strip()
                # Drop raw SQL filter strings (contain BQ operators) — they are not
                # human-readable labels and bloat the terminal card.
                if re.search(r"\b(AND|OR)\b|=\s*[01'\"]", clean, re.IGNORECASE):
                    continue
                if len(clean) > 100:
                    clean = clean[:97] + "..."
                dynamic_parts.append(clean)
    dynamic_exclusions = ", ".join(dynamic_parts) if dynamic_parts else "None"

    return {
        "portfolio":          f"{request.campaign_code} / {request.campaign_sub_code}",
        "core_product":       core_product,
        "propensity":         propensity,
        "segment_focus":      segment_focus,
        "channel_guard":      channel_guard,
        "exclusivity_sieve":  exclusivity_sieve,
        "dynamic_exclusions": dynamic_exclusions,
    }


def _print_criteria_block(request: "AudienceSizingRequest") -> None:
    """Print the structured Nexus handoff banner before Quant begins SQL generation."""
    f = _extract_criteria_fields(request)
    sep  = "=" * 70
    thin = "-" * 70
    lw   = 23  # label column width
    lines = [
        "",
        sep,
        "[NEXUS AGENT] -> FINAL RECOMMENDED TARGETING CRITERIA FOR QUANT SIZE",
        sep,
        "",
        f"  {'Portfolio / Initiative':<{lw}}: {f['portfolio']}",
        f"  {'Target Core Product':<{lw}}: {f['core_product']}",
        f"  {'Target Propensity':<{lw}}: {f['propensity']}",
        f"  {'Segment Focus':<{lw}}: {f['segment_focus']}",
        f"  {'Primary Channel Guard':<{lw}}: {f['channel_guard']}",
        f"  {'Exclusivity Sieve':<{lw}}: {f['exclusivity_sieve']}",
        f"  {'Dynamic Exclusions':<{lw}}: {f['dynamic_exclusions']}",
        "",
        thin,
        "",
    ]
    print("\n".join(lines))


def _print_terminal_error() -> None:
    print(
        "\n[ERROR]: Unable to locate data points or interpret the business context. "
        "Please reach out to the OCTCO team leads for assistance."
    )
