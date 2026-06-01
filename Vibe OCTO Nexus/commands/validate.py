from __future__ import annotations

import json
from pathlib import Path

import click
from pydantic import ValidationError

from core.validators import VibeBriefingOutput


@click.command()
@click.option("--json-file", required=True, type=click.Path(exists=True), help="Path to Vibe Briefing JSON output file")
@click.option("--strict", is_flag=True, help="Exit with code 1 on any validation warning, not just errors")
def validate(json_file, strict):
    """Validate a Vibe Briefing JSON output against the schema.

    Checks structure, required fields, type correctness, confidence scores,
    and execution readiness. Prints a summary of flags and blockers.

    Example:

        vibe-briefing validate --json-file output/q3_winback.json
    """
    data = json.loads(Path(json_file).read_text(encoding="utf-8"))

    try:
        output = VibeBriefingOutput.model_validate(data)
    except ValidationError as exc:
        click.echo(f"Validation FAILED — {exc.error_count()} error(s):", err=True)
        for error in exc.errors():
            loc = ".".join(str(l) for l in error["loc"])
            click.echo(f"  {loc}: {error['msg']}", err=True)
        raise SystemExit(1)

    meta = output.brief_metadata
    readiness = output.execution_readiness
    flags = output.qc_flags

    click.echo(f"Validation passed: {json_file}")
    click.echo(f"  Brief:        {meta.brief_name} ({meta.brief_id})")
    click.echo(f"  Portfolio:    {meta.portfolio} | Objective: {meta.campaign_objective}")
    click.echo(f"  Channels:     {', '.join(meta.medium)}")
    click.echo(f"  Completeness: {readiness.completeness_score:.0f}%")
    click.echo(f"  Confidence:   {readiness.overall_confidence:.2f}")
    click.echo(f"  Ready:        {readiness.ready_to_execute}")

    conditions = output.audience.primary_segment.data_rules.conditions
    click.echo(f"  Conditions:   {len(conditions)}")
    click.echo(f"  Exclusions:   {len(output.audience.exclusions)}")

    has_warnings = False

    if readiness.blockers:
        has_warnings = True
        click.echo(f"\nBlockers ({len(readiness.blockers)}):", err=True)
        for b in readiness.blockers:
            click.echo(f"  - {b}", err=True)

    if flags.ambiguities:
        has_warnings = True
        click.echo(f"\nAmbiguities ({len(flags.ambiguities)}):")
        for a in flags.ambiguities:
            click.echo(f"  [{a.severity.upper()}] {a.field}: {a.issue}")

    if flags.glossary_misses:
        has_warnings = True
        click.echo(f"\nGlossary misses ({len(flags.glossary_misses)}):")
        for m in flags.glossary_misses:
            click.echo(f"  '{m.business_term}' → closest: '{m.closest_match}' | {m.action_required}")

    if flags.low_confidence_rules:
        has_warnings = True
        click.echo(f"\nLow confidence rules ({len(flags.low_confidence_rules)}):")
        for r in flags.low_confidence_rules:
            click.echo(f"  {r.condition_id} (score={r.confidence_score:.2f}): {r.reason}")

    if not readiness.ready_to_execute:
        click.echo("\nNot ready to execute — resolve blockers before running downstream tools.", err=True)
        raise SystemExit(1)

    if strict and has_warnings:
        raise SystemExit(1)
