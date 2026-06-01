from __future__ import annotations

import json
import textwrap
from pathlib import Path

import click
from dotenv import load_dotenv

from connectors import enrich as enrich_with_connectors
from core.claude_client import ClaudeClient
from core.glossary import GlossaryManager


_PORTFOLIO_DESCRIPTION = "Postpaid mobility customers targeted for Add-A-Line cross-sell"

# Max terms to inject into generation context — keep prompt focused
_MAX_CONTEXT_TERMS = 30


def _build_glossary_context(glossary: GlossaryManager) -> tuple[str, int]:
    """Build a concise term summary for the generation prompt.

    Prioritises terms with higher frequency (seen in more briefs) and
    higher confidence scores — these are the most reliable patterns.
    """
    terms = glossary.terms
    if not terms:
        return "(no learned terms yet)", 0

    scored = sorted(
        terms.items(),
        key=lambda kv: (
            kv[1].get("frequency_in_briefs", 0),
            kv[1].get("confidence_score", 0),
        ),
        reverse=True,
    )[:_MAX_CONTEXT_TERMS]

    lines = []
    for term, entry in scored:
        ctype = entry.get("criterion_type", "")
        logic = entry.get("logic", entry.get("meaning", ""))
        threshold = entry.get("threshold")
        freq = entry.get("frequency_in_briefs", 0)
        line = f"- [{ctype}] {term}: {logic}"
        if threshold:
            line += f" (threshold: {threshold})"
        if freq > 1:
            line += f" [seen in {freq} briefs]"
        lines.append(line)

    return "\n".join(lines), len(terms)


def _build_summaries_context(glossary: GlossaryManager, limit: int = 5) -> str:
    """Extract recent campaign summaries from the glossary for reference patterns."""
    summaries = glossary.data.get("campaign_summaries", {})
    if not summaries:
        return "(no campaign summaries learned yet)"
    items = list(summaries.items())[-limit:]
    lines = [f"- {cid}: {summary}" for cid, summary in items]
    return "\n".join(lines)


@click.command()
@click.option(
    "--prompt", "-p", required=True,
    help="Natural language request, e.g. 'Generate a Telus AAL monthly OB brief for French-speaking customers'",
)
@click.option(
    "--portfolio", default="AAL", show_default=True,
    help="Portfolio to generate brief for",
)
@click.option(
    "--glossary", required=True, type=click.Path(exists=True),
    help="Path to portfolio glossary JSON (built with vibe-briefing learn)",
)
@click.option(
    "--output", default=None, type=click.Path(),
    help="Output path for generated brief JSON (default: output/generated/<slug>.json)",
)
def chat(prompt, portfolio, glossary, output):
    """Natural language brief generation — describe what you need, get a brief.

    Draws on the portfolio glossary to generate a standardized brief template
    using known campaign patterns. The output JSON passes through all connectors
    so downstream tools (sizing, brief regeneration, brief assistant) are
    immediately populated.

    Use 'vibe-briefing feedback' to submit corrections — every correction
    becomes new knowledge in the glossary.

    Examples:

    \\b
        vibe-briefing chat \\
          --prompt "Generate a Telus AAL bi-weekly OB brief for English postpaid customers, AAL reco ranks 1-5" \\
          --glossary glossary/aal.json

        vibe-briefing chat \\
          --prompt "Help me create a Koodo AAL email campaign for customers with high churn risk" \\
          --glossary glossary/aal.json \\
          --output output/generated/koodo_aal_churn_email.json
    """
    load_dotenv()

    config_dir = Path(__file__).parent.parent / "config"
    generate_prompt = (config_dir / "prompts" / "generate_prompt.txt").read_text(encoding="utf-8")

    glossary_mgr = GlossaryManager(glossary)
    glossary_version = glossary_mgr.data.get("glossary_metadata", {}).get("version", "1.0")

    click.echo(f"Portfolio:  {portfolio}")
    click.echo(f"Glossary:   {glossary} ({len(glossary_mgr.terms)} terms)")
    click.echo(f"Request:    {prompt}")
    click.echo()

    glossary_context, term_count = _build_glossary_context(glossary_mgr)
    summaries_context = _build_summaries_context(glossary_mgr)

    claude = ClaudeClient()

    click.echo("Generating brief...")
    result = claude.generate_brief(
        request=prompt,
        generate_prompt_template=generate_prompt,
        portfolio=portfolio,
        portfolio_description=_PORTFOLIO_DESCRIPTION,
        glossary_terms_context=glossary_context,
        term_count=term_count,
        campaign_summaries=summaries_context,
    )

    # Run all connectors
    result = enrich_with_connectors(result, glossary_mgr.data, glossary_version)

    # Determine output path
    if output is None:
        slug = prompt[:50].lower()
        for ch in " /\\:*?\"<>|":
            slug = slug.replace(ch, "_")
        out_path = Path("output") / "generated" / f"{slug}.json"
    else:
        out_path = Path(output)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Print summary
    campaign = result.get("campaign", {})
    targeting = result.get("targeting", {})
    assistant = result.get("connectors", {}).get("brief_assistant", {})
    sizing = result.get("connectors", {}).get("sizing_tool", {})
    flags = result.get("data_brief_flags", {})

    click.echo()
    click.echo("=" * 64)
    click.echo("GENERATED BRIEF")
    click.echo("=" * 64)
    click.echo(f"  Campaign:     {campaign.get('id', 'TBC')}")
    click.echo(f"  Brand:        {campaign.get('brand', 'TBC')}")
    click.echo(f"  Medium:       {', '.join(campaign.get('medium', []))}")
    click.echo(f"  Cadence:      {campaign.get('cadence', 'TBC')}")
    click.echo(f"  Sub-product:  {result.get('offer', {}).get('sub_product') or 'None'}")
    click.echo()
    click.echo(f"  Include criteria: {len(targeting.get('include', []))}")
    click.echo(f"  Exclude criteria: {len(targeting.get('exclude', []))}")
    click.echo()
    click.echo(f"  Completeness:     {assistant.get('completeness_score', '?')}%")
    click.echo(f"  Sizing tool:      {sizing.get('status', '?')}")
    click.echo()

    summary = result.get("campaign_summary", "")
    if summary:
        click.echo("  Summary:")
        for line in textwrap.wrap(summary, width=60):
            click.echo(f"    {line}")
    click.echo()

    missing = flags.get("missing_required_fields", [])
    ambiguities = flags.get("ambiguities", [])
    if missing:
        click.echo(f"  Missing ({len(missing)}):")
        for f in missing:
            click.echo(f"    [MISSING] {f}")
    if ambiguities:
        click.echo(f"  Needs clarification ({len(ambiguities)}):")
        for a in ambiguities[:5]:
            click.echo(f"    [?] {a}")

    click.echo()
    click.echo(f"  Saved to: {out_path}")
    click.echo()
    click.echo(
        "  To submit corrections: vibe-briefing feedback "
        f"--brief {out_path} --notes '...' --glossary {glossary}"
    )
