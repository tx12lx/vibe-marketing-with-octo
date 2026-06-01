from __future__ import annotations

import json
import os
from pathlib import Path

import click
from dotenv import load_dotenv

from connectors import enrich as enrich_with_connectors
from core.bq_client import BQClient
from core.brief_fetcher import BriefFetcher, BriefFetchError
from core.claude_client import ClaudeClient
from core.glossary import GlossaryManager


def _load_config(config_dir: Path) -> str:
    return (config_dir / "prompts" / "learn_prompt.txt").read_text(encoding="utf-8")


@click.command()
@click.option("--portfolio", default=None, help="Portfolio camp_id to filter briefs (e.g. AAL). Mutually exclusive with --campaign-name.")
@click.option("--campaign-name", default=None, help="Exact campaign name to study (e.g. 'AAL Monthly EM'). Fetches all deployments of this campaign.")
@click.option("--bq-table", envvar="BQ_TABLE", required=True, help="BigQuery table as project.dataset.table")
@click.option("--limit", default=200, show_default=True, help="Number of training briefs when using --portfolio (ignored with --campaign-name)")
@click.option("--sort-field", default="list_pull_date", show_default=True, help="BQ field to sort by (portfolio mode only)")
@click.option("--sort-order", default="ASC", show_default=True, type=click.Choice(["ASC", "DESC"], case_sensitive=False), help="ASC = oldest first (training); DESC = most recent first (testing)")
@click.option("--held-out", default=42, show_default=True, help="Number of most recent briefs held out (portfolio mode only)")
@click.option("--output", required=True, type=click.Path(), help="Output path for glossary JSON file")
@click.option("--random", "use_random", is_flag=True, help="Select briefs randomly (portfolio mode only)")
@click.option("--resume", is_flag=True, help="Resume from a previous interrupted run")
@click.option("--concurrency", default=1, show_default=True, help="Number of concurrent Claude API calls")
def learn(portfolio, campaign_name, bq_table, limit, sort_field, sort_order, held_out, output, use_random, resume, concurrency):
    """Build a portfolio glossary by analyzing historical campaign briefs.

    Two modes:
      --portfolio AAL          : fetches --limit briefs filtered by camp_id
      --campaign-name "AAL Monthly EM" : fetches all deployments of that exact campaign

    Use --resume to continue after interruption.
    """
    load_dotenv()

    config_dir = Path(__file__).parent.parent / "config"
    learn_prompt_template = _load_config(config_dir)

    bq = BQClient(
        project=os.getenv("BQ_PROJECT", bq_table.split(".")[0]),
        credentials_path=os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
    )
    claude = ClaudeClient()
    fetcher = BriefFetcher()
    glossary = GlossaryManager(output)

    # Resume: load processed campaign IDs from checkpoint
    output_path = Path(output)
    checkpoint_path = output_path.with_suffix(".progress.json")
    processed_ids: set[str] = set()
    if resume and checkpoint_path.exists():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        processed_ids = set(checkpoint.get("processed_ids", []))
        click.echo(f"Resuming — {len(processed_ids)} briefs already processed.")

    if not portfolio and not campaign_name:
        raise click.UsageError("Provide either --portfolio or --campaign-name.")

    if campaign_name:
        click.echo(f"Fetching all deployments of campaign '{campaign_name}' from {bq_table}...")
        briefs = bq.get_briefs_by_campaign_name(table=bq_table, campaign_name=campaign_name)
        held_out = 0  # no held-out set when studying a specific campaign
    else:
        click.echo(f"Fetching {limit} briefs for portfolio '{portfolio}' from {bq_table}...")
        briefs = bq.get_historical_briefs(
            table=bq_table,
            camp_id=portfolio,
            limit=limit,
            sort_field=sort_field,
            sort_order=sort_order,
            random=use_random,
        )

    click.echo(f"Retrieved {len(briefs)} briefs from BigQuery.")

    if not briefs:
        label = f"campaign '{campaign_name}'" if campaign_name else f"portfolio '{portfolio}'"
        raise click.ClickException(
            f"No briefs found for {label} in {bq_table}. "
            "Check the name and BQ table configuration."
        )

    stats = {"processed": 0, "skipped_resume": 0, "failed": 0, "empty": 0, "new_terms": 0}

    for brief_row in briefs:
        campaign_name_val = str(brief_row.get("campaign", ""))
        sub_camp = str(brief_row.get("sub_camp_id") or "").strip()
        brief_url = str(brief_row.get("databrief_link") or "").strip()

        # In campaign-name mode all rows share the same campaign name — use the
        # brief URL as the unique dedup key so every deployment is processed.
        # In portfolio mode the campaign name is already unique per row.
        dedup_key = brief_url if campaign_name else campaign_name_val
        campaign_id = f"{campaign_name_val} [{sub_camp}]" if sub_camp else campaign_name_val

        if dedup_key in processed_ids:
            stats["skipped_resume"] += 1
            continue

        if not brief_url:
            stats["empty"] += 1
            continue

        try:
            brief_content = fetcher.fetch(brief_url)
        except BriefFetchError as exc:
            click.echo(f"  [skip] {campaign_id}: {exc}", err=True)
            stats["failed"] += 1
            continue

        if not brief_content.strip():
            stats["empty"] += 1
            continue

        metadata = {
            "campaign_id": campaign_id,
            "portfolio": portfolio or brief_row.get("camp_id", ""),
            "camp_id": brief_row.get("camp_id", ""),
            "sub_camp_id": brief_row.get("sub_camp_id", "") or "",
            "purpose": brief_row.get("campaign_purpose", ""),
            "medium": brief_row.get("medium", ""),
            "cadence": brief_row.get("cadence", ""),
            "target_base": brief_row.get("target_base", ""),
            "primary_products": brief_row.get("primary_products", ""),
        }

        try:
            result = claude.learn_from_brief(
                brief_content=brief_content,
                learn_prompt_template=learn_prompt_template,
                metadata=metadata,
            )
            # Merge terms into glossary before enriching so connector has latest mappings
            added = glossary.merge_from_brief_json(result) if isinstance(result, dict) else 0
            summary = result.get("campaign_summary", "") if isinstance(result, dict) else ""
            if summary and campaign_id:
                glossary.add_campaign_summary(campaign_id, summary)
            stats["new_terms"] += added

            # Wrap in master contract with connector outputs
            glossary_version = glossary.data.get("glossary_metadata", {}).get("version", "1.0")
            result = enrich_with_connectors(result, glossary.data, glossary_version)

            # Save per-brief JSON — include sub_camp_id in filename for uniqueness
            safe_name = campaign_id.replace("/", "_").replace(" ", "_").replace("[", "").replace("]", "")[:60]
            brief_out = output_path.parent / "briefs" / f"{safe_name}.json"
            brief_out.parent.mkdir(parents=True, exist_ok=True)
            brief_out.write_text(
                json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            include_count = len(result.get("targeting", {}).get("include", [])) if isinstance(result, dict) else 0
            exclude_count = len(result.get("targeting", {}).get("exclude", [])) if isinstance(result, dict) else 0
            flags = result.get("data_brief_flags", {}) if isinstance(result, dict) else {}
            flag_notes = flags.get("missing_required_fields", []) + flags.get("ambiguities", [])
            connectors_status = {
                k: v.get("status", "?")
                for k, v in result.get("connectors", {}).items()
            }

            click.echo(
                f"  [{stats['processed'] + 1}] {campaign_id} "
                f"— {include_count} include / {exclude_count} exclude criteria, {added} new glossary terms"
            )
            click.echo(f"      connectors: {connectors_status}")
            if summary:
                click.echo(f"      {summary}")
            for note in flag_notes:
                click.echo(f"      [FLAG] {note}")
        except Exception as exc:
            click.echo(f"  [error] {campaign_id}: {exc}", err=True)
            stats["failed"] += 1
            continue

        processed_ids.add(dedup_key)
        stats["processed"] += 1

        # Checkpoint every 10 briefs so interrupted runs can resume
        if stats["processed"] % 10 == 0:
            checkpoint_path.write_text(
                json.dumps({"processed_ids": list(processed_ids)}),
                encoding="utf-8",
            )
            glossary.save(
                portfolio=portfolio or campaign_name or "unknown",
                built_from=stats["processed"],
                held_out=held_out,
            )

    portfolio_label = portfolio or campaign_name or "unknown"
    glossary.save(portfolio=portfolio_label, built_from=stats["processed"], held_out=held_out)

    if checkpoint_path.exists():
        checkpoint_path.unlink()

    click.echo(f"\nGlossary saved to: {output}")
    click.echo(f"  Briefs processed:    {stats['processed']}")
    click.echo(f"  Briefs skipped:      {stats['skipped_resume']} (already done) / {stats['failed']} (errors) / {stats['empty']} (empty)")
    click.echo(f"  New terms extracted: {stats['new_terms']}")
    click.echo(f"  Total glossary terms: {len(glossary.terms)}")
    click.echo(f"  Held-out (test) set:  {held_out} briefs (not touched)")
