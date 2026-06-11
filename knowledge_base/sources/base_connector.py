"""knowledge_base/sources/base_connector.py — Abstract connector interface.

Every data source for the knowledge layer implements this two-method contract.
Zero changes to the ingestion pipeline are needed when adding a new source.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class KnowledgeSourceConnector(ABC):
    """Abstract base for all knowledge data source connectors."""

    @abstractmethod
    def fetch(self) -> list[dict]:
        """Retrieve raw documents from the source.  Never raises — return [] on error."""

    @abstractmethod
    def extract_insights(self, docs: list[dict]) -> dict:
        """Derive structured insights from the fetched documents."""
