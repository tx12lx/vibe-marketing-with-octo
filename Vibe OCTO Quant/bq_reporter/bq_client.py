import json
import time
import warnings
from pathlib import Path

from google.cloud import bigquery

_VIEW_TYPES = {"VIEW", "MATERIALIZED_VIEW"}

# Two-tier PII handling:
#
# _PII_HIDDEN: columns completely removed from the schema sent to the LLM.
#   The model can't reference what it doesn't know exists. Use for any column
#   never needed for sizing (names, addresses, IMEI, secondary phones, etc.).
#
# _PII_FILTER_ONLY: columns kept visible in the schema (so the LLM can COUNT
#   them or write WHERE clauses against them), but their *values* are masked
#   ("***") in query results. Use for counting identifiers and filter-only
#   PII (e.g. WHERE CBR IS NOT NULL AND CBR != '').
#
# Both sets are matched case-insensitively.

_PII_HIDDEN = {
    # Mobility base
    "fstnam", "lstnam", "subscriber_name",
    "addr1", "addr2", "city", "postcode",
    "imei", "uuid", "email",
    # Customer profile
    "name_first", "name_last", "name_full", "name_email",
    "mail_address", "mail_city", "mail_postal_cd", "mail_country",
    "serv_address", "serv_city", "serv_postal_code", "serv_country",
    "sms_number", "svc_email_address",
}

_PII_FILTER_ONLY = {
    # Counting IDs — visible in schema so the LLM can COUNT(DISTINCT ...) them,
    # but their values are masked in output. Mobility uses lowercase, customer
    # profile uses uppercase; matched case-insensitively.
    "ban", "subscriber_no",            # mobility
    "bacct_num", "cust_id",            # customer profile
    # Reachability filter columns — usable in WHERE only; values masked.
    "cbr", "mkt_email_address",
}


def _is_pii(name: str) -> bool:
    return name.lower() in _PII_HIDDEN


def _is_filter_only_pii(name: str) -> bool:
    return name.lower() in _PII_FILTER_ONLY


def _schema_for_dataset(
    client: "bigquery.Client",
    project: str,
    dataset: str,
    views_only: bool = True,
    table_names: list[str] | None = None,
) -> list[str]:
    """Return schema strings for the requested tables/views in one dataset.

    If ``table_names`` is given, fetch only those specific tables directly
    (no full dataset listing). Otherwise fetch all views (or all tables when
    ``views_only=False``).
    """
    if table_names:
        parts = []
        for name in table_names:
            try:
                table = client.get_table(f"{project}.{dataset}.{name}")
                fields = []
                for field in table.schema:
                    if _is_pii(field.name):
                        continue
                    tokens = [field.name, field.field_type]
                    if field.mode not in ("NULLABLE", ""):
                        tokens.append(field.mode)
                    if field.description:
                        tokens.append(f"-- {field.description}")
                    fields.append("  " + " ".join(tokens))
                kind = (
                    "MATERIALIZED VIEW" if table.table_type == "MATERIALIZED_VIEW" else "VIEW"
                )
                parts.append(
                    f"{kind} `{project}.{dataset}.{name}` (\n"
                    + ",\n".join(fields)
                    + "\n)"
                )
            except Exception:
                parts.append(f"VIEW `{project}.{dataset}.{name}` (schema unavailable)")
        return parts

    try:
        all_refs = list(client.list_tables(f"{project}.{dataset}"))
    except Exception as exc:
        return [f"-- Dataset `{project}.{dataset}`: could not fetch schema ({exc})"]

    refs = [r for r in all_refs if r.table_type in _VIEW_TYPES] if views_only else all_refs

    if not refs:
        label = "views" if views_only else "tables/views"
        return [f"-- Dataset `{project}.{dataset}`: no {label} found"]

    parts = []
    for table_ref in refs:
        try:
            table = client.get_table(table_ref)
            fields = []
            for field in table.schema:
                if _is_pii(field.name):
                    continue
                tokens = [field.name, field.field_type]
                if field.mode not in ("NULLABLE", ""):
                    tokens.append(field.mode)
                if field.description:
                    tokens.append(f"-- {field.description}")
                fields.append("  " + " ".join(tokens))
            kind = "MATERIALIZED VIEW" if table_ref.table_type == "MATERIALIZED_VIEW" else "VIEW"
            parts.append(
                f"{kind} `{project}.{dataset}.{table_ref.table_id}` (\n"
                + ",\n".join(fields)
                + "\n)"
            )
        except Exception:
            parts.append(
                f"VIEW `{project}.{dataset}.{table_ref.table_id}` (schema unavailable)"
            )
    return parts


_SCHEMA_CACHE_TTL_SEC = 24 * 60 * 60  # 24 hours


def _cache_key(project: str, datasets: list[str], table_names: list[str] | None) -> str:
    """Stable key for cache invalidation when scope changes."""
    return json.dumps({
        "project": project,
        "datasets": sorted(datasets),
        "tables": sorted(table_names) if table_names else None,
    }, sort_keys=True)


def _load_cached_schema(cache_path: Path, key: str) -> str | None:
    """Return cached schema if file exists, key matches, and TTL not expired."""
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("key") != key:
        return None
    age = time.time() - data.get("fetched_at", 0)
    if age > _SCHEMA_CACHE_TTL_SEC:
        return None
    return data.get("schema")


def _save_schema_cache(cache_path: Path, key: str, schema: str) -> None:
    """Persist schema to disk for re-use across runs."""
    cache_path.write_text(
        json.dumps({"key": key, "fetched_at": time.time(), "schema": schema}),
        encoding="utf-8",
    )


def get_schema(
    project: str,
    datasets: list[str],
    views_only: bool = True,
    table_names: list[str] | None = None,
    cache_path: Path | None = None,
    refresh: bool = False,
) -> str:
    """Fetch and combine schemas for one or more datasets.

    If ``cache_path`` is given, a 24-hour disk cache is used; ``refresh=True``
    forces a fresh fetch. The cache key includes project + datasets + tables,
    so changing scope invalidates automatically.

    If ``table_names`` is provided, only those specific tables are fetched —
    no full dataset listing. Otherwise all VIEWs/MATERIALIZED_VIEWs are
    included (or all objects when ``views_only=False``).
    """
    key = _cache_key(project, datasets, table_names)
    if cache_path is not None and not refresh:
        cached = _load_cached_schema(cache_path, key)
        if cached is not None:
            return cached

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = bigquery.Client(project=project)
    all_parts: list[str] = []
    for dataset in datasets:
        all_parts.extend(
            _schema_for_dataset(
                client, project, dataset, views_only=views_only, table_names=table_names
            )
        )
    schema = "\n\n".join(all_parts)

    if cache_path is not None:
        try:
            _save_schema_cache(cache_path, key, schema)
        except Exception:
            pass  # cache write failures are non-fatal
    return schema


def dry_run_query(sql: str, project: str) -> tuple[bool, str | None, int]:
    """Validate SQL with BigQuery without executing it.

    Returns:
        (is_valid, error_message, bytes_processed)
        - is_valid: True if BigQuery accepts the SQL
        - error_message: None on success, or the exception text on failure
        - bytes_processed: BigQuery's cost estimate in bytes (0 on failure)
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = bigquery.Client(project=project)
    job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    try:
        job = client.query(sql, job_config=job_config)
        return True, None, int(job.total_bytes_processed or 0)
    except Exception as exc:
        return False, str(exc), 0


def format_bytes(n: int) -> str:
    """Human-readable byte count for cost-estimate display."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def run_query(sql: str, project: str, max_results: int = 100) -> tuple[list[dict], list[str]]:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        client = bigquery.Client(project=project)
    job = client.query(sql)
    result = job.result()

    columns = [field.name for field in result.schema]
    masked = {c for c in columns if _is_filter_only_pii(c)}

    rows: list[dict] = []
    for row in result:
        if len(rows) >= max_results:
            break
        out = {}
        for col in columns:
            val = row[col]
            if val is None:
                out[col] = ""
            elif col in masked:
                out[col] = "***"
            else:
                out[col] = str(val)
        rows.append(out)

    return rows, columns
