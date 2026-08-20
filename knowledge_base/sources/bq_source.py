"""knowledge_base/sources/bq_source.py — BigQuery campaign deployment source."""
from __future__ import annotations

import logging
from typing import Optional

from google.cloud import bigquery

from ..config import CAMPAIGN_PROJECT as _CAMPAIGN_PROJECT, CAMPAIGN_TABLE as _CAMPAIGN_TABLE
from .base_connector import KnowledgeSourceConnector

_log = logging.getLogger(__name__)


class BigQueryCampaignSource(KnowledgeSourceConnector):
    """Reads campaign deployment rows from BigQuery."""

    def __init__(self, table: str = _CAMPAIGN_TABLE, project: str = _CAMPAIGN_PROJECT):
        self._table = table
        self._project = project
        self._client: Optional[bigquery.Client] = None

    def _get_client(self) -> bigquery.Client:
        if self._client is None:
            self._client = bigquery.Client(project=self._project)
        return self._client

    def fetch(self) -> list[dict]:
        try:
            result = self._get_client().query(f"SELECT * FROM `{self._table}`").result()
            return [dict(row) for row in result]
        except Exception as exc:
            _log.error("BigQuery fetch failed: %s", exc)
            return []

    def extract_insights(self, docs: list[dict]) -> dict:
        return {
            "total_rows": len(docs),
            "camp_ids": sorted({str(d.get("camp_id", "")) for d in docs if d.get("camp_id")}),
        }
