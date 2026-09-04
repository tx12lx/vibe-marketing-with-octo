"""knowledge/retrieve.py -- the read-side functions the agents actually use:
find similar past campaigns by meaning, get the current glossary, get the
active business rules, and get a PII-safe schema description for a set of
tables. Built and tested on their own so wiring them into the agents (via
knowledge/context.py) is just wiring, not more design work.
"""
from __future__ import annotations

import math
from typing import Optional

from core.ai_client import embed_text
from knowledge.store import connect, get_active_rules, get_all_campaign_summaries, get_glossary_terms, get_table_schema_rows


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def find_similar_campaigns(query_text: str, top_k: int = 5) -> list[dict]:
    """Return the top_k most similar stored campaign summaries, by meaning.

    Comparing meaning-numbers directly in plain Python -- with only a handful
    of campaign summaries expected, this is simple today and carries over
    unchanged when this data moves into BigQuery (a VECTOR_SEARCH/manual
    cosine query can replace the loop below without changing this function's
    contract).
    """
    with connect() as conn:
        summaries = get_all_campaign_summaries(conn)
    if not summaries:
        return []

    query_embedding = embed_text(query_text)
    scored = [
        {**s, "similarity": _cosine_similarity(query_embedding, s["embedding"])}
        for s in summaries
    ]
    scored.sort(key=lambda s: s["similarity"], reverse=True)
    return scored[:top_k]


def get_glossary_summary() -> str:
    with connect() as conn:
        terms = get_glossary_terms(conn)
    if not terms:
        return "(no glossary terms yet)"
    return "\n".join(f"- {t['term']}: {t['definition']}" for t in terms)


def get_active_rules_text(scope: Optional[str] = None, campaign_code: Optional[str] = None) -> str:
    with connect() as conn:
        rules = get_active_rules(conn, scope=scope, campaign_code=campaign_code)
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
            tokens.append(f"-- {row['column_description']}")
        lines.append("  " + " ".join(tokens))
    if current_table is not None:
        parts.append(f"TABLE `{'.'.join(current_table)}` (\n" + ",\n".join(lines) + "\n)")
    return "\n\n".join(parts)
