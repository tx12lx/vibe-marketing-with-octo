from __future__ import annotations

import json
from pathlib import Path

import click
from dotenv import load_dotenv

from core.campaign_resolver import CampaignResolver
from core.feedback_schema import create_entry, append_to_brief, read_all_feedback


@click.command()
@click.option("--campaign", required=True, help="Campaign ID (or partial name) to log feedback for")
@click.option("--tool", required=True,
              type=click.Choice(["sizing_tool", "brief_assistant", "brief_regeneration", "human"]),
              help="Which tool is submitting this feedback")
@click.option("--action", default="general", help="Action that was performed")
@click.option("--status", default="success", type=click.Choice(["success", "error", "partial"]),
              help="Outcome of the tool run")
@click.option("--result-json", default=None, help="JSON string of the tool result (optional)")
@click.option("--sql", default=None, help="SQL that was executed (sizing tool)")
@click.option("--row-count", default=None, type=int, help="Row count returned by sizing query")
@click.option("--correction", "corrections", multiple=True,
              help="Correction to a previous output (repeatable)")
@click.option("--confirmed-correct", is_flag=True,
              help="Confirm the output was correct (positive learning signal)")
@click.option("--glossary", default="glossary/aal_test.json", envvar="VIBE_GLOSSARY",
              show_default=True)
@click.option("--briefs-dir", default=None)
def feedback(campaign, tool, action, status, result_json, sql, row_count,
             corrections, confirmed_correct, glossary, briefs_dir):
    """Submit feedback from a downstream tool or human reviewer."""
    load_dotenv()

    glossary_path = Path(glossary)
    briefs_path = Path(briefs_dir) if briefs_dir else glossary_path.parent / "briefs"
    resolver = CampaignResolver(briefs_path)
    brief_file = resolver.find_path(campaign)

    if not brief_file:
        known = resolver.list_campaigns()
        raise click.ClickException(
            f"No brief found for '{campaign}'. Known: {', '.join(known) if known else 'none'}"
        )

    result_data: dict = {"status": status}
    if result_json:
        try:
            result_data.update(json.loads(result_json))
        except json.JSONDecodeError as exc:
            raise click.BadParameter(f"--result-json is not valid JSON: {exc}")

    entry = create_entry(
        source_tool=tool, action=action, result=result_data,
        corrections=list(corrections), confirmed_correct=confirmed_correct,
        sql_executed=sql, row_count=row_count,
    )
    append_to_brief(brief_file, entry)

    click.echo(f"Feedback logged to: {brief_file}")
    click.echo(f"  entry_id : {entry['entry_id']}")
    click.echo(f"  source   : {tool} / {action} / {status}")
    if row_count is not None:
        click.echo(f"  rows     : {row_count:,}")
    if confirmed_correct:
        click.echo("  signal   : confirmed correct")
    if corrections:
        click.echo(f"  signal   : {len(corrections)} correction(s)")


@click.command("feedback-summary")
@click.option("--glossary", default="glossary/aal_test.json", envvar="VIBE_GLOSSARY", show_default=True)
@click.option("--briefs-dir", default=None)
def feedback_summary(glossary, briefs_dir):
    """Show a summary of all accumulated feedback entries across all briefs."""
    load_dotenv()
    glossary_path = Path(glossary)
    briefs_path = Path(briefs_dir) if briefs_dir else glossary_path.parent / "briefs"
    entries = read_all_feedback(briefs_path)

    if not entries:
        click.echo("No feedback entries found yet.")
        return

    by_tool: dict[str, list] = {}
    for e in entries:
        t = e.get("source", {}).get("tool", "unknown")
        by_tool.setdefault(t, []).append(e)

    click.echo(f"Total feedback entries: {len(entries)}")
    click.echo("\nBy source:")
    for t, es in sorted(by_tool.items()):
        confirmed = sum(1 for e in es if e.get("learning_signal", {}).get("confirmed_correct"))
        corr = sum(len(e.get("learning_signal", {}).get("corrections", [])) for e in es)
        click.echo(f"  {t:<25} {len(es):>4} entries | {confirmed} confirmed | {corr} corrections")

    actionable = sum(
        1 for e in entries
        if e.get("learning_signal", {}).get("corrections")
        or e.get("learning_signal", {}).get("confirmed_correct")
    )
    click.echo(f"\nActionable learning signals: {actionable} (ready for retrain when you enable it)")
