"""
Vibe OCTO Nexus — Ingestion & Taxonomy Agent

Step 1: On startup, pull 5 campaign briefs from BigQuery (or local glossary
        as fallback), send them to Claude via Fuel iX, and build a strategic
        taxonomy matrix. The matrix is pinned as an ephemeral cached block at
        the top of every subsequent prompt, targeting a ~90% token discount on
        repeated queries.

Step 2: Path 1 — Given a loaded brief dict, resolve it to a validated
        AudienceSizingRequest for Quant.

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

Classify this into exactly one of two workflows using the rules below.
Apply the WORKFLOW_A hard-routing rule first — if it matches, stop and return WORKFLOW_A
without considering WORKFLOW_B.

HARD RULE — WORKFLOW_A: Ad-Hoc Exploratory Request
  If the request is phrased as a metric or probing question — i.e. it opens with or is
  semantically equivalent to "How many...", "Count...", "What is the size of...",
  "Give me a count of...", or any other interrogative asking for a number — classify
  immediately as WORKFLOW_A. Do NOT trigger a campaign playbook lookup for these.
  Examples: "How many customers in AB or BC?",
            "Count postpaid subscribers with SHS eligible",
            "How many Koodo prepaid customers are MTM?",
            "What is the size of the TELUS postpaid base?"

WORKFLOW_B — Structured Campaign Execution Request
  Use ONLY when the consultant explicitly orders campaign setup or execution using
  action verbs such as "Size the...", "Run...", "Execute...", "Set up...", or
  "Pull the playbook for...". The request must name a specific campaign, brief, or
  recognised campaign label.
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
        """Path 1 — resolve a loaded campaign brief to a validated sizing request."""
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

        hint_words = [w for w in campaign_hint.upper().split() if len(w) > 2]
        search_term = f"%{hint_words[0]}%" if hint_words else f"%{campaign_hint.upper()}%"

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from google.cloud import bigquery  # type: ignore

            client = bigquery.Client(project=self._bq_project)
            query_str = f"""
            SELECT DISTINCT
                campaign      AS campaign_name,
                camp_id       AS campaign_code,
                sub_camp_id   AS campaign_sub_code,
                cadence,
                medium,
                campaign_purpose,
                primary_products
            FROM `{self._bq_table}`
            WHERE current_ind  = 1
              AND closed_ind   = 0
              AND UPPER(target_base) <> 'EPP'
              AND UPPER(campaign) LIKE @search_term
            ORDER BY list_pull_date DESC
            LIMIT 1
            """
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("search_term", "STRING", search_term)
                ]
            )
            rows = [dict(r) for r in client.query(query_str, job_config=job_config).result()]
            if rows:
                return rows[0]
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


def _print_terminal_error() -> None:
    print(
        "\n[ERROR]: Unable to locate data points or interpret the business context. "
        "Please reach out to the OCTCO team leads for assistance."
    )
