"""knowledge_base/sources/sheets_enricher.py — Google Sheets brief enricher.

Fetches data briefs from Google Sheets URLs with mandatory rate limiting.
Only used from `--refresh-briefs` mode — never called during --full-refresh.

Rate limiting (configurable via ingestion_config.json `brief_fetch` block):
  inter_fetch_delay_seconds (default 15): minimum pause between each document fetch.
  max_per_run (default 10): hard cap on briefs fetched in a single invocation.

The delay includes ±3s random jitter so the access pattern is indistinguishable
from a person manually opening documents one at a time, preventing Google
Workspace "Mass Download Event" security alerts.
"""
from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "ingestion_config.json"
_DEFAULT_DELAY = 15
_DEFAULT_MAX_PER_RUN = 10
_JITTER_SECONDS = 3


def _load_rate_config() -> tuple[int, int]:
    """Return (inter_fetch_delay_seconds, max_per_run) from ingestion_config.json."""
    try:
        cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        bf = cfg.get("brief_fetch") or {}
        delay = int(bf.get("inter_fetch_delay_seconds", _DEFAULT_DELAY))
        cap = int(bf.get("max_per_run", _DEFAULT_MAX_PER_RUN))
        return max(1, delay), max(1, cap)
    except Exception:
        return _DEFAULT_DELAY, _DEFAULT_MAX_PER_RUN


class SheetsEnricher:
    """Fetches and caches brief text from Google Sheets URLs.

    Enforces a configurable inter-fetch delay (default 15s) with random jitter
    and a per-run cap (default 10) to prevent Google Workspace mass-download
    security alerts.  Requires explicit confirmation before any fetch begins.
    """

    def __init__(self, brief_fetcher: object, timeout: int = 60) -> None:
        self._fetcher = brief_fetcher
        self._timeout = timeout
        self._delay, self._max_per_run = _load_rate_config()

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

        total_available = len(targets)
        if total_available > self._max_per_run:
            targets = targets[: self._max_per_run]
            print(
                f"\n  NOTE: {total_available} campaigns have briefs to fetch, "
                f"but this run is capped at {self._max_per_run}."
            )
            print("  Run --refresh-briefs again to continue with the remaining campaigns.")

        print(f"\n  About to fetch {len(targets)} Google Sheets document(s):")
        for c in targets:
            print(f"    - {c.get('camp_id', '?')}: {c.get('databrief_link', '')[:80]}")

        print(f"\n  Rate limit: 1 document every ~{self._delay}s (±{_JITTER_SECONDS}s jitter)")
        est_seconds = len(targets) * self._delay
        print(f"  Estimated time: ~{est_seconds}s ({est_seconds // 60}m {est_seconds % 60}s)")
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
                actual_delay = self._delay + random.uniform(-_JITTER_SECONDS, _JITTER_SECONDS)
                actual_delay = max(1.0, actual_delay)
                print(f"    Waiting {actual_delay:.1f}s before next request...")
                time.sleep(actual_delay)

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
