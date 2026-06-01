from __future__ import annotations

import json
from pathlib import Path

import click
from dotenv import load_dotenv
from pydantic import ValidationError

from core.claude_client import ClaudeClient
from core.glossary import GlossaryManager
from core.json_formatter import add_translation_metadata, save_output
from core.validators import VibeBriefingOutput


@click.command()
@click.option("--brief", required=True, type=click.Path(exists=True), help="Path to brief text file")
@click.option("--glossary", required=True, type=click.Path(exists=True), help="Path to portfolio glossary JSON")
@click.option("--bq-schema", default=None, type=click.Path(), help="Path to BQ schema JSON (default: config/bq_schema.json)")
@click.option("--output", required=True, type=click.Path(), help="Output path for translated JSON")
@click.option("--campaign-id", default=None, help="Campaign ID (for metadata)")
@click.option("--portfolio", default=None, help="Portfolio name override")
@click.option("--purpose", default=None, help="Campaign purpose override (winback/upsell/retention/acquisition)")
@click.option("--cadence", default=None, help="Cadence override (monthly/weekly/daily)")
@click.option("--medium", default=None, multiple=True, help="Channel(s): sms, email, outbound_call (repeatable)")
def analyze(brief, glossary, bq_schema, output, campaign_id, portfolio, purpose, cadence, medium):
    """Translate a campaign brief into the standard Vibe Briefing JSON contract.

    Reads the brief from a text file, consults the portfolio glossary, and uses
    Claude to produce a fully-structured JSON with audience rules, SQL snippets,
    QC flags, and downstream tool readiness indicators.

    Example:

        vibe-briefing analyze \\
          --brief briefs/q3_winback.txt \\
          --glossary glossary/postpaid_winback.json \\
          --output output/q3_winback.json
    """
    load_dotenv()

    config_dir = Path(__file__).parent.parent / "config"

    schema_path = (
        Path(bq_schema) if bq_schema else config_dir / "bq_schema.json"
    )
    if not schema_path.exists():
        raise click.ClickException(f"BQ schema not found at {schema_path}")

    system_prompt = (config_dir / "prompts" / "system_prompt.txt").read_text(encoding="utf-8")
    translate_prompt = (config_dir / "prompts" / "translate_prompt.txt").read_text(encoding="utf-8")
    bq_schema_data = json.loads(schema_path.read_text(encoding="utf-8"))

    brief_content = Path(brief).read_text(encoding="utf-8").strip()
    if not brief_content:
        raise click.ClickException(f"Brief file is empty: {brief}")

    glossary_mgr = GlossaryManager(glossary)
    glossary_meta = glossary_mgr.data.get("glossary_metadata", {})
    glossary_version = glossary_meta.get("version", "unknown")

    metadata = {
        "campaign_id": campaign_id or "unspecified",
        "portfolio": portfolio or glossary_meta.get("portfolio", "unspecified"),
        "purpose": purpose or "",
        "cadence": cadence or "",
        "medium": list(medium) if medium else [],
    }

    claude = ClaudeClient()

    click.echo(f"Translating brief: {brief}")
    click.echo(f"  Glossary: {glossary} ({len(glossary_mgr.terms)} terms)")

    result = claude.translate_brief(
        brief_content=brief_content,
        system_prompt=system_prompt,
        translate_prompt_template=translate_prompt,
        bq_schema=json.dumps(bq_schema_data, indent=2),
        glossary_json=glossary_mgr.to_json_string(),
        metadata=metadata,
    )

    result = add_translation_metadata(result, glossary_version=glossary_version)

    # Validate against Pydantic schema
    validation_ok = True
    try:
        validated = VibeBriefingOutput.model_validate(result)
        click.echo(f"  Validation: passed")
        click.echo(f"  {validated.readiness_summary()}")

        high_ambiguities = [
            a for a in validated.qc_flags.ambiguities if a.severity == "high"
        ]
        if high_ambiguities:
            click.echo(f"  WARNING: {len(high_ambiguities)} high-severity ambiguity flag(s):", err=True)
            for a in high_ambiguities:
                click.echo(f"    [{a.flag_id}] {a.field}: {a.issue}", err=True)

        glossary_misses = validated.qc_flags.glossary_misses
        if glossary_misses:
            click.echo(f"  INFO: {len(glossary_misses)} term(s) not in glossary — review qc_flags.glossary_misses")

    except ValidationError as exc:
        validation_ok = False
        click.echo(f"  Validation: FAILED ({exc.error_count()} error(s))", err=True)
        for error in exc.errors()[:5]:
            click.echo(f"    {'.'.join(str(l) for l in error['loc'])}: {error['msg']}", err=True)
        click.echo("  Saving raw output anyway — fix validation errors before using with downstream tools.", err=True)

    out_path = save_output(result, output)
    click.echo(f"Output saved to: {out_path}")

    if not validation_ok:
        raise SystemExit(1)
