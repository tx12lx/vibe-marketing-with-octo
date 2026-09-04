"""knowledge/sync_schema.py -- pull the real, live structure of the named
BigQuery tables and store what each table and column means.

Safe to re-run any time by hand (no automatic schedule). Prefers each column's
real, existing BigQuery description; only asks Gemini for a short one when
BigQuery has none, so the tool never invents an explanation for something
that's already documented. Sensitivity is computed directly from
core.pii_masking's existing rules, so this can never disagree with what the
sizing tool itself masks.

Usage:  python -m knowledge.sync_schema
"""
from __future__ import annotations

import json
import logging
import sys
import warnings
from typing import Optional

from core.ai_client import ask_ai
from core.pii_masking import _is_filter_only_pii, _is_pii
from knowledge.store import IntegrityError, connect, replace_table_columns

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# The tables this knowledge layer knows about. Scoped to one table for the
# current proof-of-concept -- expanding to the full nine is purely a matter of
# adding their fully-qualified names here.
DEFAULT_TABLES = [
    "bi-srv-hsmdet-pr-7b9def.campaign_data.bq_fda_mob_mobility_base",
]


def _sensitivity_for(column_name: str) -> str:
    if _is_pii(column_name):
        return "hidden"
    if _is_filter_only_pii(column_name):
        return "filter_only"
    return "none"


_DESCRIBE_CHUNK_SIZE = 25
_DESCRIBE_TOKENS_PER_COLUMN = 40  # generous headroom per column so a chunk never gets truncated


def _describe_column_chunk(table_name: str, chunk: list[dict]) -> dict[str, str]:
    listing = "\n".join(f"- {c['name']} ({c['data_type']})" for c in chunk)
    prompt = (
        f"For the BigQuery table `{table_name}`, write a short (under 12 words) plain-English "
        f"description for each of these columns, based only on its name and type -- no guessing "
        f"at business meaning you can't infer from the name itself. Columns:\n{listing}\n\n"
        'Reply with only a JSON object mapping column name to description, e.g. '
        '{"some_column": "short description"}.'
    )
    raw = ask_ai(prompt, temperature=0, max_tokens=len(chunk) * _DESCRIBE_TOKENS_PER_COLUMN, thinking_budget=0)
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


def _describe_missing_columns(table_name: str, columns: list[dict]) -> dict[str, str]:
    """Ask Gemini for short descriptions, but only for columns BigQuery didn't
    already document -- chunked so a wide table's response never gets
    truncated into invalid JSON."""
    undocumented = [c for c in columns if not c["description"] and c["sensitivity"] == "none"]
    if not undocumented:
        return {}

    descriptions: dict[str, str] = {}
    for i in range(0, len(undocumented), _DESCRIBE_CHUNK_SIZE):
        chunk = undocumented[i:i + _DESCRIBE_CHUNK_SIZE]
        try:
            descriptions.update(_describe_column_chunk(table_name, chunk))
        except Exception as exc:  # noqa: BLE001 -- a failed chunk is non-fatal, the rest still try
            _log.warning(
                "Could not generate descriptions for %s columns %d-%d: %s",
                table_name, i, i + len(chunk), exc,
            )
    return descriptions


def _describe_table(table_name: str, existing_description: str, columns: list[dict]) -> str:
    if existing_description:
        return existing_description
    listing = ", ".join(c["name"] for c in columns[:20])
    prompt = (
        f"In one short sentence (under 20 words), describe what the BigQuery table "
        f"`{table_name}` most likely contains, based only on its name and this sample of its "
        f"column names: {listing}. Do not guess specific business rules -- just what kind of "
        f"data this table holds."
    )
    try:
        return ask_ai(prompt, temperature=0, max_tokens=64, thinking_budget=0).strip()
    except Exception as exc:  # noqa: BLE001 -- a failed description pass is non-fatal
        _log.warning("Could not generate a description for %s: %s", table_name, exc)
        return ""


def sync_table(fully_qualified_name: str) -> None:
    """Fetch one table's real schema from BigQuery and store it."""
    project, dataset, table_id = fully_qualified_name.split(".", 2)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from google.cloud import bigquery  # noqa: PLC0415

    client = bigquery.Client(project=project)
    table = client.get_table(fully_qualified_name)

    columns = []
    for field in table.schema:
        columns.append({
            "name": field.name,
            "data_type": field.field_type,
            "mode": field.mode or "",
            "sensitivity": _sensitivity_for(field.name),
            "description": field.description or "",
        })

    generated = _describe_missing_columns(table_id, columns)
    for c in columns:
        if not c["description"] and c["name"] in generated:
            c["description"] = generated[c["name"]]

    table_description = _describe_table(table_id, table.description or "", columns)

    with connect() as conn:
        try:
            replace_table_columns(conn, project, dataset, table_id, table_description, columns)
        except IntegrityError:
            _log.error(
                "Sync of %s stopped -- the new schema looks suspicious compared to what was "
                "already stored. Nothing was overwritten.", fully_qualified_name,
            )
            raise

    hidden = sum(1 for c in columns if c["sensitivity"] == "hidden")
    filter_only = sum(1 for c in columns if c["sensitivity"] == "filter_only")
    _log.info(
        "Synced %s -- %d column(s) (%d hidden, %d filter-only).",
        fully_qualified_name, len(columns), hidden, filter_only,
    )


def main(table_names: Optional[list[str]] = None) -> None:
    for name in (table_names or DEFAULT_TABLES):
        sync_table(name)


if __name__ == "__main__":
    main(sys.argv[1:] or None)
