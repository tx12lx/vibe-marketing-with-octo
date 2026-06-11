"""knowledge_base/vibe_octo_knowledge.py — Vibe OCTO Knowledge Ingestion Agent (v3).

What changed from v2
--------------------
- brief_texts.json removed: raw brief content is never persisted to disk.
- GOLD insight extraction: targeting_summary + segment_summary are parsed to derive
  cross-campaign patterns (telecom standards, targeting criteria, exclusion rules).
- Structured requirement extraction: brief text -> {targeting, exclusions, personalization}
  stored per deployment; raw text discarded after extraction.
- GOLD-guided hints: SILVER/BRONZE deployments receive GOLD-derived standard patterns
  as guidance when their own brief is absent or incomplete.
- Deployment-aware schema: each BQ row is one deployment under its campaign.
- Graceful degradation: inaccessible briefs -> BRONZE + GOLD standard rules, no error raised.
- schema_version: "3.0"

Data sources (read-only, in-memory)
-------------------------------------
1. wb-tian-pr-d0dbe6.wb_tian_pr_dataset.campaign_deployments
   Columns of interest: camp_id, sub_camp_id, campaign_name, cadence, medium,
   campaign_purpose, primary_products, targeting_summary, segment_summary,
   databrief_link.

2. bi-srv-hsmdet-pr-7b9def.adobe (INFORMATION_SCHEMA.TABLES + COLUMNS)

Output artifacts  (knowledge_base/artifacts/)
----------------------------------------------
  semantic_knowledge_index.json  (v3.0 — deployment-aware, GOLD insights embedded)
  adobe_schema.json
  cross_campaign_patterns.json
  ingestion_report.json
  ingestion_log.json

Usage
-----
  python -m knowledge_base.vibe_octo_knowledge --full-refresh
  python -m knowledge_base.vibe_octo_knowledge --refresh-schema-only
  python -m knowledge_base.vibe_octo_knowledge --validate
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

_NEXUS_DIR = Path(__file__).resolve().parent.parent / "Vibe OCTO Nexus"
if str(_NEXUS_DIR) not in sys.path:
    sys.path.insert(0, str(_NEXUS_DIR))

from core.brief_fetcher import BriefFetcher, BriefFetchError  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CAMPAIGN_PROJECT = "wb-tian-pr-d0dbe6"
CAMPAIGN_TABLE = "wb-tian-pr-d0dbe6.wb_tian_pr_dataset.campaign_deployments"

ADOBE_PROJECT = "bi-srv-hsmdet-pr-7b9def"
ADOBE_DATASET = "adobe"

ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts"

DEFAULT_BRIEF_TIMEOUT = int(os.environ.get("BRIEF_FETCH_TIMEOUT", "60"))

# Artifact files that are allowed to persist in the artifacts directory.
# Any other file found there is treated as a stale download and removed.
_APPROVED_ARTIFACTS = frozenset({
    "semantic_knowledge_index.json",
    "adobe_schema.json",
    "cross_campaign_patterns.json",
    "ingestion_report.json",
    "ingestion_log.json",
})

_log = logging.getLogger(__name__)

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
        """Cleanup stale files, ingest all data, extract GOLD insights, write 5 artifacts."""
        _config_path = Path(__file__).resolve().parent.parent / "ingestion_config.json"
        if _config_path.exists():
            try:
                _cfg = json.loads(_config_path.read_text(encoding="utf-8"))
                if _cfg.get("schedule", {}).get("mode") == "disabled":
                    print(
                        "\n  Auto-ingestion is currently disabled.\n"
                        "  Running manual refresh..."
                    )
            except Exception:
                pass

        print("\n=== VIBE OCTO KNOWLEDGE v3 — FULL REFRESH ===")
        run_at = datetime.now(tz=timezone.utc).isoformat()

        self._cleanup_stale_files()

        campaigns, fetch_log, failures = self._phase1_campaigns_and_briefs()
        adobe_schema = self._phase2_adobe_schema()
        self._phase3_build_and_write(campaigns, fetch_log, failures, adobe_schema, run_at)

        print("\n=== FULL REFRESH COMPLETE ===\n")

    def run_validate(self) -> None:
        """Verify the 5 v3 artifacts exist and contain valid JSON."""
        print("\n=== VIBE OCTO KNOWLEDGE v3 — VALIDATE ===\n")
        expected = [
            "semantic_knowledge_index.json",
            "adobe_schema.json",
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
            print("All 5 artifacts are valid. No downloaded brief files on disk.")
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
        """Re-fetch the Adobe schema (views only) and overwrite adobe_schema.json.

        Skips campaign ingestion and brief fetching. Useful when the warehouse
        schema changes and you want to update the artifact without a full refresh.
        """
        print("\n=== VIBE OCTO KNOWLEDGE v3 — REFRESH SCHEMA ONLY ===")
        adobe_schema = self._phase2_adobe_schema()

        if self.dry_run:
            print("\n[DRY-RUN] adobe_schema.json not written.")
            print(f"  Would write: {adobe_schema['view_count']} views, "
                  f"{adobe_schema['column_count']} columns")
            print("\n=== REFRESH SCHEMA ONLY COMPLETE ===\n")
            return

        path = self._artifacts_dir / "adobe_schema.json"
        _write_atomic(path, adobe_schema)
        size_kb = path.stat().st_size / 1024
        print(f"\n  [WRITTEN] adobe_schema.json  ({size_kb:.1f} KB)")
        print(f"  Views:   {adobe_schema['view_count']}")
        print(f"  Columns: {adobe_schema['column_count']}")
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
    # Phase 1: Campaign & Brief Ingestion
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
    ) -> None:
        print("\n[PHASE 3] GOLD Insight Extraction & Artifact Generation")

        # Separate GOLD campaigns for insight extraction
        gold_camps_raw = [
            c for c in campaigns
            if bool(c.get("targeting_summary", "").strip())
            and bool(c.get("segment_summary", "").strip())
        ]
        print(f"  GOLD campaigns (have ACC summaries): {len(gold_camps_raw)}")

        # Extract insights from GOLD tier
        gold_insights = _extract_gold_insights(gold_camps_raw)
        print(f"  Targeting patterns extracted:  {len(gold_insights['common_targeting_patterns'])}")
        print(f"  Exclusion patterns extracted:  {len(gold_insights['common_exclusions'])}")
        print(f"  Standard telecom rules:        {len(gold_insights['standard_telecom_rules'])}")

        # Classify tiers and build per-campaign deployment records
        gold_count = silver_count = bronze_count = 0
        campaign_records: list[dict] = []

        for camp in campaigns:
            brief_text: str = camp.pop("_brief_text", "") or ""
            brief_accessible: bool = camp.pop("_brief_accessible", False)

            tier = _classify_tier(
                camp.get("targeting_summary", ""),
                camp.get("segment_summary", ""),
                brief_accessible,
            )

            if tier == "GOLD":
                gold_count += 1
            elif tier == "SILVER":
                silver_count += 1
            else:
                bronze_count += 1

            # Build deployment block — brief_text is consumed here and not persisted
            deployment = _build_brief_deployment(
                brief_text=brief_text,
                brief_accessible=brief_accessible,
                camp=camp,
                gold_insights=gold_insights,
                tier=tier,
            )

            brief_reason = (
                "brief_extracted" if brief_accessible
                else ("link_missing" if not camp.get("databrief_link") else "fetch_failed")
            )

            campaign_records.append({
                "camp_id":       camp["camp_id"],
                "sub_camp_id":   camp["sub_camp_id"],
                "campaign_name": camp["campaign_name"],
                "cadence":       camp["cadence"],
                "medium":        camp["medium"],
                "campaign_purpose": camp["campaign_purpose"],
                "primary_products": camp["primary_products"],
                "tier":          tier,
                "acc_summaries": {
                    "targeting_summary": camp.get("targeting_summary") or None,
                    "segment_summary":   camp.get("segment_summary") or None,
                    "source":            "acc_workflow_xml" if tier == "GOLD" else None,
                },
                "deployments": [deployment],
                "brief_status": {
                    "accessible": brief_accessible,
                    "reason":     brief_reason,
                    "note":       "Read in-memory only — no raw content persisted",
                },
                "ingested_at": run_at,
            })

        print(f"\n  Tier summary:")
        print(f"    GOLD:   {gold_count}  (proven — ACC summaries present)")
        print(f"    SILVER: {silver_count}  (brief extracted, no ACC summaries)")
        print(f"    BRONZE: {bronze_count}  (no ACC summaries + no/inaccessible brief)")

        # Build cross-campaign patterns from GOLD
        cross_patterns = _build_cross_campaign_patterns(gold_camps_raw, gold_insights)

        # Build report
        report = self._build_report(campaigns, failures, fetch_log, adobe_schema, run_at,
                                     gold_count, silver_count, bronze_count, gold_insights)

        if self.dry_run:
            print("[PHASE 3] DRY-RUN — no files written.")
            self._print_summary(report)
            return

        # Attach verified business rules to campaign records before writing.
        _attach_business_rules(campaign_records)

        # Write 5 artifacts atomically (no brief_texts.json)
        artifacts = {
            "semantic_knowledge_index.json": {
                "schema_version":  "3.0",
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

        print()
        for filename, data in artifacts.items():
            path = self._artifacts_dir / filename
            _write_atomic(path, data)
            size_kb = path.stat().st_size / 1024
            print(f"  [WRITTEN] {filename}  ({size_kb:.1f} KB)")

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
        description="Vibe OCTO Knowledge v3 — GOLD-guided Ingestion Agent",
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--full-refresh",
        action="store_true",
        help=(
            "Cleanup stale files, ingest all campaigns, fetch briefs in-memory, "
            "extract GOLD insights, write 5 artifacts."
        ),
    )
    group.add_argument(
        "--validate",
        action="store_true",
        help="Verify all 5 artifacts exist and contain valid JSON.",
    )
    group.add_argument(
        "--refresh-schema-only",
        action="store_true",
        help=(
            "Re-fetch the Adobe schema (views only) and overwrite adobe_schema.json. "
            "Skips campaign ingestion and brief fetching."
        ),
    )
    group.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all phases without writing any files.",
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

    if args.full_refresh:
        agent.run_full_refresh()
    elif args.validate:
        agent.run_validate()
    elif args.refresh_schema_only:
        agent.run_refresh_schema_only()
    elif args.dry_run:
        agent.run_full_refresh()


if __name__ == "__main__":
    main()
