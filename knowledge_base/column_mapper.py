from __future__ import annotations

import json
from pathlib import Path


class ColumnResolutionError(Exception):
    pass


class ColumnMapper:
    def __init__(self, config_path: Path) -> None:
        with open(config_path, "r", encoding="utf-8") as fh:
            config = json.load(fh)
        self._case_sensitive: bool = config.get("case_sensitive", False)
        self._required: list[str] = config["required_columns"]
        self._patterns: dict[str, list[str]] = config["patterns"]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_columns(self, bq_columns: list[str]) -> dict[str, str]:
        """Map logical column names to actual BQ column names.

        Returns a dict {logical_name: actual_bq_column}.
        Raises ColumnResolutionError if any required logical column cannot
        be matched.
        """
        lookup = self._build_lookup(bq_columns)
        mapping: dict[str, str] = {}
        missing: dict[str, list[str]] = {}

        for logical, patterns in self._patterns.items():
            match = self._find_match(patterns, lookup)
            if match is not None:
                mapping[logical] = match
            elif logical in self._required:
                missing[logical] = patterns

        if missing:
            available = ", ".join(sorted(bq_columns))
            lines = [
                "Column resolution failed. Could not find BQ columns for:"
            ]
            for logical, tried in sorted(missing.items()):
                lines.append(f"  {logical}: Tried patterns {tried}")
            lines.append(f"Available BQ columns: [{available}]")
            raise ColumnResolutionError("\n".join(lines))

        return mapping

    def validate_required_columns(self, mapping: dict[str, str]) -> bool:
        """Return True only if every required logical column appears in mapping."""
        return all(col in mapping for col in self._required)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _normalise(self, value: str) -> str:
        return value if self._case_sensitive else value.lower()

    def _build_lookup(self, bq_columns: list[str]) -> dict[str, str]:
        """Return {normalised_name: original_name} for fast lookup."""
        return {self._normalise(col): col for col in bq_columns}

    def _find_match(
        self, patterns: list[str], lookup: dict[str, str]
    ) -> str | None:
        """Try exact match for each pattern, then fuzzy (substring) match."""
        # Pass 1: exact match on normalised names
        for pattern in patterns:
            key = self._normalise(pattern)
            if key in lookup:
                return lookup[key]

        # Pass 2: fuzzy substring match — pattern contained in a BQ column name
        for pattern in patterns:
            key = self._normalise(pattern)
            for norm_col, orig_col in lookup.items():
                if key in norm_col or norm_col in key:
                    return orig_col

        return None
