from __future__ import annotations

import json
from pathlib import Path

import click

from core.json_formatter import generate_sql


@click.command()
@click.option("--json-file", required=True, type=click.Path(exists=True), help="Path to Vibe Briefing JSON output file")
@click.option("--output", "-o", default=None, type=click.Path(), help="Write SQL to file instead of stdout")
@click.option("--show-metadata", is_flag=True, help="Print brief metadata as SQL comments at the top")
def sql(json_file, output, show_metadata):
    """Generate a BigQuery SELECT query from a Vibe Briefing JSON output.

    Extracts the pre-built sql_snippets from every audience condition, exclusion,
    and join to produce a runnable WHERE clause. Useful for QA and debugging.

    Example:

        vibe-briefing sql --json-file output/q3_winback.json | bq query --use_legacy_sql=false
    """
    data = json.loads(Path(json_file).read_text(encoding="utf-8"))

    lines: list[str] = []

    if show_metadata:
        meta = data.get("brief_metadata", {})
        lines.extend([
            f"-- Brief:     {meta.get('brief_name', 'unknown')}",
            f"-- Portfolio: {meta.get('portfolio', 'unknown')}",
            f"-- Objective: {meta.get('campaign_objective', 'unknown')}",
            f"-- Generated: {meta.get('translation_timestamp', 'unknown')}",
            "",
        ])

    readiness = data.get("execution_readiness", {})
    if not readiness.get("ready_to_execute", True):
        blockers = readiness.get("blockers", [])
        click.echo("WARNING: JSON is not marked as ready_to_execute.", err=True)
        for b in blockers:
            click.echo(f"  Blocker: {b}", err=True)

    sql_text = generate_sql(data)
    lines.append(sql_text)

    result = "\n".join(lines)

    if output:
        Path(output).write_text(result, encoding="utf-8")
        click.echo(f"SQL written to: {output}")
    else:
        click.echo(result)
