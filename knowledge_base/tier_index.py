"""knowledge_base/tier_index.py — In-memory GoldTierIndex.

Loads the compiled semantic_knowledge_index.json and provides O(1) lookup,
in-memory promotion, and bias deprioritisation.  All mutations are session-only;
persistent changes happen exclusively on the next run_full_refresh() call.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .ingester import GoldCampaignRecord

_log = logging.getLogger(__name__)


class GoldTierIndex:
    """In-memory dictionary of GoldCampaignRecord keyed by '{camp_id}::{sub_camp_id}'."""

    def __init__(self) -> None:
        self._index: dict[str, GoldCampaignRecord] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def load_from_file(self, index_path: Path) -> None:
        """Parse semantic_knowledge_index.json and populate the internal dict."""
        self._index.clear()
        try:
            data = json.loads(Path(index_path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            _log.warning("semantic_knowledge_index.json not found at %s — index empty", index_path)
            return
        except Exception as exc:
            _log.warning("Failed to load knowledge index from %s: %s", index_path, exc)
            return

        for raw in data.get("gold_records", []):
            try:
                rec = GoldCampaignRecord(
                    camp_id=raw.get("camp_id", ""),
                    sub_camp_id=raw.get("sub_camp_id", ""),
                    campaign_name=raw.get("campaign_name", ""),
                    targeting_summary=raw.get("targeting_summary", ""),
                    segment_summary=raw.get("segment_summary", ""),
                    brief_text=raw.get("brief_text", ""),
                    cadence=raw.get("cadence", ""),
                    medium=raw.get("medium", ""),
                    campaign_purpose=raw.get("campaign_purpose", ""),
                    primary_products=raw.get("primary_products", ""),
                    source=raw.get("source", "bq_metadata"),
                    bias_weight=float(raw.get("bias_weight", 1.0)),
                    ingested_at=raw.get("ingested_at", ""),
                )
                key = f"{rec.camp_id}::{rec.sub_camp_id}"
                self._index[key] = rec
            except Exception as exc:
                _log.warning("Skipping malformed gold record %s: %s", raw, exc)

        _log.debug("GoldTierIndex loaded %d records from %s", len(self._index), index_path)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def lookup(self, camp_id: str, sub_camp_id: str) -> Optional[GoldCampaignRecord]:
        """Return the record for '{camp_id}::{sub_camp_id}', or None if not found."""
        return self._index.get(f"{camp_id}::{sub_camp_id}")

    # ------------------------------------------------------------------
    # Session-only mutations (never write to disk)
    # ------------------------------------------------------------------

    def promote_in_memory(
        self,
        camp_id: str,
        sub_camp_id: str,
        targeting_summary: str,
        segment_summary: str,
        source: str = "verified_registry",
    ) -> None:
        """Add or update a record in-memory for the current session.

        Does NOT write to any file.  The persistent promotion happens
        on the next run_full_refresh() call.
        """
        key = f"{camp_id}::{sub_camp_id}"
        existing = self._index.get(key)
        if existing is not None:
            existing.targeting_summary = targeting_summary
            existing.segment_summary = segment_summary
            existing.source = source
        else:
            self._index[key] = GoldCampaignRecord(
                camp_id=camp_id,
                sub_camp_id=sub_camp_id,
                campaign_name="",
                targeting_summary=targeting_summary,
                segment_summary=segment_summary,
                brief_text="",
                cadence="",
                medium="",
                campaign_purpose="",
                primary_products="",
                source=source,
            )

    def deprioritize(self, camp_id: str) -> None:
        """Reduce bias_weight for all records matching camp_id by 0.2, floor at 0.2.
        Applied in-memory only."""
        for key, rec in self._index.items():
            if rec.camp_id == camp_id:
                rec.bias_weight = max(0.2, round(rec.bias_weight - 0.2, 6))

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def all_keys(self) -> list[str]:
        """Return sorted list of all camp_id::sub_camp_id keys."""
        return sorted(self._index.keys())
