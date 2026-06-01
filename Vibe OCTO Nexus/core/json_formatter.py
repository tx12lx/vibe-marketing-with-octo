from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


VIBE_BRIEFING_VERSION = "1.0"


def add_translation_metadata(output: dict, glossary_version: str = "unknown") -> dict:
    """Inject translation timestamp and version into brief_metadata if present."""
    if "brief_metadata" in output:
        output["brief_metadata"]["translation_timestamp"] = (
            datetime.now(tz=timezone.utc).isoformat()
        )
        output["brief_metadata"]["vibe_briefing_version"] = VIBE_BRIEFING_VERSION
        if not output["brief_metadata"].get("glossary_version"):
            output["brief_metadata"]["glossary_version"] = glossary_version
    return output


def save_output(data: dict, path: str) -> Path:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def generate_sql(output_json: dict) -> str:
    """Generate a BigQuery SELECT query from a Vibe Briefing JSON output."""
    audience = output_json.get("audience", {})
    primary_segment = audience.get("primary_segment", {})
    data_rules = primary_segment.get("data_rules", {})
    conditions = data_rules.get("conditions", [])
    exclusions = audience.get("exclusions", [])
    joins = primary_segment.get("joins", [])
    freq_cap = audience.get("frequency_cap")

    data_sources = output_json.get("data_sources", {})
    tables = data_sources.get("primary_source", {}).get("tables", [])
    primary_table = tables[0]["table_name"] if tables else "customer_base"
    primary_alias = tables[0].get("alias", "c") if tables else "c"

    lines: list[str] = [f"SELECT\n  {primary_alias}.*"]
    lines.append(f"FROM `{primary_table}` AS {primary_alias}")

    for join in joins:
        snippet = join.get("sql_snippet", "").strip()
        if snippet:
            lines.append(snippet)

    where_parts: list[str] = []

    for cond in conditions:
        snippet = cond.get("sql_snippet", "").strip()
        if snippet:
            where_parts.append(f"  -- [{cond.get('business_term', '')}]\n  {snippet}")

    for excl in exclusions:
        snippet = excl.get("sql_snippet", "").strip()
        if snippet:
            where_parts.append(
                f"  -- exclusion: {excl.get('exclusion_name', '')}\n  NOT ({snippet})"
            )

    if freq_cap:
        snippet = freq_cap.get("sql_snippet", "").strip()
        if snippet:
            where_parts.append(f"  -- frequency cap\n  {snippet}")

    if where_parts:
        lines.append("WHERE\n" + "\n  AND ".join(where_parts))

    return "\n".join(lines)
