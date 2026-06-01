import difflib
import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()

FUELIX_BASE_URL = "https://api.fuelix.ai"
FUELIX_MODEL = "claude-sonnet-4"
SCHEMA_CHAR_LIMIT = 8_000


def _fix_table_refs(sql: str, project: str, datasets: list[str]) -> str:
    """Backtick-quote any unquoted fully-qualified table references."""
    for dataset in datasets:
        pattern = (
            r"(?<!`)"
            + re.escape(project)
            + r"\."
            + re.escape(dataset)
            + r"\.([A-Za-z0-9_]+)"
        )
        sql = re.sub(pattern, rf"`{project}.{dataset}.\1`", sql)
    return sql


def _correct_table_names(
    sql: str, project: str, datasets: list[str], known_tables: list[str]
) -> str:
    """Replace hallucinated table names with the closest match from known_tables."""
    if not known_tables:
        return sql

    for dataset in datasets:
        pattern = r"`" + re.escape(project) + r"\." + re.escape(dataset) + r"\.([A-Za-z0-9_]+)`"

        def _replace(m: re.Match) -> str:
            name = m.group(1)
            if name in known_tables:
                return m.group(0)
            matches = difflib.get_close_matches(name, known_tables, n=1, cutoff=0.6)
            if matches:
                return f"`{project}.{dataset}.{matches[0]}`"
            return m.group(0)

        sql = re.sub(pattern, _replace, sql)

    return sql


def generate_sql(
    question: str,
    schema_context: str,
    project: str,
    datasets: list[str],
    table_names: list[str] | None = None,
    business_context: str = "",
) -> str:
    api_key = os.getenv("FUELIX_API_KEY")
    if not api_key:
        raise RuntimeError(
            "FUELIX_API_KEY is not set. Add it to your .env file:\n"
            "  FUELIX_API_KEY=your-fuelix-api-key"
        )

    if schema_context and len(schema_context) > SCHEMA_CHAR_LIMIT:
        schema_context = schema_context[:SCHEMA_CHAR_LIMIT] + "\n-- (schema truncated)"
    schema_section = f"\nAvailable schema:\n{schema_context}" if schema_context else ""
    context_section = (
        f"\n\nBusiness context (authoritative — follow these rules over schema guesses):\n{business_context}"
        if business_context
        else ""
    )

    example_dataset = datasets[0] if datasets else "<dataset>"
    table_example = f"`{project}.{example_dataset}.table_name`"
    datasets_hint = (
        f"{table_example} (dataset is one of: " + ", ".join(f"`{d}`" for d in datasets) + ")"
        if datasets
        else table_example
    )

    system_prompt = (
        f"You are a BigQuery SQL expert. Translate the user's plain English question "
        f"into a valid BigQuery Standard SQL query.\n\n"
        f"Rules:\n"
        f"- Always wrap fully-qualified table names in backticks, e.g. {table_example}\n"
        f"- Table name format: {datasets_hint}\n"
        f"- Use Standard SQL syntax (not Legacy SQL)\n"
        f"- Include a LIMIT clause (default 100) unless the user explicitly asks for all rows\n"
        f"- Return ONLY the raw SQL — no explanation, no markdown, no code fences\n"
        f"- Do not include a trailing semicolon"
        f"{schema_section}"
        f"{context_section}"
    )

    response = requests.post(
        f"{FUELIX_BASE_URL}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": FUELIX_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            "max_tokens": 1024,
            "temperature": 0,
        },
        timeout=60,
    )
    response.raise_for_status()
    sql = response.json()["choices"][0]["message"]["content"].strip()

    # Strip markdown code fences (``` ... ```)
    if sql.startswith("```"):
        lines = sql.splitlines()
        sql = "\n".join(lines[1:] if lines[0].startswith("```") else lines)
    if sql.endswith("```"):
        sql = sql[: sql.rfind("```")].rstrip()

    sql = sql.strip()

    # Strip whole-SQL backtick wrapping
    _SQL_KEYWORDS = {"SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "CREATE", "MERGE"}
    if sql.startswith("`") and not sql.startswith("```"):
        first_word = sql[1:].split()[0].upper() if sql[1:].split() else ""
        if first_word in _SQL_KEYWORDS:
            sql = sql[1:]
            if sql.endswith("`"):
                sql = sql[:-1]
            sql = sql.strip()

    sql = _fix_table_refs(sql, project, datasets)

    if table_names:
        sql = _correct_table_names(sql, project, datasets, table_names)

    return sql
