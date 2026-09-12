"""knowledge/retrieve.py -- the read-side functions the agents actually use:
get the current glossary, get the active business rules, and get a PII-safe
schema description for a set of tables. Built and tested on their own so
wiring them into the agents (via knowledge/context.py) is just wiring, not
more design work.
"""
from __future__ import annotations

from typing import Optional

from knowledge.store import connect, get_active_rules, get_glossary_terms, get_table_schema_rows


def get_glossary_summary() -> str:
    with connect() as conn:
        terms = get_glossary_terms(conn)
    if not terms:
        return "(no glossary terms yet)"
    return "\n".join(f"- {t['term']}: {t['definition']}" for t in terms)


def get_active_rules_text(
    scope: Optional[str] = None,
    table_name: Optional[str] = None,
) -> str:
    with connect() as conn:
        rules = get_active_rules(conn, scope=scope, table_name=table_name)
    if not rules:
        return "(no confirmed business rules yet)"
    return "\n".join(f"- [{r['scope']}] {r['rule_text']}" for r in rules)


def get_table_schema_text(table_names: Optional[list[str]] = None) -> str:
    """PII-safe schema description built from the knowledge layer's own stored
    metadata (fast, local) rather than a live BigQuery call."""
    with connect() as conn:
        rows = get_table_schema_rows(conn, table_names)
    if not rows:
        return "(no tables synced yet)"

    parts: list[str] = []
    current_table = None
    lines: list[str] = []
    for row in rows:
        table_key = (row["project"], row["dataset"], row["table_name"])
        if table_key != current_table:
            if current_table is not None:
                parts.append(f"TABLE `{'.'.join(current_table)}` (\n" + ",\n".join(lines) + "\n)")
            current_table = table_key
            lines = []
        if row["sensitivity"] == "hidden":
            continue  # never shown to the model, same rule as core/pii_masking.py
        tokens = [row["column_name"], row["data_type"]]
        if row["mode"]:
            tokens.append(row["mode"])
        if row["sensitivity"] == "filter_only":
            tokens.append("-- filter/count only, values masked")
        if row["column_description"]:
            marker = " (confirmed)" if row["description_source"] == "human" else " (unconfirmed guess)"
            tokens.append(f"-- {row['column_description']}{marker}")
        if row["value_notes"]:
            tokens.append(f"-- actual values: {row['value_notes']}")
        lines.append("  " + " ".join(tokens))
    if current_table is not None:
        parts.append(f"TABLE `{'.'.join(current_table)}` (\n" + ",\n".join(lines) + "\n)")
    return "\n\n".join(parts)
