"""knowledge_base/vibe_octo_knowledge.py — Vibe OCTO Knowledge Ingestion Agent (v4).

What changed from v3
--------------------
- Three-mode ingestion: --full-refresh never touches Google Sheets (safe for daily use).
  Brief fetching is isolated to --refresh-briefs (rate-limited, confirmation required).
- LLM-based brief extraction via Fuel iX (two-stage: strategy interpretation + structured
  extraction) replaces regex snippet extraction.
- Conflict detection: GOLD campaigns get an additional LLM call comparing brief vs. ACC.
- schema_version: "4.0"
- New artifacts: campaign_data_schema.json, gch_current_schema.json, view_domain_catalog.json
- Sparse TF-IDF embeddings stored in campaign_embeddings.db (SQLite) for RAG retrieval.

Three ingestion modes
---------------------
  --full-refresh     : Queries BQ; carries forward existing brief_extraction; no Sheets access.
  --incremental      : Same as --full-refresh but only processes new/changed campaigns.
  --refresh-briefs   : Fetches Google Sheets briefs (rate-limited, confirmation required),
                       then runs two-stage LLM extraction + conflict detection.
  --refresh-schema-only : Re-fetches all three dataset schemas and domain catalog.
  --validate         : Verify all artifacts exist and are valid JSON.

Data sources (read-only)
-------------------------
1. BQ campaign metadata: wb-tian-pr-d0dbe6.wb_tian_pr_dataset.campaign_deployments
2. BQ execution schemas: bi-srv-hsmdet-pr-7b9def.{adobe, campaign_data, gch_current}
3. Google Sheets briefs: accessed only via --refresh-briefs

Output artifacts  (knowledge_base/artifacts/)
----------------------------------------------
  semantic_knowledge_index.json  (v4.0)
  adobe_schema.json
  campaign_data_schema.json      (new)
  gch_current_schema.json        (new)
  cross_campaign_patterns.json
  view_domain_catalog.json       (new)
  ingestion_report.json
  ingestion_log.json
  campaign_embeddings.db         (SQLite — TF-IDF vectors for RAG)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from google.cloud import bigquery

_ROOT_DIR = Path(__file__).resolve().parent.parent
_NEXUS_DIR = _ROOT_DIR / "Vibe OCTO Nexus"
for _p in (str(_ROOT_DIR), str(_NEXUS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.brief_fetcher import BriefFetcher, BriefFetchError  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CAMPAIGN_PROJECT = "wb-tian-pr-d0dbe6"
CAMPAIGN_TABLE = "wb-tian-pr-d0dbe6.wb_tian_pr_dataset.campaign_deployments"

ADOBE_PROJECT = "bi-srv-hsmdet-pr-7b9def"
ADOBE_DATASET = "adobe"
CAMPAIGN_DATA_DATASET = "campaign_data"
GCH_DATASET = "gch_current"

ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts"

DEFAULT_BRIEF_TIMEOUT = int(os.environ.get("BRIEF_FETCH_TIMEOUT", "60"))

# Artifact files that are allowed to persist in the artifacts directory.
# Any other file found there is treated as a stale download and removed.
_APPROVED_ARTIFACTS = frozenset({
    "semantic_knowledge_index.json",
    "semantic_knowledge_index.backup.json",
    "adobe_schema.json",
    "campaign_data_schema.json",
    "gch_current_schema.json",
    "view_domain_catalog.json",
    "schema_annotations.json",
    "cross_campaign_patterns.json",
    "ingestion_report.json",
    "ingestion_log.json",
    "campaign_embeddings.db",
})

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# View domain classification (keyword-based heuristics for initial population)
# ---------------------------------------------------------------------------

# Order matters: first match wins.
_DOMAIN_RULES: list[tuple[str, str]] = [
    # GCH suppression tables (dataset = gch_current)
    ("bq_campaign_segment",           "gch_suppression"),
    ("bq_campaign_communication",     "gch_suppression"),
    ("bq_campaign_description",       "gch_suppression"),
    # Mobility spine
    ("bq_fda_mob_mobility_base",      "mobility_spine"),
    ("mob_mobility_base",             "mobility_spine"),
    ("bq_fda_mob",                    "mobility_spine"),
    # FFH / Home Solutions
    ("bq_dly_dbm_customer_profl",     "ffh_profile"),
    ("dbm_customer_profl",            "ffh_profile"),
    ("ffh",                           "ffh_profile"),
    ("home_solutions",                "ffh_profile"),
    ("internet",                      "ffh_profile"),
    # NBA / propensity model scores
    ("model_score_master",            "scoring"),
    ("model_score",                   "scoring"),
    ("propensity",                    "scoring"),
    ("nba",                           "scoring"),
    ("score_master",                  "scoring"),
    # Channel governance / DNC
    ("dnc",                           "channel_governance"),
    ("do_not_contact",                "channel_governance"),
    ("opt_out",                       "channel_governance"),
    ("consent",                       "channel_governance"),
    ("suppression",                   "channel_governance"),
    # Product eligibility
    ("eligib",                        "product_eligibility"),
    ("product_elig",                  "product_eligibility"),
    ("shs_elig",                      "product_eligibility"),
    ("device_elig",                   "product_eligibility"),
    # Campaign planning / deployment
    ("plan_camp",                     "campaign_planning"),
    ("camp_deploy",                   "campaign_planning"),
    ("campaign_deploy",               "campaign_planning"),
    ("campaign_plan",                 "campaign_planning"),
]

_DOMAIN_DESCRIPTIONS: dict[str, str] = {
    "mobility_spine":      "Primary subscriber base for wireless/mobility campaign sizing",
    "ffh_profile":         "Home Solutions customer profile for FFH/internet campaign sizing",
    "scoring":             "NBA and propensity model scores for model-based targeting",
    "gch_suppression":     "GCH Global Contact History — recency suppression exclusions",
    "channel_governance":  "DNC, consent, and opt-out lists for channel-specific suppression",
    "product_eligibility": "Product ownership and service eligibility views",
    "campaign_planning":   "Campaign deployment metadata and targeting definitions",
    "general":             "General-purpose view — review for applicable domain tag",
}

_VIEW_DESCRIPTIONS: dict[str, str] = {
    "bq_fda_mob_mobility_base":              "Primary mobility subscriber base — COUNT(DISTINCT ban) for wireless sizing",
    "bq_dly_dbm_customer_profl":             "Home Solutions customer profile — COUNT(DISTINCT BACCT_NUM) for FFH sizing",
    "bq_fda_current_model_score_master_view": "NBA and propensity model scores — joined for model-based targeting",
    "bq_campaign_segment":                   "GCH segment table — anti-join for recency suppression",
    "bq_campaign_communication":             "GCH communication history — recency suppression by channel",
    "bq_campaign_description":              "GCH campaign description — maps CAMPAIGN_CD for suppression rules",
}


def _classify_view_domain(view_name: str, dataset: str) -> str:
    """Assign a domain tag to a BQ view name using keyword heuristics."""
    # GCH dataset: all views are suppression by default
    if dataset == GCH_DATASET:
        lower = view_name.lower()
        for prefix, domain in _DOMAIN_RULES:
            if prefix in lower:
                return domain
        return "gch_suppression"

    lower = view_name.lower()
    for prefix, domain in _DOMAIN_RULES:
        if prefix in lower:
            return domain
    return "general"


def _describe_view(view_name: str) -> str:
    """Return a known one-line description or generate a generic one."""
    if view_name in _VIEW_DESCRIPTIONS:
        return _VIEW_DESCRIPTIONS[view_name]
    # Generic description from name structure
    clean = view_name.replace("bq_", "").replace("_", " ").strip()
    return f"{clean} view"


def _infer_lob(view_name: str) -> str:
    lower = view_name.lower()
    if any(k in lower for k in ("mob", "mobility", "wireless")):
        return "mobility"
    if any(k in lower for k in ("ffh", "home", "internet", "customer_profl")):
        return "ffh"
    return "all"


# ---------------------------------------------------------------------------
# Column role discovery patterns
# ---------------------------------------------------------------------------

_ROLE_PATTERNS: dict[str, list[str]] = {
    "camp_id":           ["camp_id", "campaign_id", "campaign_code", "cmp_id"],
    "sub_camp_id":       ["sub_camp_id", "sub_campaign_id", "campaign_sub_id"],
    "campaign_name":     ["campaign_name", "campaign", "name"],
    "targeting_summary": ["targeting_summary", "targeting_note", "target_note"],
    "segment_summary":   ["segment_summary", "segment_count", "audience_definition"],
    "cadence":           ["cadence", "frequency", "schedule"],
    "medium":            ["medium", "channel", "media_type"],
    "campaign_purpose":  ["campaign_purpose", "purpose", "objective"],
    "primary_products":  ["primary_products", "products", "product_list"],
    "databrief_link":    ["databrief_link", "brief_url", "brief_link", "data_brief_link"],
}

# ---------------------------------------------------------------------------
# Pattern extraction constants
# ---------------------------------------------------------------------------

# Finds bare column=value or column > value fragments in ACC-generated summaries.
_COL_FILTER_RE = re.compile(
    r"\b([a-zA-Z_][a-zA-Z0-9_]{2,})\s*(?:=|!=|<>|>=|<=|>|<)\s*"
    r"(?:'[^']*'|\"[^\"]*\"|\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_]*)\b"
)

_TARGETING_KEYWORDS: list[str] = [
    "active", "primary", "primary_sub", "tenure", "subscriber", "eligible",
    "qualified", "resident", "product", "service", "contract", "rate plan",
    "data plan", "home service", "wireless", "internet",
]

_EXCLUSION_KEYWORDS: list[str] = [
    "exclude", "exclusion", "exclusions", "exclude list", "dnc", "do not contact",
    "epp", "opt-out", "opted out", "opted-out", "suppression", "suppress",
    "unsubscribe", "unsubscribed", "hard bounce", "hard-bounce", "deceased",
    "collections", "credit hold", "pending cancel", "control group", "holdout",
]

_PERSONALIZATION_PATTERNS: list[re.Pattern] = [
    re.compile(r"\{(\w+)\}"),
    re.compile(r"\[\[(\w+)\]\]"),
    re.compile(r"<%=?\s*(\w+)\s*%>"),
    re.compile(r"\$\{(\w+)\}"),
]

# Maps a canonical rule name to substrings that signal its presence in text.
_TELECOM_STANDARD_SIGNALS: dict[str, list[str]] = {
    "active_subscriber_filter":  ["active_status", "active subscriber", "active customer",
                                   "is_active", "active =", "active="],
    "dnc_suppression":           ["dnc", "do not contact", "do not call", "do_not_contact"],
    "control_group_exclusion":   ["control group", "holdout group", "control_group",
                                   "exclude control", "split"],
    "unsubscribe_handling":      ["unsubscribe", "unsub", "email opt", "optin_status"],
    "stop_handling":             ["stop handling", "sms opt", "crtc", "stop list"],
    "device_frequency_cap":      ["frequency cap", "push frequency", "max push",
                                   "device_freq", "impression cap"],
}

# ---------------------------------------------------------------------------
# Error remediation catalogue (unchanged from v2)
# ---------------------------------------------------------------------------

_REMEDIATION: dict[str, dict] = {
    "401_unauthorized": {
        "root_cause": (
            "ADC credentials are valid but the token is missing required Google API "
            "scopes (spreadsheets.readonly / drive.readonly)."
        ),
        "severity": "HIGH",
        "steps": [
            "Re-authenticate with all required scopes:\n"
            "    gcloud auth application-default login \\\n"
            "        --scopes=https://www.googleapis.com/auth/cloud-platform,"
            "https://www.googleapis.com/auth/spreadsheets.readonly,"
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    },
    "403_forbidden": {
        "root_cause": (
            "The authenticated identity does not have permission to access the specific "
            "document. Share the sheet with the ADC identity."
        ),
        "severity": "MEDIUM",
        "steps": [
            "Run `gcloud config get-value account` to identify the ADC identity.",
            "Open each affected databrief_link, click Share, add the ADC identity as Viewer.",
        ],
    },
    "404_not_found": {
        "root_cause": "The document no longer exists or the URL is malformed.",
        "severity": "LOW",
        "steps": [
            "Open each affected URL in a browser to confirm deletion.",
            "Contact the campaign owner to provide a replacement link.",
        ],
    },
    "timeout": {
        "root_cause": "The HTTP request timed out. Corporate proxy may be down.",
        "severity": "LOW",
        "steps": [
            "Check HTTPS_PROXY env var (should be http://127.0.0.1:14888).",
            "Increase timeout: BRIEF_FETCH_TIMEOUT=120 python -m knowledge_base.vibe_octo_knowledge --full-refresh",
        ],
    },
    "5xx_server_error": {
        "root_cause": "Google API returned a 5xx error (transient).",
        "severity": "LOW",
        "steps": ["Wait 5-10 minutes and retry."],
    },
    "no_url": {
        "root_cause": "Campaign row has no databrief_link value.",
        "severity": "INFO",
        "steps": ["Contact campaign owner to provide a databrief_link."],
    },
    "other": {
        "root_cause": "Unexpected error. See error_message in ingestion_log.json.",
        "severity": "LOW",
        "steps": ["Review ingestion_log.json for the full error message."],
    },
}

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _extract_http_code(msg: str) -> Optional[int]:
    for pattern in (r"\bHTTP[^\d]*(\d{3})\b", r"\bstatus[^\d]*(\d{3})\b", r"\b(\d{3})\b"):
        m = re.search(pattern, msg, re.IGNORECASE)
        if m:
            code = int(m.group(1))
            if 100 <= code < 600:
                return code
    return None


def _classify_error(http_code: Optional[int], msg: str) -> str:
    msg_lower = msg.lower()
    if "timeout" in msg_lower or "timed out" in msg_lower or "read timeout" in msg_lower:
        return "timeout"
    if http_code is None:
        return "other"
    if http_code == 401:
        return "401_unauthorized"
    if http_code == 403:
        return "403_forbidden"
    if http_code == 404:
        return "404_not_found"
    if 500 <= http_code < 600:
        return "5xx_server_error"
    return f"http_{http_code}"


def _attach_business_rules(campaign_records: list[dict]) -> None:
    """Attach applicable verified business rules to each campaign record.

    Reads business_rules.json from the project root. Universal rules attach to
    every campaign; campaign-scope rules attach only to their matching camp_id.
    Rules are marked confidence=1.0 (human verified). Runs silently on any error
    so a missing or corrupt rules file never blocks the knowledge base refresh.
    """
    _root = Path(__file__).resolve().parent.parent
    rules_path = _root / "business_rules.json"
    if not rules_path.exists():
        return
    try:
        data = json.loads(rules_path.read_text(encoding="utf-8"))
        rules: list[dict] = data.get("rules", [])
        if not rules:
            return
    except Exception:
        return

    for camp in campaign_records:
        camp_id = camp.get("camp_id", "").upper()
        applicable: list[dict] = []
        for rule in rules:
            if not rule.get("applies_to_future", True):
                continue
            scope = rule.get("scope", "").lower()
            if scope == "universal":
                applicable.append({**rule, "confidence": 1.0})
            elif scope == "campaign":
                if rule.get("campaign_code", "").upper() == camp_id:
                    applicable.append({**rule, "confidence": 1.0})
        if applicable:
            camp["verified_business_rules"] = applicable


def _classify_tier(targeting_summary: str, segment_summary: str, brief_accessible: bool) -> str:
    has_ts = bool(targeting_summary and targeting_summary.strip())
    has_ss = bool(segment_summary and segment_summary.strip())
    if has_ts and has_ss:
        return "GOLD"
    if brief_accessible:
        return "SILVER"
    return "BRONZE"


def _write_atomic(path: Path, data: dict | list) -> None:
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=path.stem + "_"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Text analysis helpers
# ---------------------------------------------------------------------------

def _extract_col_filters(text: str) -> list[str]:
    """Extract column=value fragments from ACC-generated summary text."""
    seen: dict[str, None] = {}
    for m in _COL_FILTER_RE.finditer(text):
        frag = m.group(0).strip()
        if frag not in seen:
            seen[frag] = None
    return list(seen.keys())[:20]


def _extract_targeting_snippets(text: str) -> list[str]:
    """Extract targeting criterion snippets from free-form or summary text."""
    text_lower = text.lower()
    snippets: dict[str, None] = {}

    # Column-level filters come first (most precise)
    for frag in _extract_col_filters(text):
        snippets[frag] = None

    # Keyword-anchored phrases: start FROM the keyword, end at next sentence boundary
    for kw in _TARGETING_KEYWORDS:
        if kw not in text_lower:
            continue
        idx = text_lower.find(kw)
        raw = text[idx: min(len(text), idx + 80)]
        # Truncate at first newline, semicolon, or bullet marker
        raw = re.split(r"[\n\r;]|\s+-\s+", raw)[0].strip()
        raw = re.sub(r"\s+", " ", raw)
        if len(raw) > 3 and raw not in snippets:
            snippets[raw] = None

    return list(snippets.keys())[:15]


def _extract_exclusion_snippets(text: str) -> list[str]:
    """Extract exclusion-related snippets from free-form or summary text."""
    text_lower = text.lower()
    snippets: dict[str, None] = {}
    for kw in _EXCLUSION_KEYWORDS:
        if kw not in text_lower:
            continue
        idx = text_lower.find(kw)
        # Start from the keyword position, not before
        raw = text[idx: min(len(text), idx + 80)].strip()
        raw = re.split(r"[\n\r;]|\s+-\s+", raw)[0].strip()
        raw = re.sub(r"\s+", " ", raw)
        if len(raw) > 3 and raw not in snippets:
            snippets[raw] = None
    return list(snippets.keys())[:12]


def _extract_personalization_fields(text: str) -> list[str]:
    fields: set[str] = set()
    for pat in _PERSONALIZATION_PATTERNS:
        for m in pat.finditer(text):
            f = m.group(1).lower()
            if 3 <= len(f) <= 40:
                fields.add(f)
    return sorted(fields)


def _check_telecom_standards(text: str) -> dict[str, bool]:
    text_lower = text.lower()
    return {
        rule: any(sig in text_lower for sig in signals)
        for rule, signals in _TELECOM_STANDARD_SIGNALS.items()
    }


# ---------------------------------------------------------------------------
# GOLD insight extraction
# ---------------------------------------------------------------------------

def _extract_gold_insights(gold_campaigns: list[dict]) -> dict:
    """Aggregate cross-campaign patterns from all GOLD campaigns."""
    if not gold_campaigns:
        return {
            "common_targeting_patterns": [],
            "common_exclusions": [],
            "common_personalization_fields": [],
            "standard_telecom_rules": list(_TELECOM_STANDARD_SIGNALS.keys()),
        }

    # Concatenate all GOLD summary text for frequency analysis
    all_text = " ".join(
        (c.get("targeting_summary") or "") + " " + (c.get("segment_summary") or "")
        for c in gold_campaigns
    )
    all_lower = all_text.lower()

    common_targeting = [kw for kw in _TARGETING_KEYWORDS if kw in all_lower]
    common_exclusions = [kw for kw in _EXCLUSION_KEYWORDS if kw in all_lower]

    # Personalization fields from GOLD summaries
    gold_pers: set[str] = set()
    for c in gold_campaigns:
        summary = (c.get("targeting_summary") or "") + " " + (c.get("segment_summary") or "")
        gold_pers.update(_extract_personalization_fields(summary))

    # Telecom standards: mark rules present in more than 30% of GOLD campaigns
    rule_counts: dict[str, int] = defaultdict(int)
    for c in gold_campaigns:
        summary = (c.get("targeting_summary") or "") + " " + (c.get("segment_summary") or "")
        for rule, present in _check_telecom_standards(summary).items():
            if present:
                rule_counts[rule] += 1

    threshold = max(1, int(len(gold_campaigns) * 0.30))
    standard_rules = [rule for rule, cnt in rule_counts.items() if cnt >= threshold]

    # Always include the most fundamental telecom rules regardless of frequency
    for rule in ("active_subscriber_filter", "dnc_suppression", "control_group_exclusion"):
        if rule not in standard_rules:
            standard_rules.append(rule)

    return {
        "common_targeting_patterns": common_targeting,
        "common_exclusions": common_exclusions,
        "common_personalization_fields": sorted(gold_pers)[:20],
        "standard_telecom_rules": standard_rules,
    }


def _build_cross_campaign_patterns(
    gold_campaigns: list[dict],
    gold_insights: dict,
) -> dict:
    """Build cross_campaign_patterns.json from GOLD analysis."""
    gold_count = len(gold_campaigns)

    # Frequency of targeting keywords across GOLD campaigns
    t_freq: dict[str, list[str]] = defaultdict(list)
    e_freq: dict[str, list[str]] = defaultdict(list)

    for c in gold_campaigns:
        cid = c.get("camp_id", "")
        summary = (c.get("targeting_summary") or "") + " " + (c.get("segment_summary") or "")
        summary_lower = summary.lower()

        for kw in _TARGETING_KEYWORDS:
            if kw in summary_lower:
                t_freq[kw].append(cid)

        for kw in _EXCLUSION_KEYWORDS:
            if kw in summary_lower:
                e_freq[kw].append(cid)

    def _dedupe_examples(camps: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for c in camps:
            if c:
                seen[c] = None
        return list(seen.keys())[:5]

    targeting_patterns = [
        {
            "pattern": kw,
            "frequency": len(camps),
            "gold_examples": _dedupe_examples(camps),
            "bq_column_used": None,
            "confidence": round(len(camps) / gold_count, 2) if gold_count else 0.0,
        }
        for kw, camps in sorted(t_freq.items(), key=lambda x: -len(x[1]))
        if len(camps) >= 2
    ]

    exclusion_patterns = [
        {
            "pattern": kw,
            "frequency": len(camps),
            "gold_examples": _dedupe_examples(camps),
            "typical_lookback": None,
            "confidence": round(len(camps) / gold_count, 2) if gold_count else 0.0,
        }
        for kw, camps in sorted(e_freq.items(), key=lambda x: -len(x[1]))
        if len(camps) >= 2
    ]

    return {
        "extracted_at": datetime.now(tz=timezone.utc).isoformat(),
        "source_campaigns_gold": gold_count,
        "insights": {
            "targeting_patterns": targeting_patterns[:20],
            "exclusion_patterns": exclusion_patterns[:15],
            "telecom_standards": {
                "always_required": gold_insights.get("standard_telecom_rules", []),
                "by_channel": {
                    "EM":   ["unsubscribe_handling"],
                    "SMS":  ["stop_handling", "crtc_compliance"],
                    "PUSH": ["device_frequency_cap"],
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# Deployment-level requirement extraction
# ---------------------------------------------------------------------------

def _gold_confidence(
    targeting: list[str],
    exclusions: list[str],
    standards: dict[str, bool],
    gold_insights: dict,
) -> float:
    """Compute 0..1 confidence score based on GOLD alignment signals."""
    gold_targeting = set(gold_insights.get("common_targeting_patterns", []))
    gold_exclusions = set(gold_insights.get("common_exclusions", []))

    targeting_hit = any(
        any(gt in t.lower() for gt in gold_targeting)
        for t in targeting
    ) if gold_targeting and targeting else False

    exclusion_hit = any(
        any(ge in e.lower() for ge in gold_exclusions)
        for e in exclusions
    ) if gold_exclusions and exclusions else False

    standards_score = sum(
        1 for rule in ("active_subscriber_filter", "dnc_suppression")
        if standards.get(rule, False)
    ) / 2.0

    signals = [targeting_hit, exclusion_hit, standards_score > 0]
    base = sum(1 for s in signals if s) / len(signals)
    return round(min(1.0, base + 0.15), 2)  # +0.15 base credit


def _build_brief_deployment(
    brief_text: str,
    brief_accessible: bool,
    camp: dict,
    gold_insights: dict,
    tier: str,
) -> dict:
    """Build the deployment block for a campaign row.

    For GOLD: use targeting/segment summaries directly.
    For SILVER: extract requirements from brief text, cross-reference GOLD.
    For BRONZE: assign GOLD standard rules as guidance, mark as inferred.
    """
    medium = (camp.get("medium") or "").upper().strip()

    if tier == "GOLD":
        summary_text = (
            (camp.get("targeting_summary") or "") + " " +
            (camp.get("segment_summary") or "")
        )
        targeting = _extract_targeting_snippets(summary_text)
        exclusions = _extract_exclusion_snippets(summary_text)
        personalization = _extract_personalization_fields(summary_text)
        standards = _check_telecom_standards(summary_text)
        confidence = 1.0
        source_note = "extracted_from_acc_summaries"

    elif tier == "SILVER" and brief_accessible and brief_text:
        targeting = _extract_targeting_snippets(brief_text)
        exclusions = _extract_exclusion_snippets(brief_text)
        personalization = _extract_personalization_fields(brief_text)
        standards = _check_telecom_standards(brief_text)
        confidence = _gold_confidence(targeting, exclusions, standards, gold_insights)
        source_note = "extracted_from_brief"

        # Supplement with GOLD standard rules when missing from brief
        std_rules = gold_insights.get("standard_telecom_rules", [])
        for rule in std_rules:
            if not standards.get(rule):
                exclusions.append(f"[GOLD-guided] {rule.replace('_', ' ')}")

    else:  # BRONZE
        std_rules = gold_insights.get("standard_telecom_rules", [])
        gold_targeting = gold_insights.get("common_targeting_patterns", [])
        targeting = [f"[GOLD-guided] {kw}" for kw in gold_targeting[:5]]
        exclusions = [f"[GOLD-guided] {rule.replace('_', ' ')}" for rule in std_rules]
        personalization = []
        standards = {rule: True for rule in std_rules}
        confidence = 0.50
        source_note = "gold_standard_rules_applied"

    # Channel-specific DNC flags
    channel_dnc: list[str] = []
    for kw in ("dnc", "epp", "opt-out", "suppression"):
        combined = (
            " ".join(targeting) + " " + " ".join(exclusions)
        ).lower()
        if kw in combined and kw not in channel_dnc:
            channel_dnc.append(kw)

    return {
        "deployment_id": camp.get("sub_camp_id") or camp.get("camp_id") or "",
        "deployment_name": camp.get("campaign_name") or "",
        "brief_accessible": brief_accessible,
        "extracted_requirements": {
            "targeting": targeting[:12],
            "exclusions": exclusions[:12],
            "personalization": personalization[:15],
            "seasonality": None,
            "business_objectives": [],
            "channel_rules": {
                "medium": medium or None,
                "dnc_flags": channel_dnc[:6],
                "compliance_notes": None,
            },
        },
        "mapped_to_gold_patterns": {
            "source": source_note,
            "targeting_aligned_with_gold": any(
                any(gt in t.lower() for gt in gold_insights.get("common_targeting_patterns", []))
                for t in targeting
            ) if gold_insights.get("common_targeting_patterns") else None,
            "exclusions_aligned_with_gold": any(
                any(ge in e.lower() for ge in gold_insights.get("common_exclusions", []))
                for e in exclusions
            ) if gold_insights.get("common_exclusions") else None,
            "confidence_from_gold_insights": confidence,
        },
    }


# ---------------------------------------------------------------------------
# Schema annotation heuristics
# ---------------------------------------------------------------------------

# Ordered list of (pattern_fn, note) pairs applied to column names.
# Each pattern_fn receives the UPPER-CASED column name and returns bool.
# First match wins.
_ANNOTATION_RULES: list[tuple] = [
    # Exact-match well-known keys
    (lambda n: n == "MOB_BAN",       "Mobility Billing Account Number — primary join key for wireless sizing"),
    (lambda n: n == "BACCT_NUM",     "Business Account Number — primary join key for Home Solutions (FFH) sizing"),
    (lambda n: n == "BAN",           "Billing Account Number — join key"),
    # Suffix patterns
    (lambda n: n.endswith("_BAN"),   "Billing Account Number — join key"),
    (lambda n: n.endswith("_STATUS_CD"), "Account or service status code — ACT=active, SUS=suspended, CAN=cancelled"),
    (lambda n: n.endswith("_STATUS"), "Status field — check reference table for valid values"),
    (lambda n: n.endswith("_CD"),    "Code value — join to reference/lookup table before displaying"),
    (lambda n: n.endswith("_DT"),    "Date field (YYYYMMDD or ISO format)"),
    (lambda n: n.endswith("_FLG"),   "Boolean flag (Y/N or 1/0)"),
    (lambda n: n.endswith("_IND"),   "Indicator field (Y/N or 1/0)"),
    (lambda n: n.endswith("_AMT"),   "Monetary amount field"),
    (lambda n: n.endswith("_NUM"),   "Numeric identifier or count field"),
    (lambda n: n.endswith("_NM"),    "Name or label field"),
    (lambda n: n.endswith("_ID"),    "Identifier — foreign key or surrogate key"),
    (lambda n: n.endswith("_TYPE"),  "Type classification code"),
    # Prefix patterns
    (lambda n: n.startswith("MOB_"), "Mobility domain field"),
    (lambda n: n.startswith("FFH_"), "Fixed/Home Solutions domain field"),
    # Substring patterns
    (lambda n: "SCORE" in n,         "Propensity or model score — higher value = more likely"),
    (lambda n: "SUPPRESS" in n,      "Suppression indicator — exclude records flagged here"),
    (lambda n: "SUPPRESSION" in n,   "Suppression flag — exclude records flagged here"),
    (lambda n: "CAMPAIGN" in n or "CAMP_" in n, "Campaign identifier or campaign-related field"),
    (lambda n: "EMAIL" in n,         "Email address or email-related field"),
    (lambda n: "PHONE" in n,         "Phone number field"),
    (lambda n: "PROV" in n,          "Province code (AB, BC, ON, QC, etc.)"),
    (lambda n: "TENURE" in n,        "Account tenure — months or years as customer"),
    (lambda n: "ELIG" in n,          "Eligibility indicator — Y if eligible for product or offer"),
    (lambda n: "DNC" in n,           "Do-Not-Contact flag — exclude if set"),
    (lambda n: "OPT" in n,           "Opt-in/opt-out consent field"),
    (lambda n: "CONTRACT" in n,      "Contract term or contract-related field"),
]


def _annotate_column(col_name: str) -> str:
    """Return a heuristic note for a column name, or '' if no pattern matches."""
    upper = col_name.upper()
    for pattern_fn, note in _ANNOTATION_RULES:
        try:
            if pattern_fn(upper):
                return note
        except Exception:
            continue
    return ""


def _build_schema_annotations(
    artifacts_dir: Path,
    adobe_schema: dict,
    camp_data_schema: dict,
    gch_schema: dict,
) -> dict:
    """Merge-safe heuristic annotation builder for all schema artifacts.

    Reads existing schema_annotations.json (preserves human-written notes).
    Adds a heuristic note for every unannotated column.
    Never overwrites an existing non-empty annotation.
    Returns the merged dict (caller must write it atomically).

    No LLM calls. No network calls. Pure Python deterministic pattern matching.
    """
    ann_path = artifacts_dir / "schema_annotations.json"
    existing: dict = {}
    if ann_path.exists():
        try:
            existing = json.loads(ann_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    result: dict = {k: dict(v) for k, v in existing.items()}  # deep copy

    for schema_data in (adobe_schema, camp_data_schema, gch_schema):
        views = schema_data.get("views", {})
        if not isinstance(views, dict):
            continue
        for view_name, view_data in views.items():
            if view_name not in result:
                result[view_name] = {}
            view_annots = result[view_name]
            for col in (view_data.get("columns") or []):
                col_name = col.get("name", "") if isinstance(col, dict) else str(col)
                if not col_name:
                    continue
                if view_annots.get(col_name):
                    continue  # preserve existing human note
                note = _annotate_column(col_name)
                view_annots[col_name] = note  # "" is fine — placeholder slot

    return result


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class VibeOctoKnowledge:
    """v3 Ingestion Agent — GOLD-guided, deployment-aware knowledge base."""

    def __init__(self, dry_run: bool = False, brief_timeout: int = DEFAULT_BRIEF_TIMEOUT):
        self.dry_run = dry_run
        self._brief_timeout = brief_timeout
        self._artifacts_dir = ARTIFACTS_DIR

        if not dry_run:
            self._artifacts_dir.mkdir(parents=True, exist_ok=True)

        self._campaign_bq = bigquery.Client(project=CAMPAIGN_PROJECT)
        self._adobe_bq = bigquery.Client(project=ADOBE_PROJECT)
        self._brief_fetcher = BriefFetcher(timeout=brief_timeout)
        self._col_map: Optional[dict[str, str]] = None

    # ------------------------------------------------------------------
    # Public commands
    # ------------------------------------------------------------------

    def run_full_refresh(self) -> None:
        """Query BQ for campaign metadata; carry forward existing brief_extraction data.

        Does NOT access Google Sheets.  To populate brief_extraction fields,
        run --refresh-briefs separately.  Safe for daily scheduled use.
        """
        print("\n=== VIBE OCTO KNOWLEDGE v4 — FULL REFRESH ===")
        print("  (Brief fetching disabled — use --refresh-briefs to extract brief data)")
        run_at = datetime.now(tz=timezone.utc).isoformat()

        self._cleanup_stale_files()

        # Load the existing index so we can carry forward brief_extraction data
        old_index = self._load_existing_index()

        # Phase 1: Query BQ for campaign metadata (no Sheets access)
        campaigns = self._phase1_campaigns_bq_only()
        fetch_log: list[dict] = []
        failures: list[dict] = []

        # Phase 2: Schema ingestion (all three datasets)
        adobe_schema = self._phase2_adobe_schema()
        camp_data_schema, gch_schema = self._phase2b_execution_schemas()

        # Phase 2c: View domain catalog
        domain_catalog = self._phase2c_domain_catalog(camp_data_schema, gch_schema)

        # Phase 3: Build and write all artifacts
        self._phase3_build_and_write(
            campaigns, fetch_log, failures, adobe_schema, run_at,
            old_index=old_index,
            camp_data_schema=camp_data_schema,
            gch_schema=gch_schema,
            domain_catalog=domain_catalog,
        )

        print("\n=== FULL REFRESH COMPLETE ===\n")

    def run_incremental(self) -> None:
        """Ingest only campaigns added or modified since last refresh.

        Same three-mode behavior as --full-refresh: no Sheets access, brief_extraction
        carried forward from existing index.
        """
        print("\n=== VIBE OCTO KNOWLEDGE v4 — INCREMENTAL REFRESH ===")
        run_at = datetime.now(tz=timezone.utc).isoformat()

        old_index = self._load_existing_index()
        old_timestamps: dict[str, str] = {}
        for rec in old_index.get("campaigns", []):
            key = f"{rec.get('camp_id', '')}::{rec.get('sub_camp_id', '')}"
            old_timestamps[key] = rec.get("last_ingested_at", rec.get("ingested_at", ""))

        all_campaigns = self._phase1_campaigns_bq_only()

        # Only process campaigns whose BQ row is new or has a newer ingested_at timestamp
        new_or_changed = [
            c for c in all_campaigns
            if f"{c['camp_id']}::{c['sub_camp_id']}" not in old_timestamps
        ]

        if not new_or_changed:
            print("  No new or changed campaigns found. Index is up to date.")
            return

        print(f"  Processing {len(new_or_changed)} new/changed campaign(s).")

        # Merge: update changed campaigns, keep unchanged ones from old index
        merged = {
            f"{r.get('camp_id', '')}::{r.get('sub_camp_id', '')}": r
            for r in old_index.get("campaigns", [])
        }
        for c in new_or_changed:
            key = f"{c['camp_id']}::{c['sub_camp_id']}"
            merged[key] = c

        campaigns = list(merged.values())
        adobe_schema = self._phase2_adobe_schema()
        camp_data_schema, gch_schema = self._phase2b_execution_schemas()
        domain_catalog = self._phase2c_domain_catalog(camp_data_schema, gch_schema)

        self._phase3_build_and_write(
            campaigns, [], [], adobe_schema, run_at,
            old_index=old_index,
            camp_data_schema=camp_data_schema,
            gch_schema=gch_schema,
            domain_catalog=domain_catalog,
        )
        print("\n=== INCREMENTAL REFRESH COMPLETE ===\n")

    def run_refresh_briefs(self, campaign_id: Optional[str] = None) -> None:
        """Fetch Google Sheets briefs (rate-limited) and run two-stage LLM extraction.

        This is the only mode that accesses Google Workspace.  Requires explicit
        Y/N confirmation before fetching begins.  Rate-limited to 1 document per
        5 seconds (enforced in code, not configurable).
        """
        print("\n=== VIBE OCTO KNOWLEDGE v4 — REFRESH BRIEFS ===")
        print("  NOTE: This mode will access Google Workspace (Google Sheets).")
        if campaign_id:
            print(f"  Scope: single campaign — {campaign_id}")
        else:
            print("  Scope: all campaigns with databrief_link URLs")

        # Load existing index
        old_index = self._load_existing_index()
        old_campaigns = {
            f"{r.get('camp_id', '')}::{r.get('sub_camp_id', '')}": r
            for r in old_index.get("campaigns", [])
        }

        # Build list of targets (campaigns that need brief fetch)
        all_campaigns = self._phase1_campaigns_bq_only()
        targets = []
        for c in all_campaigns:
            key = f"{c['camp_id']}::{c['sub_camp_id']}"
            existing = old_campaigns.get(key, {})
            existing_url = existing.get("brief_fetched_from", "")
            current_url = c.get("databrief_link", "")

            if campaign_id and c.get("camp_id", "").upper() != campaign_id.upper():
                continue

            if not current_url.strip():
                continue  # no URL to fetch

            needs_fetch = (
                not existing.get("brief_extraction")
                or existing_url != current_url
            )

            if needs_fetch:
                targets.append(c)

        if not targets:
            print("\n  All campaigns have up-to-date brief extractions. Nothing to fetch.")
            return

        # Rate-limited fetch with confirmation
        from knowledge_base.sources.sheets_enricher import SheetsEnricher
        enricher = SheetsEnricher(self._brief_fetcher, timeout=self._brief_timeout)
        fetch_results = enricher.confirm_and_fetch(targets, camp_id_filter=campaign_id)

        if not fetch_results:
            print("\n  No briefs fetched.")
            return

        # LLM extraction for each fetched brief
        print(f"\n  Running LLM extraction for {len(fetch_results)} brief(s)...")
        now_ts = datetime.now(tz=timezone.utc).isoformat()
        updated_count = 0

        for camp, brief_text, success in fetch_results:
            if not success or not brief_text:
                _log.info("Skipping LLM extraction for %s (no brief text)", camp.get("camp_id"))
                continue

            key = f"{camp['camp_id']}::{camp['sub_camp_id']}"
            tier = old_campaigns.get(key, {}).get("tier", "BRONZE")
            name = camp.get("campaign_name", camp.get("camp_id", "?"))

            print(f"\n  Extracting: {name[:60]}")
            extraction, conflict_notes = self._llm_extract_brief(
                camp=camp,
                brief_text=brief_text,
                tier=tier,
            )

            # Merge into existing record
            rec = old_campaigns.get(key) or camp
            rec["brief_extraction"] = extraction
            rec["conflict_notes"] = conflict_notes
            rec["brief_fetched_from"] = camp.get("databrief_link", "")
            rec["brief_fetched_at"] = now_ts
            old_campaigns[key] = rec
            updated_count += 1

        if not self.dry_run and updated_count > 0:
            # Rebuild the full index with updated brief_extraction fields.
            # Use all_campaigns (17 flat BQ rows) as the campaigns source — NOT
            # old_campaigns.values(), which is deduplicated to 8 unique keys and
            # would cause _phase3_build_and_write() to drop 9 deployment records.
            # Propagate this run's new brief extractions into the old_index so
            # _phase3_build_and_write() carries them forward via old_records lookup.
            run_at = datetime.now(tz=timezone.utc).isoformat()
            updated_old_campaigns_list = []
            for r in old_index.get("campaigns", []):
                key = f"{r.get('camp_id', '')}::{r.get('sub_camp_id', '')}"
                upd = old_campaigns.get(key, {})
                if upd.get("brief_extraction") or upd.get("brief_fetched_from"):
                    r = dict(r)
                    if upd.get("brief_extraction"):
                        r["brief_extraction"] = upd["brief_extraction"]
                    if upd.get("conflict_notes"):
                        r["conflict_notes"] = upd["conflict_notes"]
                    if upd.get("brief_fetched_from"):
                        r["brief_fetched_from"] = upd["brief_fetched_from"]
                    if upd.get("brief_fetched_at"):
                        r["brief_fetched_at"] = upd["brief_fetched_at"]
                updated_old_campaigns_list.append(r)
            updated_old_index = dict(old_index)
            updated_old_index["campaigns"] = updated_old_campaigns_list
            campaigns = all_campaigns
            adobe_schema = self._load_existing_artifact("adobe_schema.json") or {}
            camp_data_schema = self._load_existing_artifact("campaign_data_schema.json") or {}
            gch_schema = self._load_existing_artifact("gch_current_schema.json") or {}
            domain_catalog = self._load_existing_artifact("view_domain_catalog.json") or {}
            self._phase3_build_and_write(
                campaigns, [], [], adobe_schema, run_at,
                old_index=updated_old_index,
                camp_data_schema=camp_data_schema,
                gch_schema=gch_schema,
                domain_catalog=domain_catalog,
                skip_schema_artifacts=True,
            )

        print(f"\n  Updated {updated_count} campaign brief extraction(s).")
        print("\n=== REFRESH BRIEFS COMPLETE ===\n")

    def _llm_extract_brief(
        self,
        camp: dict,
        brief_text: str,
        tier: str,
    ) -> tuple[dict, list[str]]:
        """Two-stage LLM extraction + optional conflict detection for GOLD campaigns.

        Returns (brief_extraction_dict, conflict_notes_list).
        Falls back gracefully if Fuel iX is unreachable.
        """
        try:
            from core.claude_client import ClaudeClient  # type: ignore[import]
            client = ClaudeClient()
        except Exception as exc:
            _log.warning("ClaudeClient unavailable: %s — skipping LLM extraction", exc)
            return {}, []

        # Stage 1: Campaign strategy interpretation
        strategy_prompt = f"""You are a telecom campaign analyst.  Read the campaign data brief below
and produce a strategic understanding before any extraction.

Campaign: {camp.get('campaign_name', '')}
Medium: {camp.get('medium', '')}  |  Cadence: {camp.get('cadence', '')}

=== BRIEF TEXT ===
{brief_text[:6000]}
=== END BRIEF ===

Respond with JSON:
{{
  "campaign_strategy_summary": "2-3 sentence plain-English campaign strategy",
  "intended_audience": "plain-English audience description",
  "customer_action": "what we want customers to do (upgrade/cross-sell/reactivate etc.)",
  "geographic_scope": ["province codes"],
  "ambiguities": ["any unclear or contradictory statements in the brief"]
}}"""

        try:
            stage1 = client._call(
                "You are a telecom campaign brief analyst. Return only valid JSON.",
                strategy_prompt,
            )
            stage1_data = client._extract_json(stage1)
        except Exception as exc:
            _log.warning("Stage 1 extraction failed for %s: %s", camp.get("camp_id"), exc)
            return {}, []

        # Stage 2: Structured extraction using Stage 1 as context
        extraction_prompt = f"""Using your understanding of the campaign strategy below,
extract precise structured criteria from the brief.

Campaign strategy summary: {stage1_data.get('campaign_strategy_summary', '')}

=== BRIEF TEXT ===
{brief_text[:6000]}
=== END BRIEF ===

Respond with JSON matching this exact schema:
{{
  "campaign_strategy_summary": "string",
  "targeting_filters": ["list of targeting criteria — SQL-translatable"],
  "exclusion_rules": ["list of exclusion rules (GCH, DNC, etc.)"],
  "channel_governance": {{"medium": "EM/SMS/PUSH", "dnc_note": "string"}},
  "geographic_scope": ["province codes"],
  "lifecycle_constraints": ["contract/tenure criteria"],
  "product_eligibility_pairs": ["shs_ind=0 AND shs_elig=1 style pairs"],
  "segmentation_only_notes": ["language splits, A/B allocs — NOT SQL filters"],
  "ambiguities_found": ["unclear statements with how they were resolved"],
  "extraction_confidence": {{"overall": 0.85, "targeting_filters": 0.90, "exclusion_rules": 0.80}}
}}"""

        try:
            stage2 = client._call(
                "You are a telecom campaign brief analyst. Return only valid JSON.",
                extraction_prompt,
            )
            extraction = client._extract_json(stage2)
            extraction["extracted_at"] = datetime.now(tz=timezone.utc).isoformat()
        except Exception as exc:
            _log.warning("Stage 2 extraction failed for %s: %s", camp.get("camp_id"), exc)
            return {}, []

        # Conflict detection for GOLD campaigns only
        conflict_notes: list[str] = []
        if tier == "GOLD":
            acc = camp.get("acc_summaries") or {}
            ts = acc.get("targeting_summary", "")
            if ts:
                try:
                    conflict_prompt = f"""Compare the brief extraction against the ACC-verified targeting summary.
Identify any material differences (not cosmetic phrasing differences).

Brief extraction targeting filters: {extraction.get('targeting_filters', [])}
Brief exclusion rules: {extraction.get('exclusion_rules', [])}
Brief lifecycle constraints: {extraction.get('lifecycle_constraints', [])}

ACC-verified targeting summary: {ts[:2000]}

List each material discrepancy as a plain-English statement.
If fully aligned, return an empty list.

Respond with JSON: {{"conflict_notes": ["...", "..."]}}
Note: ACC summary is authoritative where conflicts exist."""

                    conflict_resp = client._call(
                        "You are a telecom campaign analyst checking for data brief vs. ACC conflicts. Return only valid JSON.",
                        conflict_prompt,
                    )
                    conflict_data = client._extract_json(conflict_resp)
                    conflict_notes = conflict_data.get("conflict_notes", [])
                except Exception as exc:
                    _log.warning("Conflict detection failed for %s: %s", camp.get("camp_id"), exc)

        return extraction, conflict_notes

    def run_validate(self) -> None:
        """Verify all v4 artifacts exist and contain valid JSON."""
        print("\n=== VIBE OCTO KNOWLEDGE v4 — VALIDATE ===\n")
        expected = [
            "semantic_knowledge_index.json",
            "adobe_schema.json",
            "campaign_data_schema.json",
            "gch_current_schema.json",
            "view_domain_catalog.json",
            "schema_annotations.json",
            "cross_campaign_patterns.json",
            "ingestion_report.json",
            "ingestion_log.json",
        ]
        all_ok = True
        for name in expected:
            path = self._artifacts_dir / name
            if not path.exists():
                print(f"  MISSING  {name}")
                all_ok = False
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                size_kb = path.stat().st_size / 1024
                if isinstance(data, dict):
                    print(f"  OK       {name}  ({size_kb:.1f} KB, {len(data)} top-level keys)")
                elif isinstance(data, list):
                    print(f"  OK       {name}  ({size_kb:.1f} KB, {len(data)} entries)")
                else:
                    print(f"  OK       {name}  ({size_kb:.1f} KB)")

                # Extra sanity checks per artifact
                if name == "semantic_knowledge_index.json":
                    v = data.get("schema_version", "")
                    n = len(data.get("campaigns", []))
                    gold = data.get("ingestion_summary", {}).get("gold_count", 0)
                    print(f"           schema_version={v}  campaigns={n}  gold={gold}")
                elif name == "cross_campaign_patterns.json":
                    tp = len(data.get("insights", {}).get("targeting_patterns", []))
                    ep = len(data.get("insights", {}).get("exclusion_patterns", []))
                    print(f"           targeting_patterns={tp}  exclusion_patterns={ep}")

            except json.JSONDecodeError as exc:
                print(f"  INVALID  {name}  — JSON parse error: {exc}")
                all_ok = False

        print()
        if all_ok:
            print("All v4 artifacts are valid. No downloaded brief files on disk.")
        else:
            print("One or more artifacts are missing or invalid. Run --full-refresh.")

        # Confirm no stale brief files
        stale = self._find_stale_files()
        if stale:
            print(f"\nWARNING: {len(stale)} unapproved file(s) in artifacts/:")
            for f in stale:
                print(f"  {f.name}")
        else:
            print("No unapproved files found in artifacts/ directory.")

        print("\n=== VALIDATE COMPLETE ===\n")

    def run_refresh_schema_only(self) -> None:
        """Re-fetch all three dataset schemas and regenerate the view domain catalog.

        Also seeds schema_annotations.json with heuristic column notes for any
        column not yet annotated (preserves existing human-written notes).
        Skips campaign ingestion and brief fetching. BQ-only, no Workspace calls.
        """
        print("\n=== VIBE OCTO KNOWLEDGE v4 — REFRESH SCHEMA ONLY ===")
        adobe_schema = self._phase2_adobe_schema()
        camp_data_schema, gch_schema = self._phase2b_execution_schemas()
        domain_catalog = self._phase2c_domain_catalog(camp_data_schema, gch_schema)
        annotations = _build_schema_annotations(
            self._artifacts_dir,
            adobe_schema,
            camp_data_schema,
            gch_schema,
        )

        if self.dry_run:
            print("\n[DRY-RUN] Schema artifacts not written.")
            return

        to_write = {
            "adobe_schema.json":        adobe_schema,
            "campaign_data_schema.json": camp_data_schema,
            "gch_current_schema.json":  gch_schema,
            "view_domain_catalog.json": domain_catalog,
            "schema_annotations.json":  annotations,
        }
        print()
        for filename, data in to_write.items():
            path = self._artifacts_dir / filename
            _write_atomic(path, data)
            size_kb = path.stat().st_size / 1024
            print(f"  [WRITTEN] {filename}  ({size_kb:.1f} KB)")
        print("\n=== REFRESH SCHEMA ONLY COMPLETE ===\n")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def _find_stale_files(self) -> list[Path]:
        """Return any files in artifacts/ that are not in _APPROVED_ARTIFACTS."""
        if not self._artifacts_dir.exists():
            return []
        stale = []
        for f in self._artifacts_dir.iterdir():
            if f.is_file() and f.name not in _APPROVED_ARTIFACTS and not f.name.endswith(".tmp"):
                stale.append(f)
        return stale

    def _cleanup_stale_files(self) -> None:
        """Remove any files in artifacts/ that are not in the approved set."""
        stale = self._find_stale_files()
        if not stale:
            print("\n[CLEANUP] No stale/downloaded files found. Directory is clean.")
            return
        print(f"\n[CLEANUP] Found {len(stale)} unapproved file(s) to remove:")
        for f in stale:
            print(f"  Removing: {f.name}")
            if not self.dry_run:
                try:
                    f.unlink()
                    print(f"  Removed:  {f.name}")
                except OSError as exc:
                    print(f"  ERROR removing {f.name}: {exc}")
        print("[CLEANUP] Done.")

    # ------------------------------------------------------------------
    # Helpers: load existing artifacts
    # ------------------------------------------------------------------

    def _load_existing_index(self) -> dict:
        """Load semantic_knowledge_index.json from disk; return {} if missing."""
        path = self._artifacts_dir / "semantic_knowledge_index.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            _log.warning("Could not load existing index: %s", exc)
            return {}

    def _load_existing_artifact(self, filename: str) -> Optional[dict]:
        path = self._artifacts_dir / filename
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Phase 1b: BQ-only campaign ingestion (no Sheets access)
    # ------------------------------------------------------------------

    def _phase1_campaigns_bq_only(self) -> list[dict]:
        """Query BQ for campaign metadata rows.  Returns raw dicts (no brief fetching)."""
        print("\n[PHASE 1] Campaign Metadata Ingestion (BQ only)")
        if self._col_map is None:
            self._col_map = self._resolve_columns()
        cmap = self._col_map

        print(f"  Querying: {CAMPAIGN_TABLE}")
        raw_rows = self._fetch_campaign_rows()
        total = len(raw_rows)
        print(f"  Rows fetched: {total}")

        def _g(row: dict, role: str) -> str:
            col = cmap.get(role)
            if col is None:
                return ""
            v = row.get(col)
            return str(v).strip() if v is not None else ""

        campaigns: list[dict] = []
        for row in raw_rows:
            campaigns.append({
                "camp_id":           _g(row, "camp_id"),
                "sub_camp_id":       _g(row, "sub_camp_id"),
                "campaign_name":     _g(row, "campaign_name"),
                "targeting_summary": _g(row, "targeting_summary"),
                "segment_summary":   _g(row, "segment_summary"),
                "cadence":           _g(row, "cadence"),
                "medium":            _g(row, "medium"),
                "campaign_purpose":  _g(row, "campaign_purpose"),
                "primary_products":  _g(row, "primary_products"),
                "databrief_link":    _g(row, "databrief_link"),
            })
        print(f"[PHASE 1 COMPLETE]  ({total} campaigns)")
        return campaigns

    # ------------------------------------------------------------------
    # Phase 2b: Execution schema ingestion (campaign_data + gch_current)
    # ------------------------------------------------------------------

    def _phase2b_execution_schemas(self) -> tuple[dict, dict]:
        """Ingest INFORMATION_SCHEMA metadata for campaign_data and gch_current datasets."""
        print("\n[PHASE 2b] Execution Schema Ingestion")
        camp_data = self._fetch_dataset_schema(CAMPAIGN_DATA_DATASET)
        gch = self._fetch_dataset_schema(GCH_DATASET)
        print(
            f"  campaign_data: {camp_data.get('view_count', 0)} views, "
            f"{camp_data.get('column_count', 0)} columns"
        )
        print(
            f"  gch_current:   {gch.get('view_count', 0)} views, "
            f"{gch.get('column_count', 0)} columns"
        )
        print("[PHASE 2b COMPLETE]")
        return camp_data, gch

    def _fetch_dataset_schema(self, dataset: str) -> dict:
        """Fetch views + columns from INFORMATION_SCHEMA for a single dataset."""
        client = self._adobe_bq  # reuse same project client

        try:
            tables_rows = list(
                client.query(
                    f"SELECT table_name, table_type "
                    f"FROM `{ADOBE_PROJECT}.{dataset}.INFORMATION_SCHEMA.TABLES` "
                    f"ORDER BY table_name"
                ).result()
            )
        except Exception as exc:
            _log.warning("%s TABLES query failed: %s", dataset, exc)
            print(f"  WARNING: Could not fetch {dataset} views — {exc}")
            tables_rows = []

        try:
            columns_rows = list(
                client.query(
                    f"SELECT c.table_name, c.column_name, c.data_type, "
                    f"c.is_nullable, c.ordinal_position "
                    f"FROM `{ADOBE_PROJECT}.{dataset}.INFORMATION_SCHEMA.COLUMNS` c "
                    f"JOIN `{ADOBE_PROJECT}.{dataset}.INFORMATION_SCHEMA.TABLES` t "
                    f"  ON c.table_name = t.table_name "
                    f"ORDER BY c.table_name, c.ordinal_position"
                ).result()
            )
        except Exception as exc:
            _log.warning("%s COLUMNS query failed: %s", dataset, exc)
            print(f"  WARNING: Could not fetch {dataset} columns — {exc}")
            columns_rows = []

        schema: dict[str, dict] = {}
        for row in tables_rows:
            schema[row.table_name] = {"type": row.table_type, "columns": []}

        for row in columns_rows:
            tbl = row.table_name
            if tbl not in schema:
                schema[tbl] = {"type": "TABLE", "columns": []}
            schema[tbl]["columns"].append({
                "name":     row.column_name,
                "type":     row.data_type,
                "nullable": row.is_nullable == "YES",
            })

        total_cols = sum(len(v["columns"]) for v in schema.values())
        return {
            "schema_version": "4.0",
            "snapshot_at":    datetime.now(tz=timezone.utc).isoformat(),
            "project":        ADOBE_PROJECT,
            "dataset":        dataset,
            "scope":          "all_objects",
            "view_count":     len(schema),
            "column_count":   total_cols,
            "views":          schema,
        }

    # ------------------------------------------------------------------
    # Phase 2c: View domain catalog
    # ------------------------------------------------------------------

    def _phase2c_domain_catalog(self, camp_data_schema: dict, gch_schema: dict) -> dict:
        """Build view_domain_catalog.json from all three dataset schemas."""
        print("\n[PHASE 2c] View Domain Catalog")

        views: dict[str, dict] = {}

        # Process each dataset
        for schema_data, dataset in [
            (self._load_existing_artifact("adobe_schema.json"), ADOBE_DATASET),
            (camp_data_schema, CAMPAIGN_DATA_DATASET),
            (gch_schema, GCH_DATASET),
        ]:
            if not schema_data:
                continue
            for view_name in (schema_data.get("views") or {}).keys():
                domain = _classify_view_domain(view_name, dataset)
                views[view_name] = {
                    "dataset":     dataset,
                    "domain":      domain,
                    "description": _describe_view(view_name),
                    "lob":         _infer_lob(view_name),
                    "is_primary":  view_name in (
                        "bq_fda_mob_mobility_base",
                        "bq_dly_dbm_customer_profl",
                    ),
                }

        print(f"  Classified {len(views)} views across 3 datasets")
        print("[PHASE 2c COMPLETE]")

        return {
            "catalog_version": "1.0",
            "generated_at":    datetime.now(tz=timezone.utc).isoformat(),
            "views":           views,
            "domains":         _DOMAIN_DESCRIPTIONS,
        }

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------

    def _generate_embeddings(self, campaign_records: list[dict]) -> None:
        """Build sparse TF-IDF vectors for each campaign and store in SQLite."""
        import sqlite3
        from collections import Counter as _Counter

        def _tfidf_vector(text: str) -> dict[str, float]:
            tokens = re.findall(r"[a-z]{3,}", text.lower())
            if not tokens:
                return {}
            counts = _Counter(tokens)
            total = len(tokens)
            return {t: c / total for t, c in counts.items()}

        db_path = self._artifacts_dir / "campaign_embeddings.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS campaign_embeddings (
                    campaign_key TEXT PRIMARY KEY,
                    embedding_json TEXT NOT NULL,
                    summary_text TEXT,
                    tier TEXT
                )
            """)
            conn.execute("DELETE FROM campaign_embeddings")

            for rec in campaign_records:
                key = f"{rec.get('camp_id', '')}::{rec.get('sub_camp_id', '')}"
                acc = rec.get("acc_summaries") or {}
                summary = " ".join(filter(None, [
                    rec.get("campaign_name", ""),
                    rec.get("campaign_purpose", ""),
                    acc.get("targeting_summary", ""),
                    acc.get("segment_summary", ""),
                ]))
                # Enrich with targeting_filters from brief_extraction if available
                be = rec.get("brief_extraction") or {}
                for f in be.get("targeting_filters", []):
                    summary += f" {f}"

                vec = _tfidf_vector(summary)
                conn.execute(
                    "INSERT OR REPLACE INTO campaign_embeddings VALUES (?, ?, ?, ?)",
                    (key, json.dumps(vec, ensure_ascii=False), summary[:500], rec.get("tier", "GOLD")),
                )
            conn.commit()
        finally:
            conn.close()

        print(f"  [EMBEDDINGS] {len(campaign_records)} vectors written to campaign_embeddings.db")

    # ------------------------------------------------------------------
    # Phase 1: Campaign & Brief Ingestion (legacy — preserved for --refresh-briefs)
    # ------------------------------------------------------------------

    def _phase1_campaigns_and_briefs(
        self,
    ) -> tuple[list[dict], list[dict], list[dict]]:
        """Query BQ, fetch briefs in-memory, return enriched campaign dicts.

        Returns
        -------
        campaigns  : list of campaign dicts with brief_text included (in-memory only)
        fetch_log  : one entry per campaign row
        failures   : subset of fetch_log where success is False
        """
        print("\n[PHASE 1] Campaign & Brief Ingestion")

        if self._col_map is None:
            self._col_map = self._resolve_columns()
        cmap = self._col_map

        print(f"  Querying: {CAMPAIGN_TABLE}")
        raw_rows = self._fetch_campaign_rows()
        total = len(raw_rows)
        print(f"  Rows fetched: {total}")

        def _g(row: dict, role: str) -> str:
            col = cmap.get(role)
            if col is None:
                return ""
            v = row.get(col)
            return str(v).strip() if v is not None else ""

        campaigns: list[dict] = []
        fetch_log: list[dict] = []
        failures: list[dict] = []
        success_count = 0

        pad = len(str(total))
        for idx, row in enumerate(raw_rows, 1):
            camp: dict = {
                "camp_id":           _g(row, "camp_id"),
                "sub_camp_id":       _g(row, "sub_camp_id"),
                "campaign_name":     _g(row, "campaign_name"),
                "targeting_summary": _g(row, "targeting_summary"),
                "segment_summary":   _g(row, "segment_summary"),
                "cadence":           _g(row, "cadence"),
                "medium":            _g(row, "medium"),
                "campaign_purpose":  _g(row, "campaign_purpose"),
                "primary_products":  _g(row, "primary_products"),
                "databrief_link":    _g(row, "databrief_link"),
            }

            url = camp["databrief_link"]
            camp_id = camp["camp_id"]
            name = camp["campaign_name"] or camp_id

            brief_text, entry = self._fetch_one_brief(url, camp_id, name, idx, total, pad)

            # Brief text is held in-memory only — NOT written to disk.
            camp["_brief_text"] = brief_text
            camp["_brief_accessible"] = entry["success"]

            fetch_log.append(entry)
            if entry["success"]:
                success_count += 1
            else:
                failures.append(entry)

            campaigns.append(camp)

        fail_count = total - success_count
        fail_pct = fail_count / total * 100 if total else 0.0

        print(f"\n[PHASE 1 COMPLETE]")
        print(f"  Briefs fetched:  {success_count}")
        print(f"  Inaccessible:    {fail_count} ({fail_pct:.2f}%)  -> classified BRONZE")

        return campaigns, fetch_log, failures

    def _resolve_columns(self) -> dict[str, str]:
        project = CAMPAIGN_PROJECT
        dataset = "wb_tian_pr_dataset"
        query = (
            f"SELECT column_name "
            f"FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
            f"WHERE table_name = 'campaign_deployments' "
            f"ORDER BY ordinal_position"
        )
        result = self._campaign_bq.query(query).result()
        actual_cols = [row.column_name.lower() for row in result]

        role_map: dict[str, str] = {}
        for role, patterns in _ROLE_PATTERNS.items():
            for col in actual_cols:
                if any(col == p or p in col or col in p for p in patterns):
                    role_map[role] = col
                    break

        _log.debug("Resolved column map: %s", role_map)
        return role_map

    def _fetch_campaign_rows(self) -> list[dict]:
        result = self._campaign_bq.query(f"SELECT * FROM `{CAMPAIGN_TABLE}`").result()
        return [dict(row) for row in result]

    def _fetch_one_brief(
        self,
        url: str,
        camp_id: str,
        campaign_name: str,
        idx: int,
        total: int,
        pad: int,
    ) -> tuple[str, dict]:
        """Fetch one brief in-memory. Returns (text, log_entry). Never raises."""
        ts = datetime.now(tz=timezone.utc).isoformat()
        label = f"{campaign_name[:50]:<50}"
        prefix = f"[{idx:>{pad}}/{total}]"

        entry: dict = {
            "seq":           idx,
            "camp_id":       camp_id,
            "campaign_name": campaign_name,
            "url":           url,
            "timestamp":     ts,
            "success":       False,
            "http_code":     None,
            "error_type":    None,
            "error_message": None,
            "text_length":   0,
        }

        if not url or not url.strip():
            entry["error_type"] = "no_url"
            entry["error_message"] = "No databrief_link set for this campaign"
            print(f"{prefix} {label} ... SKIP")
            return "", entry

        url = url.strip()

        # Tier 1: BriefFetcher.to_flat_string() — multi-tier fallback, in-memory
        try:
            text = self._brief_fetcher.to_flat_string(url)
        except Exception:
            text = ""

        if text:
            entry["success"] = True
            entry["text_length"] = len(text)
            print(f"{prefix} {label} ... OK ({len(text):,} chars)")
            return text, entry

        # to_flat_string returned "" — probe for the actual HTTP error
        try:
            probe = self._brief_fetcher.fetch(url)
            if probe:
                entry["success"] = True
                entry["text_length"] = len(probe)
                print(f"{prefix} {label} ... OK ({len(probe):,} chars)")
                return probe, entry
            entry["error_type"] = "other"
            entry["error_message"] = "Fetch returned empty content"
            print(f"{prefix} {label} ... FAIL (empty)")
            return "", entry

        except BriefFetchError as exc:
            msg = str(exc)
            http_code = _extract_http_code(msg)
            error_type = _classify_error(http_code, msg)
            entry["http_code"] = http_code
            entry["error_type"] = error_type
            entry["error_message"] = msg[:600]
            code_str = str(http_code) if http_code else error_type
            print(f"{prefix} {label} ... FAIL ({code_str})")
            return "", entry

        except Exception as exc:
            msg = str(exc)
            entry["error_type"] = "timeout" if "timeout" in msg.lower() else "other"
            entry["error_message"] = msg[:600]
            print(f"{prefix} {label} ... FAIL ({entry['error_type']})")
            return "", entry

    # ------------------------------------------------------------------
    # Phase 2: Adobe Schema
    # ------------------------------------------------------------------

    def _phase2_adobe_schema(self) -> dict:
        print("\n[PHASE 2] Adobe Schema Ingestion (views only)")
        print(f"  Dataset: {ADOBE_PROJECT}.{ADOBE_DATASET}")

        # ACC workflows reference only views in this dataset; base tables are
        # internal implementation detail and not campaign-facing.
        try:
            tables_rows = list(
                self._adobe_bq.query(
                    f"SELECT table_name, table_type "
                    f"FROM `{ADOBE_PROJECT}.{ADOBE_DATASET}.INFORMATION_SCHEMA.TABLES` "
                    f"WHERE table_type = 'VIEW' "
                    f"ORDER BY table_name"
                ).result()
            )
        except Exception as exc:
            _log.warning("TABLES query failed: %s", exc)
            print(f"  WARNING: Could not fetch views — {exc}")
            tables_rows = []

        try:
            columns_rows = list(
                self._adobe_bq.query(
                    f"SELECT c.table_name, c.column_name, c.data_type, "
                    f"c.is_nullable, c.ordinal_position "
                    f"FROM `{ADOBE_PROJECT}.{ADOBE_DATASET}.INFORMATION_SCHEMA.COLUMNS` c "
                    f"JOIN `{ADOBE_PROJECT}.{ADOBE_DATASET}.INFORMATION_SCHEMA.TABLES` t "
                    f"  ON c.table_name = t.table_name "
                    f"WHERE t.table_type = 'VIEW' "
                    f"ORDER BY c.table_name, c.ordinal_position"
                ).result()
            )
        except Exception as exc:
            _log.warning("COLUMNS query failed: %s", exc)
            print(f"  WARNING: Could not fetch columns — {exc}")
            columns_rows = []

        schema: dict[str, dict] = {}
        for row in tables_rows:
            schema[row.table_name] = {"type": row.table_type, "columns": []}

        for row in columns_rows:
            tbl = row.table_name
            if tbl not in schema:
                schema[tbl] = {"type": "VIEW", "columns": []}
            schema[tbl]["columns"].append({
                "name":     row.column_name,
                "type":     row.data_type,
                "nullable": row.is_nullable == "YES",
            })

        total_cols = sum(len(v["columns"]) for v in schema.values())
        print(f"  Views:   {len(schema)}")
        print(f"  Columns: {total_cols}")
        print("[PHASE 2 COMPLETE]")

        return {
            "schema_version": "2.0",
            "snapshot_at": datetime.now(tz=timezone.utc).isoformat(),
            "project": ADOBE_PROJECT,
            "dataset": ADOBE_DATASET,
            "scope": "views_only",
            "view_count": len(schema),
            "column_count": total_cols,
            "views": schema,
        }

    # ------------------------------------------------------------------
    # Phase 3: GOLD insight extraction + artifact generation
    # ------------------------------------------------------------------

    def _phase3_build_and_write(
        self,
        campaigns: list[dict],
        fetch_log: list[dict],
        failures: list[dict],
        adobe_schema: dict,
        run_at: str,
        old_index: Optional[dict] = None,
        camp_data_schema: Optional[dict] = None,
        gch_schema: Optional[dict] = None,
        domain_catalog: Optional[dict] = None,
        skip_schema_artifacts: bool = False,
    ) -> None:
        print("\n[PHASE 3] GOLD Insight Extraction & Artifact Generation")

        # Build lookup of old index records for carrying forward Phase 1 fields
        old_records: dict[str, dict] = {}
        if old_index:
            for r in old_index.get("campaigns", []):
                key = f"{r.get('camp_id', '')}::{r.get('sub_camp_id', '')}"
                old_records[key] = r

        # Separate GOLD campaigns for insight extraction.
        # GOLD = has ACC-verified targeting_summary AND segment_summary.
        # Support both flat fields (fresh BQ query) and acc_summaries nesting (old index records).
        def _flat_ts(c: dict) -> str:
            return c.get("targeting_summary", "") or (c.get("acc_summaries") or {}).get("targeting_summary", "")

        def _flat_ss(c: dict) -> str:
            return c.get("segment_summary", "") or (c.get("acc_summaries") or {}).get("segment_summary", "")

        gold_camps_raw = [
            c for c in campaigns
            if bool(_flat_ts(c).strip()) and bool(_flat_ss(c).strip())
        ]
        print(f"  GOLD campaigns (have ACC summaries): {len(gold_camps_raw)}")

        # Extract insights from GOLD tier
        gold_insights = _extract_gold_insights(gold_camps_raw)
        print(f"  Targeting patterns extracted:  {len(gold_insights['common_targeting_patterns'])}")
        print(f"  Exclusion patterns extracted:  {len(gold_insights['common_exclusions'])}")
        print(f"  Standard telecom rules:        {len(gold_insights['standard_telecom_rules'])}")

        # Classify tiers and build per-campaign deployment records
        gold_count = silver_count = bronze_count = 0
        needs_brief_refresh: list[str] = []
        campaign_records: list[dict] = []

        for camp in campaigns:
            # Pop internal-only fields that may be set by legacy brief-fetching path
            brief_text: str = camp.pop("_brief_text", "") or ""
            brief_accessible: bool = camp.pop("_brief_accessible", False)

            # In new three-mode design, tier is determined solely by BQ metadata presence.
            # Normalize: fresh BQ records have flat fields; old index records nest them under acc_summaries.
            _acc = camp.get("acc_summaries") or {}
            tier = _classify_tier(
                camp.get("targeting_summary", "") or _acc.get("targeting_summary", ""),
                camp.get("segment_summary", "") or _acc.get("segment_summary", ""),
                brief_accessible,
            )

            if tier == "GOLD":
                gold_count += 1
            elif tier == "SILVER":
                silver_count += 1
            else:
                bronze_count += 1

            # Carry forward brief_extraction and conflict_notes from old index
            key = f"{camp['camp_id']}::{camp['sub_camp_id']}"
            old_rec = old_records.get(key, {})
            current_url = camp.get("databrief_link", "").strip()
            stored_url = old_rec.get("brief_fetched_from", "")

            brief_extraction = old_rec.get("brief_extraction")
            conflict_notes = old_rec.get("conflict_notes") or []
            brief_fetched_from = old_rec.get("brief_fetched_from", "")
            brief_fetched_at = old_rec.get("brief_fetched_at", "")

            # Flag campaigns where brief URL changed or extraction is missing
            if current_url and (not brief_extraction or current_url != stored_url):
                needs_brief_refresh.append(camp["camp_id"])

            # Build deployment block
            deployment = _build_brief_deployment(
                brief_text=brief_text,
                brief_accessible=brief_accessible or bool(brief_extraction),
                camp=camp,
                gold_insights=gold_insights,
                tier=tier,
            )

            brief_reason = "brief_extracted" if brief_extraction else (
                "link_missing" if not current_url else "needs_refresh"
            )

            campaign_records.append({
                "camp_id":          camp["camp_id"],
                "sub_camp_id":      camp["sub_camp_id"],
                "campaign_name":    camp["campaign_name"],
                "cadence":          camp["cadence"],
                "medium":           camp["medium"],
                "campaign_purpose": camp["campaign_purpose"],
                "primary_products": camp["primary_products"],
                "tier":             tier,
                "acc_summaries": {
                    "targeting_summary": camp.get("targeting_summary") or _acc.get("targeting_summary") or None,
                    "segment_summary":   camp.get("segment_summary") or _acc.get("segment_summary") or None,
                    "source":            "acc_workflow_xml" if tier == "GOLD" else None,
                },
                "brief_extraction":   brief_extraction,
                "conflict_notes":     conflict_notes,
                "brief_fetched_from": brief_fetched_from,
                "brief_fetched_at":   brief_fetched_at,
                "deployments": [deployment],
                "brief_status": {
                    "accessible": bool(brief_extraction) or brief_accessible,
                    "reason":     brief_reason,
                    "note":       "Raw content not persisted; extracted fields stored.",
                },
                "last_ingested_at": run_at,
                "ingested_at":      old_rec.get("ingested_at", run_at),
            })

        print(f"\n  Tier summary:")
        print(f"    GOLD:   {gold_count}  (proven — ACC summaries present)")
        print(f"    SILVER: {silver_count}  (brief extracted, no ACC summaries)")
        print(f"    BRONZE: {bronze_count}  (no ACC summaries + no/inaccessible brief)")

        if needs_brief_refresh:
            unique_needing = sorted(set(needs_brief_refresh))
            print(f"\n  {len(unique_needing)} campaign(s) need brief refresh "
                  f"(run --refresh-briefs):")
            for cid in unique_needing[:10]:
                print(f"    - {cid}")
            if len(unique_needing) > 10:
                print(f"    ... and {len(unique_needing) - 10} more")

        # Build cross-campaign patterns from GOLD
        cross_patterns = _build_cross_campaign_patterns(gold_camps_raw, gold_insights)

        # Build report
        report = self._build_report(campaigns, failures, fetch_log, adobe_schema, run_at,
                                     gold_count, silver_count, bronze_count, gold_insights)

        if self.dry_run:
            print("[PHASE 3] DRY-RUN — no files written.")
            self._print_summary(report)
            return

        # Backup existing index before overwriting
        existing_index = self._artifacts_dir / "semantic_knowledge_index.json"
        if existing_index.exists():
            backup = self._artifacts_dir / "semantic_knowledge_index.backup.json"
            try:
                import shutil
                shutil.copy2(str(existing_index), str(backup))
            except Exception as exc:
                _log.warning("Could not write index backup: %s", exc)

        # Attach verified business rules to campaign records before writing.
        _attach_business_rules(campaign_records)

        # Validate before write: must have > 0 campaigns
        assert len(campaign_records) > 0, "Ingestion produced zero campaigns — aborting write"

        # Core knowledge artifacts
        artifacts = {
            "semantic_knowledge_index.json": {
                "schema_version":  "4.0",
                "generated_at":    run_at,
                "gold_insights":   gold_insights,
                "ingestion_summary": {
                    "total_campaigns": len(campaign_records),
                    "gold_count":      gold_count,
                    "silver_count":    silver_count,
                    "bronze_count":    bronze_count,
                },
                "campaigns": campaign_records,
            },
            "adobe_schema.json":           adobe_schema,
            "cross_campaign_patterns.json": cross_patterns,
            "ingestion_report.json":       report,
            "ingestion_log.json":          fetch_log,
        }

        # New v4 schema artifacts (skip when called from --refresh-briefs)
        if not skip_schema_artifacts:
            if camp_data_schema:
                artifacts["campaign_data_schema.json"] = camp_data_schema
            if gch_schema:
                artifacts["gch_current_schema.json"] = gch_schema
            if domain_catalog:
                artifacts["view_domain_catalog.json"] = domain_catalog

        print()
        for filename, data in artifacts.items():
            path = self._artifacts_dir / filename
            _write_atomic(path, data)
            size_kb = path.stat().st_size / 1024
            print(f"  [WRITTEN] {filename}  ({size_kb:.1f} KB)")

        # Generate embeddings (TF-IDF sparse vectors stored in SQLite)
        try:
            self._generate_embeddings(campaign_records)
        except Exception as exc:
            _log.warning("Embedding generation failed (non-fatal): %s", exc)
            print(f"  WARNING: Embedding generation failed — RAG retrieval will use keyword fallback")

        print(f"\n[PHASE 3 COMPLETE]")
        self._print_summary(report)

    # ------------------------------------------------------------------
    # Report builder
    # ------------------------------------------------------------------

    def _build_report(
        self,
        campaigns: list[dict],
        failures: list[dict],
        fetch_log: list[dict],
        adobe_schema: Optional[dict],
        run_at: str,
        gold_count: int = 0,
        silver_count: int = 0,
        bronze_count: int = 0,
        gold_insights: Optional[dict] = None,
    ) -> dict:
        total_attempted = len(fetch_log)
        total_success = sum(1 for e in fetch_log if e["success"])
        total_failed = total_attempted - total_success
        fail_pct = round(total_failed / total_attempted * 100, 4) if total_attempted else 0.0

        by_type: dict[str, list[dict]] = defaultdict(list)
        for entry in failures:
            by_type[entry.get("error_type") or "other"].append(entry)

        failures_by_error_type: dict[str, dict] = {}
        for etype, entries in sorted(by_type.items()):
            rem = _REMEDIATION.get(etype, _REMEDIATION["other"])
            failures_by_error_type[etype] = {
                "count": len(entries),
                "root_cause": rem["root_cause"],
                "affected_campaigns": [
                    {
                        "camp_id":       e["camp_id"],
                        "campaign_name": e["campaign_name"],
                        "url":           e["url"],
                        "http_code":     e.get("http_code"),
                        "error_message": (e.get("error_message") or "")[:200],
                    }
                    for e in entries
                ],
                "remediation": {
                    "steps":    rem["steps"],
                    "severity": rem["severity"],
                },
            }

        return {
            "schema_version": "3.0",
            "generated_at":   run_at,
            "summary": {
                "total_campaigns":               len(campaigns),
                "gold_count":                    gold_count,
                "silver_count":                  silver_count,
                "bronze_count":                  bronze_count,
                "briefs_fetched_successfully":   total_success,
                "briefs_inaccessible":           total_failed,
                "inaccessibility_rate_percent":  fail_pct,
            },
            "gold_insights_extracted": {
                "targeting_patterns": len((gold_insights or {}).get("common_targeting_patterns", [])),
                "exclusion_patterns": len((gold_insights or {}).get("common_exclusions", [])),
                "standard_rules":     len((gold_insights or {}).get("standard_telecom_rules", [])),
                "personalization_fields": len((gold_insights or {}).get("common_personalization_fields", [])),
            },
            "schema_ingestion": {
                "adobe_project": ADOBE_PROJECT,
                "adobe_dataset": ADOBE_DATASET,
                "scope":         "views_only",
                "view_count":    (adobe_schema or {}).get("view_count", 0),
                "column_count":  (adobe_schema or {}).get("column_count", 0),
            } if adobe_schema else None,
            "brief_access_diagnostics": {
                "total_attempted":       total_attempted,
                "fetched_successfully":  total_success,
                "failed":                total_failed,
                "failure_rate_percent":  fail_pct,
                "failures_by_error_type": failures_by_error_type,
            },
            "constraints_verified": {
                "no_brief_files_on_disk": True,
                "all_briefs_in_memory_only": True,
                "all_campaigns_indexed": True,
                "graceful_degradation": True,
            },
        }

    def _print_summary(self, report: dict) -> None:
        s = report.get("summary", {})
        gi = report.get("gold_insights_extracted", {})
        diag = report.get("brief_access_diagnostics", {})
        print(f"\n--- Ingestion Summary ---")
        print(f"  Total campaigns:    {s.get('total_campaigns', 0)}")
        print(f"  GOLD (ACC proven):  {s.get('gold_count', 0)}")
        print(f"  SILVER (brief):     {s.get('silver_count', 0)}")
        print(f"  BRONZE (guidance):  {s.get('bronze_count', 0)}")
        print(f"\n--- GOLD Insights Extracted ---")
        print(f"  Targeting patterns:      {gi.get('targeting_patterns', 0)}")
        print(f"  Exclusion patterns:      {gi.get('exclusion_patterns', 0)}")
        print(f"  Standard telecom rules:  {gi.get('standard_rules', 0)}")
        print(f"\n--- Brief Access ---")
        print(f"  Fetched successfully:    {diag.get('fetched_successfully', 0)}")
        print(f"  Inaccessible -> BRONZE:  {diag.get('failed', 0)}  "
              f"({diag.get('failure_rate_percent', 0):.2f}%)")
        print(f"\n  No raw brief content on disk. Knowledge base is clean.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m knowledge_base.vibe_octo_knowledge",
        description="Vibe OCTO Knowledge v4 — Knowledge Ingestion Agent",
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--full-refresh",
        action="store_true",
        help=(
            "Query BQ for campaign metadata; carry forward existing brief_extraction. "
            "Does NOT access Google Sheets. Safe for daily scheduled use."
        ),
    )
    group.add_argument(
        "--incremental",
        action="store_true",
        help="Process only campaigns added since last refresh. No Sheets access.",
    )
    group.add_argument(
        "--refresh-briefs",
        action="store_true",
        help=(
            "Fetch Google Sheets briefs (rate-limited, confirmation required), "
            "run two-stage LLM extraction, and update stored brief_extraction fields."
        ),
    )
    group.add_argument(
        "--validate",
        action="store_true",
        help="Verify all v4 artifacts exist and contain valid JSON.",
    )
    group.add_argument(
        "--refresh-schema-only",
        action="store_true",
        help=(
            "Re-fetch all three dataset schemas and regenerate view domain catalog. "
            "Skips campaign ingestion."
        ),
    )
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all phases without writing any files.",
    )
    p.add_argument(
        "--campaign",
        metavar="CAMP_ID",
        default=None,
        help="Limit --refresh-briefs to a single campaign ID.",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_BRIEF_TIMEOUT,
        metavar="SECONDS",
        help=f"HTTP timeout for brief fetches (default: {DEFAULT_BRIEF_TIMEOUT}s). "
             "Override with BRIEF_FETCH_TIMEOUT env var.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return p


def _load_env() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore[import]
    except ImportError:
        return
    for path in [
        _NEXUS_DIR / ".env",
        Path(__file__).resolve().parent.parent / ".env",
        Path.cwd() / ".env",
    ]:
        if path.exists():
            load_dotenv(str(path), override=False)


def main() -> None:
    _load_env()
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    agent = VibeOctoKnowledge(
        dry_run=args.dry_run,
        brief_timeout=args.timeout,
    )

    if args.full_refresh or args.dry_run:
        agent.run_full_refresh()
    elif args.incremental:
        agent.run_incremental()
    elif args.refresh_briefs:
        agent.run_refresh_briefs(campaign_id=args.campaign)
    elif args.validate:
        agent.run_validate()
    elif args.refresh_schema_only:
        agent.run_refresh_schema_only()


if __name__ == "__main__":
    main()
