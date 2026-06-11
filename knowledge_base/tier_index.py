"""knowledge_base/tier_index.py — In-memory GoldTierIndex.

Loads the compiled semantic_knowledge_index.json and provides O(1) lookup,
in-memory promotion, and bias deprioritisation.  All mutations are session-only;
persistent changes happen exclusively on the next run_full_refresh() call.
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


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
    tier: str = "GOLD"
    bias_weight: float = 1.0
    ingested_at: str = ""
    last_ingested_at: str = ""
    brief_extraction: Optional[dict] = None   # populated by --refresh-briefs
    conflict_notes: list[str] = field(default_factory=list)
    brief_fetched_from: str = ""
    brief_fetched_at: str = ""

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

        for raw in data.get("campaigns", []):
            try:
                # v3/v4 schema nests targeting/segment under acc_summaries; fall back to
                # flat fields for compatibility with older artifacts.
                acc = raw.get("acc_summaries") or {}
                rec = GoldCampaignRecord(
                    camp_id=raw.get("camp_id", ""),
                    sub_camp_id=raw.get("sub_camp_id", ""),
                    campaign_name=raw.get("campaign_name", ""),
                    targeting_summary=acc.get("targeting_summary") or raw.get("targeting_summary", ""),
                    segment_summary=acc.get("segment_summary") or raw.get("segment_summary", ""),
                    brief_text="",
                    cadence=raw.get("cadence", ""),
                    medium=raw.get("medium", ""),
                    campaign_purpose=raw.get("campaign_purpose", ""),
                    primary_products=raw.get("primary_products", ""),
                    source=raw.get("source", "bq_metadata"),
                    tier=raw.get("tier", "GOLD"),
                    bias_weight=float(raw.get("bias_weight", 1.0)),
                    ingested_at=raw.get("ingested_at", ""),
                    last_ingested_at=raw.get("last_ingested_at", raw.get("ingested_at", "")),
                    brief_extraction=raw.get("brief_extraction"),
                    conflict_notes=raw.get("conflict_notes") or [],
                    brief_fetched_from=raw.get("brief_fetched_from", ""),
                    brief_fetched_at=raw.get("brief_fetched_at", ""),
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

    def retrieve_similar(
        self,
        query: str,
        top_k: int = 3,
        db_path: Optional[Path] = None,
    ) -> list[GoldCampaignRecord]:
        """Return top-k records by cosine similarity against query text.

        Tries the SQLite embeddings database first (populated by ingestion).
        Falls back to in-memory keyword scoring when the DB is absent.
        """
        if not self._index:
            return []

        if db_path and db_path.exists():
            try:
                return self._retrieve_from_db(query, top_k, db_path)
            except Exception as exc:
                _log.debug("SQLite retrieval failed, falling back to keyword: %s", exc)

        return self._retrieve_keyword(query, top_k)

    def _retrieve_from_db(
        self, query: str, top_k: int, db_path: Path
    ) -> list[GoldCampaignRecord]:
        import sqlite3
        q_vec = _tfidf_vector(query)
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT campaign_key, embedding_json FROM campaign_embeddings"
            ).fetchall()
        finally:
            conn.close()

        scored: list[tuple[float, str]] = []
        for campaign_key, emb_json in rows:
            if campaign_key not in self._index:
                continue
            try:
                c_vec: dict[str, float] = json.loads(emb_json)
                sim = _cosine_sim(q_vec, c_vec)
                scored.append((sim, campaign_key))
            except Exception:
                continue

        scored.sort(key=lambda x: -x[0])
        return [
            self._index[key] for _, key in scored[:top_k] if key in self._index
        ]

    def _retrieve_keyword(self, query: str, top_k: int) -> list[GoldCampaignRecord]:
        """Fallback: rank by keyword overlap between query and campaign text."""
        q_tokens = set(_tokenize(query))
        scored: list[tuple[int, str]] = []
        for key, rec in self._index.items():
            be = rec.brief_extraction or {}
            text = " ".join(filter(None, [
                rec.campaign_name,
                rec.targeting_summary,
                rec.segment_summary,
                rec.campaign_purpose,
                be.get("campaign_strategy_summary", ""),
                " ".join(be.get("exclusion_rules") or []),
            ]))
            overlap = len(q_tokens & set(_tokenize(text)))
            scored.append((overlap, key))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [self._index[key] for _, key in scored[:top_k]]


# ---------------------------------------------------------------------------
# Sparse TF-IDF helpers (pure Python, no numpy dependency)
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z]{3,}", text.lower())


def _tfidf_vector(text: str) -> dict[str, float]:
    tokens = _tokenize(text)
    if not tokens:
        return {}
    counts = Counter(tokens)
    total = len(tokens)
    return {t: count / total for t, count in counts.items()}


def _cosine_sim(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a.get(t, 0.0) * w for t, w in b.items())
    mag_a = math.sqrt(sum(v * v for v in a.values()))
    mag_b = math.sqrt(sum(v * v for v in b.values()))
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)
