"""
schema_discovery/discovery_layer.py

Pillar 2: Dynamic BigQuery Schema Discovery Layer.

Queries INFORMATION_SCHEMA.COLUMNS and INFORMATION_SCHEMA.TABLES for the
configured datasets to build a live schema snapshot. Results are cached with
a 1-hour TTL in a dedicated '.sdl_schema_cache.json' file that is owned
exclusively by this module. This file is fully isolated from the Quant
bq_reporter's '.schema_cache.json', eliminating any risk of mutual clobber.

All BigQuery access is strictly read-only. Only INFORMATION_SCHEMA SELECT
queries are issued. No column names, view names, or table names are hardcoded
in this module — all schema metadata is derived entirely from query results.
"""
from __future__ import annotations

import json
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class SchemaColumn:
    table_name: str
    column_name: str
    data_type: str
    is_nullable: bool
    description: str
    is_view: bool


@dataclass
class SchemaSnapshot:
    project: str
    datasets: list[str]
    columns: list[SchemaColumn]
    fetched_at: str   # ISO 8601 timestamp
    cache_hit: bool

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "datasets": self.datasets,
            "columns": [asdict(c) for c in self.columns],
            "fetched_at": self.fetched_at,
            "cache_hit": self.cache_hit,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SchemaSnapshot":
        columns = [SchemaColumn(**c) for c in d.get("columns", [])]
        return cls(
            project=d["project"],
            datasets=d["datasets"],
            columns=columns,
            fetched_at=d["fetched_at"],
            cache_hit=d.get("cache_hit", True),
        )


class SchemaDiscoveryLayer:
    """Fetches and caches INFORMATION_SCHEMA metadata for one or more BQ datasets.

    Cache strategy
    --------------
    Results are persisted in a dedicated '.sdl_schema_cache.json' file that
    belongs exclusively to this class. The Quant bq_reporter's '.schema_cache.json'
    is never touched, so there is no risk of SDL writes being overwritten by
    bq_client.py or vice versa.

    The cache file is a JSON object keyed by the SDL cache key string:
        {
            "<cache_key>": {
                "fetched_at": "<ISO timestamp>",
                "project":    "...",
                "datasets":   [...],
                "columns":    [...]
            }
        }

    Multiple (project, datasets) combinations can coexist in the same file
    because each uses a distinct cache key.
    """

    def __init__(
        self,
        project: str,
        datasets: list[str],
        cache_path: Path,
        ttl_seconds: int = 3600,
    ) -> None:
        self._project = project
        self._datasets = list(datasets)
        self._cache_path = cache_path
        self._ttl_seconds = ttl_seconds

    # ------------------------------------------------------------------
    # Cache key
    # ------------------------------------------------------------------

    def _cache_key(self) -> str:
        """Stable, unique key for this (project, datasets) combination.

        Includes "source": "schema_discovery_layer" as a structural discriminator.
        This guarantees the key can never match any key produced by the Quant
        bq_reporter (which uses {"project", "datasets", "tables"} with no source
        field), even if project and datasets are identical — though the two now
        live in separate files anyway.
        """
        return json.dumps(
            {
                "source": "schema_discovery_layer",
                "project": self._project,
                "datasets": sorted(self._datasets),
            },
            sort_keys=True,
        )

    # ------------------------------------------------------------------
    # Cache read / write
    # ------------------------------------------------------------------

    def _load_cache(self) -> Optional[dict]:
        """Return the cached entry for this key if present and within TTL, else None."""
        if not self._cache_path.exists():
            return None
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except Exception:
            return None

        if not isinstance(data, dict):
            return None

        entry = data.get(self._cache_key())
        if entry is None:
            return None

        try:
            fetched_at = datetime.fromisoformat(entry["fetched_at"])
            age_seconds = (datetime.now() - fetched_at).total_seconds()
            if age_seconds > self._ttl_seconds:
                return None
        except Exception:
            return None

        return entry

    def _save_cache(self, entry: dict) -> None:
        """Write the entry to the dedicated SDL cache file under this cache key.

        Reads the existing file first so that other (project, datasets) entries
        are preserved. Only the entry for the current cache key is updated.
        Cache write failures are swallowed — the next startup will re-fetch.
        """
        try:
            existing: dict = {}
            if self._cache_path.exists():
                try:
                    parsed = json.loads(self._cache_path.read_text(encoding="utf-8"))
                    if isinstance(parsed, dict):
                        existing = parsed
                except Exception:
                    existing = {}

            existing[self._cache_key()] = entry

            self._cache_path.write_text(
                json.dumps(existing, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_snapshot(self, refresh: bool = False) -> SchemaSnapshot:
        """Return a cached or freshly queried SchemaSnapshot.

        Cache key: JSON of {source, project, datasets (sorted)}.
        On TTL expiry (>1 hour since fetched_at) or refresh=True: re-queries
        INFORMATION_SCHEMA and writes a fresh entry to the cache file.
        """
        if not refresh:
            cached = self._load_cache()
            if cached is not None:
                return SchemaSnapshot.from_dict({**cached, "cache_hit": True})

        columns_rows, table_rows = self._query_information_schema()
        columns = self._build_columns(columns_rows, table_rows)
        fetched_at = datetime.now().isoformat()

        entry = {
            "project": self._project,
            "datasets": self._datasets,
            "fetched_at": fetched_at,
            "columns": [asdict(c) for c in columns],
        }
        self._save_cache(entry)

        return SchemaSnapshot(
            project=self._project,
            datasets=self._datasets,
            columns=columns,
            fetched_at=fetched_at,
            cache_hit=False,
        )

    def to_prompt_string(self, snapshot: SchemaSnapshot) -> str:
        """Flatten the snapshot to a compact, token-efficient string for LLM injection.

        Groups columns by table, preserving INFORMATION_SCHEMA ordinal_position
        order. Each table block is prefixed with its object type (VIEW or TABLE).
        Column descriptions are appended inline when non-empty. The NOT NULL tag
        is emitted only for non-nullable columns; nullable is the silent default,
        keeping token count low.

        All table names, column names, data types, and type classifications
        are derived entirely from the live INFORMATION_SCHEMA query results.
        Nothing is hardcoded in this method.
        """
        if not snapshot.columns:
            return (
                f"-- No schema available for {snapshot.project} "
                f"({', '.join(snapshot.datasets)})"
            )

        # Preserve ordinal_position order — the columns query already sorts by
        # (table_name, ordinal_position), so insertion order is correct.
        tables: dict[str, list[SchemaColumn]] = {}
        for col in snapshot.columns:
            tables.setdefault(col.table_name, []).append(col)

        dataset = snapshot.datasets[0] if snapshot.datasets else "unknown"

        lines: list[str] = [
            f"=== LIVE SCHEMA: {snapshot.project}.{dataset}"
            f" (fetched {snapshot.fetched_at[:19]}) ===",
            "",
        ]

        for table_name, cols in tables.items():
            kind = "VIEW" if cols[0].is_view else "TABLE"
            lines.append(f"{kind} `{snapshot.project}.{dataset}.{table_name}`:")
            for col in cols:
                not_null = "" if col.is_nullable else " NOT NULL"
                desc = f"  -- {col.description}" if col.description else ""
                lines.append(f"  {col.column_name} {col.data_type}{not_null}{desc}")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # BigQuery queries — strictly read-only
    # ------------------------------------------------------------------

    def _query_information_schema(self) -> tuple[list[dict], list[dict]]:
        """Issue read-only INFORMATION_SCHEMA queries against each dataset.

        Returns:
            columns_rows: rows from INFORMATION_SCHEMA.COLUMNS
            table_rows:   rows from INFORMATION_SCHEMA.TABLES

        Both queries are SELECT-only against INFORMATION_SCHEMA views.
        No production data tables are read or modified.
        """
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from google.cloud import bigquery  # type: ignore

            client = bigquery.Client(project=self._project)

        all_columns: list[dict] = []
        all_tables: list[dict] = []

        for dataset in self._datasets:
            cols_sql = (
                "SELECT\n"
                "    table_name,\n"
                "    column_name,\n"
                "    data_type,\n"
                "    CASE WHEN is_nullable = 'YES' THEN TRUE ELSE FALSE END AS is_nullable\n"
                f"FROM `{self._project}.{dataset}.INFORMATION_SCHEMA.COLUMNS`\n"
                "ORDER BY table_name, ordinal_position"
            )
            tables_sql = (
                "SELECT table_name, table_type\n"
                f"FROM `{self._project}.{dataset}.INFORMATION_SCHEMA.TABLES`\n"
                "WHERE table_type IN ('VIEW', 'MATERIALIZED_VIEW', 'BASE TABLE')"
            )

            cols_result = client.query(cols_sql).result()
            for row in cols_result:
                all_columns.append(dict(row))

            tables_result = client.query(tables_sql).result()
            for row in tables_result:
                all_tables.append(dict(row))

        return all_columns, all_tables

    def _build_columns(
        self,
        columns_rows: list[dict],
        table_rows: list[dict],
    ) -> list[SchemaColumn]:
        """Build SchemaColumn objects by cross-referencing column rows with table type.

        The is_view flag is determined solely by comparing the table_type string
        returned by INFORMATION_SCHEMA.TABLES against the known view type values.
        No table or column names are evaluated with any hardcoded logic.
        """
        view_types = {"VIEW", "MATERIALIZED_VIEW"}
        table_type_map: dict[str, str] = {
            row["table_name"]: row["table_type"] for row in table_rows
        }

        columns: list[SchemaColumn] = []
        for row in columns_rows:
            table_name = row["table_name"]
            table_type = table_type_map.get(table_name, "BASE TABLE")
            columns.append(
                SchemaColumn(
                    table_name=table_name,
                    column_name=row["column_name"],
                    data_type=str(row["data_type"]),
                    is_nullable=bool(row["is_nullable"]),
                    description=str(row.get("description") or ""),
                    is_view=table_type in view_types,
                )
            )
        return columns
