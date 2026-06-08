"""knowledge_base/vibe_octo_knowledge.py — Vibe OCTO Knowledge Weekly Ingestion Agent.

Ingests data from two BigQuery pipelines, fetches all campaign data-briefs with
full per-brief diagnostics, and writes six structured artifacts.

Data sources
------------
1. wb-tian-pr-d0dbe6.wb_tian_pr_dataset.campaign_knowledge
   1000+ campaigns with metadata and databrief_link URLs.

2. bi-srv-hsmdet-pr-7b9def.adobe
   Warehouse schema (all tables + columns).

Output artifacts  (knowledge_base/artifacts/)
--------------------------------------------
  semantic_knowledge_index.json  (v2.0)
  brief_texts.json
  adobe_schema.json
  cross_campaign_patterns.json
  ingestion_report.json
  ingestion_log.json

Usage
-----
  python -m knowledge_base.vibe_octo_knowledge --full-refresh
  python -m knowledge_base.vibe_octo_knowledge --diagnose-briefs
  python -m knowledge_base.vibe_octo_knowledge --validate
  python -m knowledge_base.vibe_octo_knowledge --dry-run
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

# ---------------------------------------------------------------------------
# Register the Nexus package root so BriefFetcher is importable without
# touching the nexus_agent entry point.
# ---------------------------------------------------------------------------
_NEXUS_DIR = Path(__file__).resolve().parent.parent / "Vibe OCTO Nexus"
if str(_NEXUS_DIR) not in sys.path:
    sys.path.insert(0, str(_NEXUS_DIR))

from core.brief_fetcher import BriefFetcher, BriefFetchError  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CAMPAIGN_PROJECT = "wb-tian-pr-d0dbe6"
CAMPAIGN_TABLE = "wb-tian-pr-d0dbe6.wb_tian_pr_dataset.campaign_knowledge"

ADOBE_PROJECT = "bi-srv-hsmdet-pr-7b9def"
ADOBE_DATASET = "adobe"

ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts"

DEFAULT_BRIEF_TIMEOUT = int(os.environ.get("BRIEF_FETCH_TIMEOUT", "60"))

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column role discovery patterns (applied against INFORMATION_SCHEMA results)
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
# Remediation catalogue — one entry per error type
# ---------------------------------------------------------------------------

_REMEDIATION: dict[str, dict] = {
    "401_unauthorized": {
        "root_cause": (
            "ADC credentials are valid but the token is missing required Google API "
            "scopes (spreadsheets.readonly / drive.readonly). Google rejects the "
            "request before checking document sharing settings."
        ),
        "severity": "HIGH",
        "steps": [
            "Check current ADC: run `gcloud auth application-default list`",
            "Re-authenticate with all required scopes:\n"
            "    gcloud auth application-default login \\\n"
            "        --scopes=https://www.googleapis.com/auth/cloud-platform,"
            "https://www.googleapis.com/auth/spreadsheets.readonly,"
            "https://www.googleapis.com/auth/drive.readonly",
            "If using a service account key, verify GOOGLE_APPLICATION_CREDENTIALS "
            "points to a key file whose service account has Sheets/Drive API enabled.",
            "Re-run: python -m knowledge_base.vibe_octo_knowledge --diagnose-briefs",
        ],
    },
    "403_forbidden": {
        "root_cause": (
            "The authenticated identity (gcloud user or service account) does not "
            "have permission to access the specific document. The token scopes are "
            "valid, but sharing has not been granted for these sheets."
        ),
        "severity": "HIGH",
        "steps": [
            "Identify the ADC identity in use:\n"
            "    gcloud config get-value account",
            "For EACH affected campaign, open the databrief_link URL in a browser.",
            "Click Share (top-right in the Google Sheet / Doc).",
            "Add the ADC identity email with at least Viewer access.",
            "Click Send / Done.",
            "Re-run: python -m knowledge_base.vibe_octo_knowledge --diagnose-briefs",
        ],
    },
    "404_not_found": {
        "root_cause": (
            "The databrief_link URL no longer resolves — the document may have been "
            "deleted, moved to Trash, or the URL in BigQuery is malformed."
        ),
        "severity": "MEDIUM",
        "steps": [
            "Open each affected databrief_link in a browser to confirm it is gone.",
            "Contact the campaign owner to provide a replacement link.",
            "Update the databrief_link value in "
            "wb-tian-pr-d0dbe6.wb_tian_pr_dataset.campaign_knowledge.",
            "Re-run: python -m knowledge_base.vibe_octo_knowledge --full-refresh",
        ],
    },
    "timeout": {
        "root_cause": (
            "The HTTP request timed out — either the corporate proxy is not running, "
            "or the Google endpoint did not respond within the configured timeout."
        ),
        "severity": "LOW",
        "steps": [
            "Verify the corporate proxy is running: `curl -I --proxy http://127.0.0.1:14888 "
            "https://www.google.com`",
            "Check HTTPS_PROXY env var: should be http://127.0.0.1:14888",
            "Increase timeout: set BRIEF_FETCH_TIMEOUT=120 before running.",
            "Re-run: python -m knowledge_base.vibe_octo_knowledge --diagnose-briefs",
        ],
    },
    "5xx_server_error": {
        "root_cause": (
            "Google API returned a 5xx server error. This is typically transient "
            "and resolves on its own within minutes."
        ),
        "severity": "LOW",
        "steps": [
            "Wait 5-10 minutes and retry.",
            "Check Google Workspace Status Dashboard: https://www.google.com/appsstatus",
            "Re-run: python -m knowledge_base.vibe_octo_knowledge --diagnose-briefs",
        ],
    },
    "no_url": {
        "root_cause": (
            "The campaign row in BigQuery has no databrief_link value. "
            "No fetch was attempted."
        ),
        "severity": "MEDIUM",
        "steps": [
            "Review the affected_campaigns list to identify which campaigns lack links.",
            "Contact campaign owners to provide a databrief_link for each campaign.",
            "Update the databrief_link column in campaign_knowledge.",
            "Re-run: python -m knowledge_base.vibe_octo_knowledge --full-refresh",
        ],
    },
    "other": {
        "root_cause": (
            "An unexpected error occurred. Inspect the error_message field in "
            "ingestion_log.json for details."
        ),
        "severity": "LOW",
        "steps": [
            "Review error_message in ingestion_log.json for each affected campaign.",
            "Check network connectivity and proxy settings.",
            "Re-run: python -m knowledge_base.vibe_octo_knowledge --diagnose-briefs",
        ],
    },
}

# ---------------------------------------------------------------------------
# Error classification helpers
# ---------------------------------------------------------------------------

def _extract_http_code(msg: str) -> Optional[int]:
    """Parse an HTTP status code out of an exception message string."""
    for pattern in (
        r"\bHTTP[^\d]*(\d{3})\b",
        r"\bstatus[^\d]*(\d{3})\b",
        r"\b(\d{3})\b",
    ):
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


# ---------------------------------------------------------------------------
# Pattern extraction helpers (keyword-based, no LLM dependency)
# ---------------------------------------------------------------------------

_EXCLUSION_KEYWORDS = [
    "exclude", "exclusion", "exclusions", "exclude list", "dnc", "do not contact",
    "epp", "opt-out", "opted out", "opted-out", "suppression", "suppress",
    "unsubscribe", "unsubscribed", "hard bounce", "hard-bounce", "deceased",
]

_PERSONALIZATION_PATTERNS = [
    re.compile(r"\{(\w+)\}"),
    re.compile(r"\[\[(\w+)\]\]"),
    re.compile(r"<%=?\s*(\w+)\s*%>"),
    re.compile(r"\$\{(\w+)\}"),
]

_LIFT_KEYWORDS = [
    "lift", "incremental", "revenue", "subscribers", "upsell", "upgrade",
    "cross-sell", "cross sell", "conversion", "retention", "churn reduction",
    "win-back", "winback", "reactivation", "arpu", "average revenue",
    "net adds", "attach rate",
]


def _extract_patterns(campaigns: list[dict], briefs: dict[str, str]) -> dict:
    """Derive cross-campaign patterns from brief texts using keyword analysis."""
    all_text_lower = " ".join(briefs.values()).lower()

    exclusions_found = [kw for kw in _EXCLUSION_KEYWORDS if kw in all_text_lower]

    pers_fields: set[str] = set()
    for text in briefs.values():
        for pat in _PERSONALIZATION_PATTERNS:
            for m in pat.finditer(text):
                field = m.group(1).lower()
                if 3 <= len(field) <= 40:  # sanity guard
                    pers_fields.add(field)

    lifts_found = [kw for kw in _LIFT_KEYWORDS if kw in all_text_lower]

    # Frequency distribution of medium / cadence across campaigns
    medium_counts: dict[str, int] = defaultdict(int)
    cadence_counts: dict[str, int] = defaultdict(int)
    for c in campaigns:
        if c.get("medium"):
            medium_counts[str(c["medium"]).strip().lower()] += 1
        if c.get("cadence"):
            cadence_counts[str(c["cadence"]).strip().lower()] += 1

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "total_briefs_analyzed": sum(1 for v in briefs.values() if v),
        "common_exclusions": exclusions_found,
        "personalization_fields": sorted(pers_fields),
        "lift_targets": lifts_found,
        "medium_distribution": dict(
            sorted(medium_counts.items(), key=lambda x: -x[1])[:20]
        ),
        "cadence_distribution": dict(
            sorted(cadence_counts.items(), key=lambda x: -x[1])[:20]
        ),
    }


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------

def _classify_tier(targeting_summary: str, segment_summary: str, brief_text: str) -> str:
    """Return GOLD, SILVER, or BRONZE based on data richness."""
    has_targeting = bool(targeting_summary and targeting_summary.strip())
    has_segment = bool(segment_summary and segment_summary.strip())
    has_brief = bool(brief_text and brief_text.strip())

    if has_targeting and has_segment:
        return "GOLD"
    if has_brief:
        return "SILVER"
    return "BRONZE"


# ---------------------------------------------------------------------------
# Atomic JSON write helper
# ---------------------------------------------------------------------------

def _write_atomic(path: Path, data: dict | list) -> None:
    """Serialize data to a .tmp file then os.replace() to final path."""
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        suffix=".tmp",
        prefix=path.stem + "_",
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
# Main class
# ---------------------------------------------------------------------------

class VibeOctoKnowledge:
    """Weekly ingestion agent for campaign knowledge and Adobe warehouse schema."""

    def __init__(self, dry_run: bool = False, brief_timeout: int = DEFAULT_BRIEF_TIMEOUT):
        self.dry_run = dry_run
        self._brief_timeout = brief_timeout
        self._artifacts_dir = ARTIFACTS_DIR

        if not dry_run:
            self._artifacts_dir.mkdir(parents=True, exist_ok=True)

        # Two separate BQ clients — one per GCP project (ADC auth)
        self._campaign_bq = bigquery.Client(project=CAMPAIGN_PROJECT)
        self._adobe_bq = bigquery.Client(project=ADOBE_PROJECT)

        # Brief fetcher (reuses Nexus BriefFetcher with full multi-tier logic)
        self._brief_fetcher = BriefFetcher(timeout=brief_timeout)

        # Column role map — resolved from INFORMATION_SCHEMA on first use
        self._col_map: Optional[dict[str, str]] = None

    # ------------------------------------------------------------------
    # Public commands
    # ------------------------------------------------------------------

    def run_full_refresh(self) -> None:
        """Phase 1 + Phase 2 + Phase 3. Writes all 6 artifacts."""
        print("\n=== VIBE OCTO KNOWLEDGE — FULL REFRESH ===")
        run_at = datetime.now(tz=timezone.utc).isoformat()

        campaigns, briefs, fetch_log, failures = self._phase1_ingest()
        adobe_schema = self._phase2_ingest_schema()
        self._phase3_write_artifacts(campaigns, briefs, fetch_log, failures, adobe_schema, run_at)

        print("\n=== FULL REFRESH COMPLETE ===\n")

    def run_diagnose_briefs(self) -> None:
        """Phase 1 only. Writes ingestion_log.json + ingestion_report.json."""
        print("\n=== VIBE OCTO KNOWLEDGE — DIAGNOSE BRIEFS ===")
        run_at = datetime.now(tz=timezone.utc).isoformat()

        campaigns, briefs, fetch_log, failures = self._phase1_ingest()

        report = self._build_report(
            campaigns=campaigns,
            failures=failures,
            fetch_log=fetch_log,
            adobe_schema=None,
            patterns=None,
            run_at=run_at,
            mode="diagnose",
        )

        if not self.dry_run:
            _write_atomic(self._artifacts_dir / "ingestion_log.json", fetch_log)
            _write_atomic(self._artifacts_dir / "ingestion_report.json", report)
            print(f"\n[WRITTEN] ingestion_log.json    — {len(fetch_log)} entries")
            print(f"[WRITTEN] ingestion_report.json — diagnostics complete")
        else:
            print("\n[DRY-RUN] ingestion_log.json and ingestion_report.json not written")

        self._print_report_summary(report)
        print("\n=== DIAGNOSE BRIEFS COMPLETE ===\n")

    def run_validate(self) -> None:
        """Verify all 6 artifacts exist and contain valid JSON."""
        print("\n=== VIBE OCTO KNOWLEDGE — VALIDATE ===\n")
        artifacts = [
            "semantic_knowledge_index.json",
            "brief_texts.json",
            "adobe_schema.json",
            "cross_campaign_patterns.json",
            "ingestion_report.json",
            "ingestion_log.json",
        ]
        all_ok = True
        for name in artifacts:
            path = self._artifacts_dir / name
            if not path.exists():
                print(f"  MISSING  {name}")
                all_ok = False
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                size_kb = path.stat().st_size / 1024
                if isinstance(data, dict):
                    keys = len(data)
                    print(f"  OK       {name}  ({size_kb:.1f} KB, {keys} top-level keys)")
                elif isinstance(data, list):
                    print(f"  OK       {name}  ({size_kb:.1f} KB, {len(data)} entries)")
                else:
                    print(f"  OK       {name}  ({size_kb:.1f} KB)")
            except json.JSONDecodeError as exc:
                print(f"  INVALID  {name}  — JSON parse error: {exc}")
                all_ok = False

        print()
        if all_ok:
            print("All artifacts are valid.")
        else:
            print("One or more artifacts are missing or invalid. Run --full-refresh.")
        print("\n=== VALIDATE COMPLETE ===\n")

    def run_dry_run(self) -> None:
        """Run all phases without writing any files."""
        print("\n=== VIBE OCTO KNOWLEDGE — DRY RUN ===")
        run_at = datetime.now(tz=timezone.utc).isoformat()

        print("\n[DRY-RUN] Phase 1 — Campaign & Brief Ingestion")
        campaigns, briefs, fetch_log, failures = self._phase1_ingest()

        print("\n[DRY-RUN] Phase 2 — Adobe Schema Ingestion")
        adobe_schema = self._phase2_ingest_schema()

        print("\n[DRY-RUN] Phase 3 — Pattern Extraction (no files written)")
        patterns = _extract_patterns(campaigns, briefs)
        report = self._build_report(campaigns, failures, fetch_log, adobe_schema, patterns, run_at)
        self._print_report_summary(report)

        print("\n[DRY-RUN] No files written.")
        print("\n=== DRY RUN COMPLETE ===\n")

    # ------------------------------------------------------------------
    # Phase 1: Campaign & Brief Ingestion
    # ------------------------------------------------------------------

    def _phase1_ingest(self) -> tuple[list[dict], dict[str, str], list[dict], list[dict]]:
        """Query campaign_knowledge and fetch all databrief links.

        Returns
        -------
        campaigns   : list of normalised campaign dicts (col_map applied)
        briefs      : {camp_id -> brief_text}  ("" when fetch failed or no URL)
        fetch_log   : one entry per campaign (including no-URL rows)
        failures    : subset of fetch_log where success is False
        """
        print("\n[PHASE 1] Campaign & Brief Ingestion")

        # 1. Discover column names
        if self._col_map is None:
            self._col_map = self._resolve_columns()
        cmap = self._col_map

        # 2. Fetch all rows
        print(f"  Querying: {CAMPAIGN_TABLE}")
        raw_rows = self._fetch_campaign_rows()
        total = len(raw_rows)
        print(f"  Rows fetched: {total}")

        # 3. Normalise rows using the column map
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

        # 4. Fetch briefs
        print(f"\n[PHASE 1] Fetching briefs...")
        briefs: dict[str, str] = {}
        fetch_log: list[dict] = []
        failures: list[dict] = []
        success_count = 0

        pad = len(str(total))
        for idx, camp in enumerate(campaigns, 1):
            camp_id = camp["camp_id"]
            url = camp["databrief_link"]
            name = camp["campaign_name"] or camp_id

            text, entry = self._fetch_one_brief(url, camp_id, name, idx, total, pad)
            briefs[camp_id] = text
            fetch_log.append(entry)
            if entry["success"]:
                success_count += 1
            else:
                failures.append(entry)

        fail_count = total - success_count
        fail_pct = (fail_count / total * 100) if total else 0.0

        print(f"\n[PHASE 1 COMPLETE]")
        print(f"  Fetched:  {success_count}")
        print(f"  Failed:   {fail_count} ({fail_pct:.2f}%)")
        if fail_count:
            print(f"  -> See ingestion_report.json for diagnostics")

        return campaigns, briefs, fetch_log, failures

    def _resolve_columns(self) -> dict[str, str]:
        """Query INFORMATION_SCHEMA.COLUMNS to discover actual column names."""
        project = CAMPAIGN_PROJECT
        dataset = "wb_tian_pr_dataset"
        query = (
            f"SELECT column_name "
            f"FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
            f"WHERE table_name = 'campaign_knowledge' "
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
        """SELECT * FROM campaign_knowledge, return as list of dicts."""
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
        """Fetch one brief URL with full diagnostic capture.

        Strategy:
          1. to_flat_string() — multi-tier fallback (Sheets API, auth CSV, anon CSV)
          2. If that returns "", call fetch() explicitly to capture the HTTP error code.

        Returns (text, log_entry). Never raises.
        """
        ts = datetime.now(tz=timezone.utc).isoformat()
        label = f"{campaign_name[:50]:<50}"
        prefix = f"[{idx:>{pad}}/{total}]"

        entry: dict = {
            "seq": idx,
            "camp_id": camp_id,
            "campaign_name": campaign_name,
            "url": url,
            "timestamp": ts,
            "success": False,
            "http_code": None,
            "error_type": None,
            "error_message": None,
            "text_length": 0,
        }

        # --- No URL ---
        if not url or not url.strip():
            entry["error_type"] = "no_url"
            entry["error_message"] = "No databrief_link set for this campaign"
            print(f"{prefix} {label} ... SKIP")
            return "", entry

        url = url.strip()

        # --- Tier 1: to_flat_string() (best-effort, all fallback tiers) ---
        text = self._brief_fetcher.to_flat_string(url)
        if text:
            entry["success"] = True
            entry["text_length"] = len(text)
            print(f"{prefix} {label} ... OK")
            return text, entry

        # --- to_flat_string returned "" — probe for the actual error ---
        try:
            probe = self._brief_fetcher.fetch(url)
            # fetch() succeeded but to_flat_string returned "" (edge case)
            if probe:
                entry["success"] = True
                entry["text_length"] = len(probe)
                print(f"{prefix} {label} ... OK")
                return probe, entry
            # Both returned "" — treat as other error
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
            if "timeout" in msg.lower() or "timed out" in msg.lower():
                entry["error_type"] = "timeout"
            else:
                entry["error_type"] = "other"
            entry["error_message"] = msg[:600]
            print(f"{prefix} {label} ... FAIL ({entry['error_type']})")
            return "", entry

    # ------------------------------------------------------------------
    # Phase 2: Adobe Schema Ingestion
    # ------------------------------------------------------------------

    def _phase2_ingest_schema(self) -> dict:
        """Query bi-srv-hsmdet-pr-7b9def.adobe INFORMATION_SCHEMA.

        Returns a schema dict:  {table_name: {meta: ..., columns: [...]}}
        """
        print("\n[PHASE 2] Adobe Schema Ingestion")
        print(f"  Dataset: {ADOBE_PROJECT}.{ADOBE_DATASET}")

        # Tables
        tables_query = (
            f"SELECT table_name, table_type "
            f"FROM `{ADOBE_PROJECT}.{ADOBE_DATASET}.INFORMATION_SCHEMA.TABLES` "
            f"ORDER BY table_name"
        )
        try:
            tables_rows = list(self._adobe_bq.query(tables_query).result())
        except Exception as exc:
            _log.warning("Adobe INFORMATION_SCHEMA.TABLES query failed: %s", exc)
            print(f"  WARNING: Could not fetch tables — {exc}")
            tables_rows = []

        # Columns
        columns_query = (
            f"SELECT table_name, column_name, data_type, is_nullable, "
            f"ordinal_position, COALESCE(description, '') AS description "
            f"FROM `{ADOBE_PROJECT}.{ADOBE_DATASET}.INFORMATION_SCHEMA.COLUMNS` "
            f"ORDER BY table_name, ordinal_position"
        )
        try:
            columns_rows = list(self._adobe_bq.query(columns_query).result())
        except Exception as exc:
            _log.warning("Adobe INFORMATION_SCHEMA.COLUMNS query failed: %s", exc)
            print(f"  WARNING: Could not fetch columns — {exc}")
            columns_rows = []

        # Build schema dict
        schema: dict[str, dict] = {}
        for row in tables_rows:
            schema[row.table_name] = {
                "table_type": row.table_type,
                "columns": [],
            }

        for row in columns_rows:
            tbl = row.table_name
            if tbl not in schema:
                schema[tbl] = {"table_type": "UNKNOWN", "columns": []}
            schema[tbl]["columns"].append({
                "column_name":  row.column_name,
                "data_type":    row.data_type,
                "is_nullable":  row.is_nullable,
                "ordinal_position": row.ordinal_position,
                "description":  row.description,
            })

        print(f"  Tables:  {len(schema)}")
        total_cols = sum(len(v["columns"]) for v in schema.values())
        print(f"  Columns: {total_cols}")
        print(f"[PHASE 2 COMPLETE]")

        return {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "project": ADOBE_PROJECT,
            "dataset": ADOBE_DATASET,
            "table_count": len(schema),
            "column_count": total_cols,
            "tables": schema,
        }

    # ------------------------------------------------------------------
    # Phase 3: Pattern Extraction & Artifact Generation
    # ------------------------------------------------------------------

    def _phase3_write_artifacts(
        self,
        campaigns: list[dict],
        briefs: dict[str, str],
        fetch_log: list[dict],
        failures: list[dict],
        adobe_schema: dict,
        run_at: str,
    ) -> None:
        """Extract patterns, classify tiers, build and write all 6 artifacts."""
        print("\n[PHASE 3] Pattern Extraction & Artifact Generation")

        # --- Tier classification ---
        gold_count = silver_count = bronze_count = 0
        knowledge_records: list[dict] = []
        for camp in campaigns:
            brief_text = briefs.get(camp["camp_id"], "")
            tier = _classify_tier(
                camp["targeting_summary"],
                camp["segment_summary"],
                brief_text,
            )
            if tier == "GOLD":
                gold_count += 1
            elif tier == "SILVER":
                silver_count += 1
            else:
                bronze_count += 1

            knowledge_records.append({
                **camp,
                "brief_text": brief_text,
                "tier": tier,
                "ingested_at": run_at,
            })

        print(f"  Tiers — GOLD: {gold_count}  SILVER: {silver_count}  BRONZE: {bronze_count}")

        # --- Pattern extraction ---
        patterns = _extract_patterns(campaigns, briefs)

        # --- Build report ---
        report = self._build_report(
            campaigns=campaigns,
            failures=failures,
            fetch_log=fetch_log,
            adobe_schema=adobe_schema,
            patterns=patterns,
            run_at=run_at,
            mode="full",
        )

        if self.dry_run:
            print("[PHASE 3] DRY-RUN — no files written")
            self._print_report_summary(report)
            return

        # --- Write 6 artifacts atomically ---
        artifacts = {
            "semantic_knowledge_index.json": {
                "schema_version": "2.0",
                "generated_at": run_at,
                "total_campaigns": len(knowledge_records),
                "gold_count":   gold_count,
                "silver_count": silver_count,
                "bronze_count": bronze_count,
                "campaigns": knowledge_records,
            },
            "brief_texts.json": {
                "generated_at": run_at,
                "total_briefs": len(briefs),
                "briefs": {k: v for k, v in briefs.items() if v},
            },
            "adobe_schema.json":           adobe_schema,
            "cross_campaign_patterns.json": patterns,
            "ingestion_report.json":       report,
            "ingestion_log.json":          fetch_log,
        }

        for filename, data in artifacts.items():
            path = self._artifacts_dir / filename
            _write_atomic(path, data)
            size_kb = path.stat().st_size / 1024
            print(f"  [WRITTEN] {filename}  ({size_kb:.1f} KB)")

        print(f"\n[PHASE 3 COMPLETE]")
        self._print_report_summary(report)

    # ------------------------------------------------------------------
    # Report builder
    # ------------------------------------------------------------------

    def _build_report(
        self,
        campaigns: list[dict],
        failures: list[dict],
        fetch_log: list[dict],
        adobe_schema: Optional[dict],
        patterns: Optional[dict],
        run_at: str,
        mode: str = "full",
    ) -> dict:
        """Construct ingestion_report.json structure."""
        total_attempted = len(fetch_log)
        total_success = sum(1 for e in fetch_log if e["success"])
        total_failed = total_attempted - total_success
        fail_pct = round(total_failed / total_attempted * 100, 4) if total_attempted else 0.0

        # Group failures by error type
        by_type: dict[str, list[dict]] = defaultdict(list)
        for entry in failures:
            etype = entry.get("error_type") or "other"
            by_type[etype].append(entry)

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

        report: dict = {
            "schema_version": "2.0",
            "generated_at":   run_at,
            "mode":           mode,
            "summary": {
                "total_campaigns":          len(campaigns),
                "total_briefs_attempted":   total_attempted,
                "briefs_fetched_successfully": total_success,
                "briefs_failed":            total_failed,
                "failure_rate_percent":     fail_pct,
            },
            "brief_access_diagnostics": {
                "total_briefs_attempted":      total_attempted,
                "briefs_fetched_successfully": total_success,
                "briefs_failed":               total_failed,
                "failure_rate_percent":        fail_pct,
                "failures_by_error_type":      failures_by_error_type,
            },
        }

        if mode == "full":
            report["schema_ingestion"] = {
                "adobe_project": ADOBE_PROJECT,
                "adobe_dataset": ADOBE_DATASET,
                "table_count":   adobe_schema.get("table_count", 0) if adobe_schema else 0,
                "column_count":  adobe_schema.get("column_count", 0) if adobe_schema else 0,
                "ingested_at":   run_at,
            } if adobe_schema else None

            if patterns:
                report["pattern_extraction"] = {
                    "briefs_analyzed":      patterns.get("total_briefs_analyzed", 0),
                    "exclusion_types_found": len(patterns.get("common_exclusions", [])),
                    "personalization_fields_found": len(patterns.get("personalization_fields", [])),
                    "lift_target_types_found": len(patterns.get("lift_targets", [])),
                }

        return report

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _print_report_summary(self, report: dict) -> None:
        diag = report.get("brief_access_diagnostics", {})
        print(f"\n--- Brief Access Diagnostics ---")
        print(f"  Total briefs attempted:   {diag.get('total_briefs_attempted', 0)}")
        print(f"  Fetched successfully:     {diag.get('briefs_fetched_successfully', 0)}")
        print(f"  Failed:                   {diag.get('briefs_failed', 0)}  "
              f"({diag.get('failure_rate_percent', 0):.2f}%)")

        by_type = diag.get("failures_by_error_type", {})
        if by_type:
            print(f"\n  Failure breakdown:")
            for etype, info in by_type.items():
                sev = info.get("remediation", {}).get("severity", "")
                print(f"    [{sev:<6}] {etype}: {info['count']} campaign(s)")
            print(f"\n  -> See ingestion_report.json for remediation steps per error type.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m knowledge_base.vibe_octo_knowledge",
        description="Vibe OCTO Knowledge — Weekly Ingestion Agent",
    )
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--full-refresh",
        action="store_true",
        help="Complete ingestion: campaigns + briefs + schema. Writes all 6 artifacts.",
    )
    group.add_argument(
        "--diagnose-briefs",
        action="store_true",
        help="Fetch all briefs and report diagnostics. Writes ingestion_log + ingestion_report.",
    )
    group.add_argument(
        "--validate",
        action="store_true",
        help="Verify all 6 artifact files exist and contain valid JSON.",
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
    """Load .env from known locations (Nexus dir, parent dir, cwd) if dotenv is available."""
    try:
        from dotenv import load_dotenv  # type: ignore[import]
    except ImportError:
        return
    # Try in order: Nexus dir (proxy config lives there), repo root, cwd
    candidates = [
        _NEXUS_DIR / ".env",
        Path(__file__).resolve().parent.parent / ".env",
        Path.cwd() / ".env",
    ]
    for path in candidates:
        if path.exists():
            load_dotenv(str(path), override=False)  # don't clobber already-set vars


def main() -> None:
    _load_env()
    parser = _build_parser()
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    agent = VibeOctoKnowledge(
        dry_run=args.dry_run,
        brief_timeout=args.timeout,
    )

    if args.full_refresh:
        agent.run_full_refresh()
    elif args.diagnose_briefs:
        agent.run_diagnose_briefs()
    elif args.validate:
        agent.run_validate()
    elif args.dry_run:
        agent.run_dry_run()


if __name__ == "__main__":
    main()
