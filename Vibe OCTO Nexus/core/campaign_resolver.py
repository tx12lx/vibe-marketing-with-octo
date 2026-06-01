from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _best_match(hint: str, candidates: list[tuple[str, Path]]) -> tuple[float, Optional[Path]]:
    best_score = 0.0
    best_path: Optional[Path] = None
    for name, path in candidates:
        score = _similarity(hint, name)
        if score > best_score:
            best_score = score
            best_path = path
    return best_score, best_path


class CampaignResolver:
    """Finds the best matching campaign brief JSON from a natural language hint.

    Searches a directory of brief JSON files and returns the one whose
    campaign.id best matches the provided hint, using fuzzy string matching.
    """

    def __init__(self, briefs_dir: str | Path):
        self.briefs_dir = Path(briefs_dir)

    def _load_index(self) -> list[tuple[str, Path]]:
        index = []
        if not self.briefs_dir.exists():
            return index
        for path in self.briefs_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                campaign_id = data.get("campaign", {}).get("id", "")
                if campaign_id:
                    index.append((campaign_id, path))
            except Exception:
                continue
        return index

    def find(self, hint: str, min_score: float = 0.35) -> Optional[dict]:
        """Return the best matching brief JSON dict, or None if no match above min_score."""
        if not hint:
            return None
        index = self._load_index()
        if not index:
            return None
        score, path = _best_match(hint, index)
        if score >= min_score and path:
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    def find_path(self, campaign_id: str) -> Optional[Path]:
        """Return the path to a brief JSON by exact campaign ID."""
        if not self.briefs_dir.exists():
            return None
        safe = campaign_id.replace("/", "_").replace(" ", "_")[:60]
        candidate = self.briefs_dir / f"{safe}.json"
        if candidate.exists():
            return candidate
        # Fall back to fuzzy search
        index = self._load_index()
        score, path = _best_match(campaign_id, index)
        if score >= 0.8:
            return path
        return None

    def list_campaigns(self) -> list[str]:
        """Return sorted list of all known campaign IDs."""
        return sorted(name for name, _ in self._load_index())
