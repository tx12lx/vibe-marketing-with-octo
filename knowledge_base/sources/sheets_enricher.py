"""knowledge_base/sources/sheets_enricher.py — Google Sheets brief enricher.

Fetches data briefs from Google Sheets URLs with mandatory rate limiting.
Only used from `--refresh-briefs` mode — never called during --full-refresh.

Rate limit: minimum 5 seconds between each document fetch (enforced in code,
not configurable).  This prevents triggering Google Workspace mass-download
security alerts.
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

_MIN_FETCH_DELAY_SECONDS = 5


class SheetsEnricher:
    """Fetches and caches brief text from Google Sheets URLs.

    Implements a mandatory 5-second inter-fetch delay and requires explicit
    confirmation before any batch fetch is started.
    """

    def __init__(self, brief_fetcher: object, timeout: int = 60) -> None:
        self._fetcher = brief_fetcher
        self._timeout = timeout

    def confirm_and_fetch(
        self,
        campaigns: list[dict],
        camp_id_filter: Optional[str] = None,
    ) -> list[tuple[dict, str, bool]]:
        """Interactive confirmation + rate-limited fetch for a list of campaigns.

        Args:
            campaigns: list of campaign dicts with 'camp_id' and 'databrief_link' fields
            camp_id_filter: if set, only fetch this specific camp_id

        Returns:
            list of (campaign_dict, brief_text, success) tuples
        """
        targets = [
            c for c in campaigns
            if (not camp_id_filter or c.get("camp_id", "").upper() == camp_id_filter.upper())
            and c.get("databrief_link", "").strip()
        ]

        if not targets:
            print("  No campaigns with databrief_link URLs to fetch.")
            return []

        print(f"\n  About to fetch {len(targets)} Google Sheets document(s):")
        for c in targets:
            print(f"    - {c.get('camp_id', '?')}: {c.get('databrief_link', '')[:80]}")

        print(f"\n  Rate limit: 1 document every {_MIN_FETCH_DELAY_SECONDS}s")
        print(f"  Estimated time: ~{len(targets) * _MIN_FETCH_DELAY_SECONDS}s")
        print()

        try:
            answer = input("  Proceed? [Y/N]: ").strip().upper()
        except EOFError:
            answer = "N"

        if answer != "Y":
            print("  Fetch cancelled.")
            return []

        results: list[tuple[dict, str, bool]] = []
        for idx, camp in enumerate(targets, 1):
            name = camp.get("campaign_name", camp.get("camp_id", "?"))
            url = camp.get("databrief_link", "")
            print(f"\n  [{idx}/{len(targets)}] Fetching brief: {name[:60]}")

            if idx > 1:
                time.sleep(_MIN_FETCH_DELAY_SECONDS)

            text, success = self._fetch_one(url, camp.get("camp_id", ""))
            results.append((camp, text, success))

            status = f"OK ({len(text):,} chars)" if success else "FAIL"
            print(f"    {status}")

        return results

    def _fetch_one(self, url: str, camp_id: str) -> tuple[str, bool]:
        try:
            text = self._fetcher.to_flat_string(url)
            if text:
                return text, True
            fallback = self._fetcher.fetch(url)
            if fallback:
                return fallback, True
            return "", False
        except Exception as exc:
            _log.debug("Brief fetch failed for %s: %s", camp_id, exc)
            return "", False
