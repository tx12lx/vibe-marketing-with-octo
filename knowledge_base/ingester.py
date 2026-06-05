"""knowledge_base/ingester.py — Pillar 1: Binary Knowledge Base & Atomic Refresh Pipeline.

Isolation contract: this module imports ONLY google.cloud.bigquery, BriefFetcher
from the Nexus core package, and the Python standard library.  It never imports
from nexus_agent, quant_agent, or vibe_orchestrator.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from google.cloud import bigquery

# Locate and register the Nexus package root so BriefFetcher is importable
# without touching nexus_agent, quant_agent, or vibe_orchestrator.
_NEXUS_DIR = Path(__file__).resolve().parent.parent / "Vibe OCTO Nexus"
if str(_NEXUS_DIR) not in sys.path:
    sys.path.insert(0, str(_NEXUS_DIR))

from core.brief_fetcher import BriefFetcher  # noqa: E402

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Heuristic role patterns — maps logical roles to candidate column name substrings.
# Discovery query runs first; these patterns are only applied after actual column
# names are returned from INFORMATION_SCHEMA.
# ---------------------------------------------------------------------------
_ROLE_PATTERNS: dict[str, list[str]] = {
    "camp_id":           ["camp_id", "campaign_id", "camp_code"],
    "sub_camp_id":       ["sub_camp_id", "sub_campaign_id", "sub_camp_code"],
    "campaign_name":     ["campaign_name", "campaign", "name"],
    "targeting_summary": ["targeting_summary", "targeting_summ", "target_summary"],
    "segment_summary":   ["segment_summary", "seg_summary"],
    "databrief_link":    ["databrief_link", "brief_link", "brief_url", "data_brief_link"],
    "cadence":           ["cadence"],
    "medium":            ["medium"],
    "campaign_purpose":  ["campaign_purpose", "purpose"],
    "primary_products":  ["primary_products", "products"],
}


# ---------------------------------------------------------------------------
# Dataclasses — shared with tier_index via package import
# ---------------------------------------------------------------------------

@dataclass
class GoldCampaignRecord:
    camp_id: str
    sub_camp_id: str
    campaign_name: str
    targeting_summary: str
    segment_summary: str
    brief_text: str
    cadence: str
    medium: str
    campaign_purpose: str
    primary_products: str
    source: str               # "bq_metadata" or "verified_registry"
    bias_weight: float = 1.0
    ingested_at: str = ""


@dataclass
class CampaignMetadataRecord:
    camp_id: str
    sub_camp_id: str
    campaign_name: str
    cadence: str
    medium: str
    campaign_purpose: str
    primary_products: str
    databrief_link: str
    tier: str = "BRONZE"


@dataclass
class IngestionSummary:
    total_rows: int
    gold_count: int
    bronze_count: int
    fetch_errors: list[str]   # one entry per failed databrief_link fetch
    run_at: str               # ISO 8601


# ---------------------------------------------------------------------------
# Ingester
# ---------------------------------------------------------------------------

class KnowledgeBaseIngester:
    def __init__(self, config_path: str | Path) -> None:
        """Load ingestion_config.json. Instantiates BQ ADC client and BriefFetcher."""
        self._config_path = Path(config_path)
        self._config: dict = json.loads(self._config_path.read_text(encoding="utf-8"))
        self._bq_client = bigquery.Client(
            project=self._config["source"]["bq_project"]
        )
        brief_timeout: int = self._config.get("brief_fetch", {}).get("timeout_seconds", 60)
        self._brief_fetcher = BriefFetcher(timeout=brief_timeout)
        self._column_map: Optional[dict[str, str]] = None  # role -> actual column name
        self._root: Path = self._config_path.parent
        self._last_fetch_errors: list[str] = []

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run_full_refresh(self) -> IngestionSummary:
        """Execute the complete atomic clean-slate refresh cycle.

        Steps:
          1. _fetch_bq_rows()     — read campaign_knowledge (read-only)
          2. _fetch_brief_texts() — BriefFetcher.to_flat_string() per databrief_link
          3. _load_registry()     — read verified_app_registry.json
          4. _classify_and_merge()
          5. _write_atomic()      — os.replace() swap of semantic_knowledge_index.json
        Returns IngestionSummary.
        """
        run_at = datetime.now(tz=timezone.utc).isoformat()
        rows = self._fetch_bq_rows()
        brief_texts = self._fetch_brief_texts(rows)
        fetch_errors = list(self._last_fetch_errors)
        registry = self._load_registry()
        gold, bronze = self._classify_and_merge(rows, brief_texts, registry)
        self._write_atomic(gold, bronze, run_at)
        return IngestionSummary(
            total_rows=len(rows),
            gold_count=len(gold),
            bronze_count=len(bronze),
            fetch_errors=fetch_errors,
            run_at=run_at,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_columns(self) -> dict[str, str]:
        """Query INFORMATION_SCHEMA to map logical roles to actual column names.

        No column names are hardcoded before this discovery query runs.
        """
        bq_table = self._config["source"]["bq_table"]
        parts = bq_table.split(".")
        project, dataset = parts[0], parts[1]

        query = (
            f"SELECT column_name "
            f"FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
            f"WHERE table_name = 'campaign_knowledge' "
            f"ORDER BY ordinal_position"
        )
        result = self._bq_client.query(query).result()
        actual_columns = [row.column_name.lower() for row in result]

        role_map: dict[str, str] = {}
        for role, patterns in _ROLE_PATTERNS.items():
            for col in actual_columns:
                if any(col == pat or pat in col for pat in patterns):
                    role_map[role] = col
                    break

        _log.debug("Resolved column map: %s", role_map)
        return role_map

    def _fetch_bq_rows(self) -> list[dict]:
        """Query campaign_knowledge. Column names resolved dynamically from
        INFORMATION_SCHEMA.COLUMNS at first call, then cached for the session."""
        if self._column_map is None:
            self._column_map = self._resolve_columns()

        bq_table = self._config["source"]["bq_table"]
        query = f"SELECT * FROM `{bq_table}`"
        result = self._bq_client.query(query).result()
        return [dict(row) for row in result]

    def _fetch_brief_texts(self, rows: list[dict]) -> dict[str, str]:
        """Map {databrief_link -> flat text} for all rows with a URL.
        On fetch error: stores "" for that key (never raises)."""
        self._last_fetch_errors = []
        col = (self._column_map or {}).get("databrief_link")
        texts: dict[str, str] = {}
        if not col:
            return texts

        unique_urls: set[str] = set()
        for row in rows:
            raw = row.get(col)
            if raw:
                url = str(raw).strip()
                if url:
                    unique_urls.add(url)

        for url in unique_urls:
            text = self._brief_fetcher.to_flat_string(url)
            texts[url] = text
            if not text:
                self._last_fetch_errors.append(url)

        return texts

    def _load_registry(self) -> list[dict]:
        """Read verified_app_registry.json. Returns [] if file absent."""
        registry_path = self._root / self._config["output_files"]["verified_registry"]
        if not registry_path.exists():
            return []
        try:
            data = json.loads(registry_path.read_text(encoding="utf-8"))
            return data.get("records", [])
        except Exception as exc:
            _log.warning("Failed to read registry at %s: %s", registry_path, exc)
            return []

    def _classify_and_merge(
        self,
        rows: list[dict],
        brief_texts: dict[str, str],
        registry: list[dict],
    ) -> tuple[list[GoldCampaignRecord], list[CampaignMetadataRecord]]:
        """GOLD if BQ row has non-empty targeting+segment summary fields
        OR a registry entry with hitl_confirmed=true exists for that campaign key.
        BRONZE otherwise."""
        cmap = self._column_map or {}
        treat_empty_as_null: bool = self._config.get(
            "gold_classification", {}
        ).get("empty_strings_treated_as_null", True)

        # Build confirmed registry lookup keyed by "{camp_id}::{sub_camp_id}"
        confirmed: dict[str, dict] = {}
        for entry in registry:
            if entry.get("hitl_confirmed"):
                key = f"{entry.get('camp_id', '')}::{entry.get('sub_camp_id', '')}"
                confirmed[key] = entry

        gold: list[GoldCampaignRecord] = []
        bronze: list[CampaignMetadataRecord] = []

        def _get(row: dict, role: str) -> str:
            col = cmap.get(role)
            if col is None:
                return ""
            val = row.get(col)
            return str(val).strip() if val is not None else ""

        def _non_empty(s: str) -> bool:
            return bool(s.strip()) if treat_empty_as_null else bool(s)

        for row in rows:
            camp_id = _get(row, "camp_id")
            sub_camp_id = _get(row, "sub_camp_id")
            campaign_name = _get(row, "campaign_name")
            targeting_summary = _get(row, "targeting_summary")
            segment_summary = _get(row, "segment_summary")
            databrief_link = _get(row, "databrief_link")
            cadence = _get(row, "cadence")
            medium = _get(row, "medium")
            campaign_purpose = _get(row, "campaign_purpose")
            primary_products = _get(row, "primary_products")

            registry_key = f"{camp_id}::{sub_camp_id}"
            reg_entry = confirmed.get(registry_key)

            has_bq_summaries = _non_empty(targeting_summary) and _non_empty(segment_summary)
            has_registry_confirmation = reg_entry is not None

            if has_bq_summaries or has_registry_confirmation:
                if reg_entry:
                    targeting_summary = reg_entry.get("targeting_summary") or targeting_summary
                    segment_summary = reg_entry.get("segment_summary") or segment_summary
                    source = "verified_registry"
                else:
                    source = "bq_metadata"

                brief_text = brief_texts.get(databrief_link, "") if databrief_link else ""
                gold.append(GoldCampaignRecord(
                    camp_id=camp_id,
                    sub_camp_id=sub_camp_id,
                    campaign_name=campaign_name,
                    targeting_summary=targeting_summary,
                    segment_summary=segment_summary,
                    brief_text=brief_text,
                    cadence=cadence,
                    medium=medium,
                    campaign_purpose=campaign_purpose,
                    primary_products=primary_products,
                    source=source,
                ))
            else:
                bronze.append(CampaignMetadataRecord(
                    camp_id=camp_id,
                    sub_camp_id=sub_camp_id,
                    campaign_name=campaign_name,
                    cadence=cadence,
                    medium=medium,
                    campaign_purpose=campaign_purpose,
                    primary_products=primary_products,
                    databrief_link=databrief_link,
                ))

        return gold, bronze

    def _write_atomic(
        self,
        gold: list[GoldCampaignRecord],
        bronze: list[CampaignMetadataRecord],
        run_at: str,
    ) -> None:
        """Serialize to JSON, write to .tmp file, then os.replace() to final path."""
        index_path = self._root / self._config["output_files"]["knowledge_index"]

        for rec in gold:
            rec.ingested_at = run_at

        payload = {
            "schema_version": "1.0",
            "generated_at": run_at,
            "gold_count": len(gold),
            "bronze_count": len(bronze),
            "gold_records": [asdict(r) for r in gold],
            "bronze_records": [asdict(r) for r in bronze],
        }

        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(index_path.parent),
            suffix=".tmp",
            prefix="semantic_knowledge_index_",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
            os.replace(tmp_path, str(index_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
