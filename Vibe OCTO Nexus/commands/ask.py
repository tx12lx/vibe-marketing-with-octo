from __future__ import annotations

import json
import os
from pathlib import Path

import click
from dotenv import load_dotenv

from core.campaign_resolver import CampaignResolver
from core.feedback_schema import create_entry, append_to_brief
from core.intent_router import IntentRouter


@click.command()
@click.argument("question")
@click.option(
    "--glossary",
    default="glossary/aal_test.json",
    envvar="VIBE_GLOSSARY",
    show_default=True,
    help="Path to the portfolio glossary (used to locate brief JSONs)",
)
@click.option(
    "--briefs-dir",
    default=None,
    help="Directory of brief JSON files (default: <glossary_dir>/briefs)",
)
def ask(question, glossary, briefs_dir):
    """Answer a natural language question about a campaign.

    Vibe Briefing routes your question to the right tool, finds the
    relevant campaign brief, and returns the appropriate output.

    Examples:

    \b
        vibe-briefing ask "how many customers are eligible for a TELUS AAL email campaign?"
        vibe-briefing ask "what are the criteria for AAL Monthly EM?"
        vibe-briefing ask "is the Koodo AAL SHS brief complete?"
    """
    load_dotenv()

    glossary_path = Path(glossary)
    briefs_path = Path(briefs_dir) if briefs_dir else glossary_path.parent / "briefs"

    resolver = CampaignResolver(briefs_path)
    known = resolver.list_campaigns()

    # Step 1: Route intent
    click.echo(f"\nQuestion: {question}")
    click.echo("Routing...")

    router = IntentRouter()
    try:
        intent = router.route(question, known_campaigns=known)
    except Exception as exc:
        click.echo(f"Routing failed: {exc}", err=True)
        return

    action = intent.get("action", "general_info")
    confidence = intent.get("confidence", 0.0)
    click.echo(f"Intent: {action} (confidence {confidence:.0%})")

    # Step 2: Resolve campaign
    hint_parts = [
        intent.get("campaign_hint") or "",
        intent.get("brand") or "",
        intent.get("portfolio") or "",
        intent.get("medium") or "",
    ]
    hint = " ".join(p for p in hint_parts if p).strip()

    if not hint:
        click.echo("\nCould not identify a specific campaign from your question.")
        if known:
            click.echo("Known campaigns:\n  " + "\n  ".join(known))
        else:
            click.echo("No campaigns learned yet. Run: vibe-briefing learn")
        return

    brief = resolver.find(hint)
    if brief is None:
        click.echo(f"\nNo brief found matching: '{hint}'")
        if known:
            click.echo("Known campaigns:\n  " + "\n  ".join(known))
        else:
            click.echo("No campaigns learned yet. Run: vibe-briefing learn")
        return

    campaign_id = brief.get("campaign", {}).get("id", "unknown")
    click.echo(f"Campaign: {campaign_id}\n")

    # Step 3: Execute action
    result = _dispatch(action, brief, question)

    # Step 4: Log to feedback_log (wired, not yet used for retraining)
    brief_file = resolver.find_path(campaign_id)
    if brief_file:
        entry = create_entry(
            source_tool="ask",
            action=action,
            human_query=question,
            result=result,
        )
        try:
            append_to_brief(brief_file, entry)
        except Exception:
            pass  # feedback logging is non-blocking


_DEFAULT_EXCLUSION_STEPS = [
    {
        "type": "exclude",
        "category": "default",
        "term": "Non-primary subscriber scrub",
        "logic": "Exclude subscribers who are not the primary subscriber on the account",
        "canonical_filter": "NON_PRIMARY_SUBSCRIBER",
    },
    {
        "type": "exclude",
        "category": "default",
        "term": "Stop sell scrub",
        "logic": "Exclude customers whose account or line is flagged for stop sell",
        "canonical_filter": "STOP_SELL",
    },
    {
        "type": "exclude",
        "category": "default",
        "term": "Universal Control Group (UCG) scrub",
        "logic": "Exclude customers assigned to the universal control group",
        "canonical_filter": "UCG",
    },
    {
        "type": "exclude",
        "category": "default",
        "term": "CASTL compliance scrub",
        "logic": "Apply CASTL compliance suppression rules per regulatory requirements",
        "canonical_filter": "CASTL_COMPLIANCE",
    },
]

_CHANNEL_EXCLUSION_STEPS = {
    "EM": [
        {
            "type": "exclude",
            "category": "channel",
            "term": "DNC scrub — email opt-out",
            "logic": "Exclude customers who have opted out of email marketing communications",
            "canonical_filter": "DNC_EMAIL",
        },
        {
            "type": "exclude",
            "category": "channel",
            "term": "Email deduplication",
            "logic": "Deduplicate at the email address level, retaining one record per unique email address",
            "canonical_filter": "EMAIL_DEDUP",
        },
    ],
    "OB": [
        {
            "type": "exclude",
            "category": "channel",
            "term": "DNC scrub — voice",
            "logic": "Exclude customers who have opted out of outbound voice contact",
            "canonical_filter": "DNC_VOICE",
        },
    ],
    "DM": [
        {
            "type": "exclude",
            "category": "channel",
            "term": "DNC scrub — direct mail",
            "logic": "Exclude customers who have opted out of direct mail",
            "canonical_filter": "DNC_MAIL",
        },
    ],
}


def _dispatch(action: str, brief: dict, question: str) -> dict:
    if action == "size_audience":
        return _handle_sizing(brief)
    if action == "get_criteria":
        return _handle_criteria(brief)
    if action == "validate_brief":
        return _handle_validate(brief)
    if action == "regenerate_brief":
        return _handle_regenerate(brief)
    return _handle_general(brief)


def _handle_sizing(brief: dict) -> dict:
    campaign = brief.get("campaign", {})
    targeting = brief.get("targeting", {})
    flags = brief.get("data_brief_flags", {})
    medium = campaign.get("medium", [])

    waterfall = []
    step = 1

    for c in targeting.get("include", []):
        waterfall.append({
            "step": step,
            "type": "include",
            "category": "campaign",
            "term": c["term"],
            "logic": c.get("logic", ""),
            "threshold": c.get("threshold", ""),
            "confidence_score": c.get("confidence_score", 0),
        })
        step += 1

    for c in targeting.get("exclude", []):
        entry = {
            "step": step,
            "type": "exclude",
            "category": "campaign",
            "term": c["term"],
            "logic": c.get("logic", ""),
            "threshold": c.get("threshold", ""),
            "confidence_score": c.get("confidence_score", 0),
        }
        if c.get("lookback_days") is not None:
            entry["lookback_days"] = c["lookback_days"]
        waterfall.append(entry)
        step += 1

    for d in _DEFAULT_EXCLUSION_STEPS:
        waterfall.append({"step": step, **d})
        step += 1

    for m in medium:
        for d in _CHANNEL_EXCLUSION_STEPS.get(m, []):
            waterfall.append({"step": step, **d})
            step += 1

    # Map campaign term → step number for annotating quality flags
    term_to_step = {
        entry["term"].lower(): entry["step"]
        for entry in waterfall
        if entry.get("category") == "campaign"
    }

    def _annotate_flag(entry) -> dict:
        if isinstance(entry, dict):
            # Structured format: {criterion_ref, flag}
            criterion_ref = entry.get("criterion_ref")
            flag_text = entry.get("flag", "")
            step_ref = term_to_step.get(criterion_ref.lower()) if criterion_ref else None
            result = {"flag": flag_text, "sizing_relevant": step_ref is not None}
            if criterion_ref:
                result["criterion_ref"] = criterion_ref
        else:
            # Legacy flat string format
            flag_text = entry
            lower = flag_text.lower()
            step_ref = next(
                (s for term, s in term_to_step.items() if term in lower),
                None,
            )
            result = {"flag": flag_text, "sizing_relevant": step_ref is not None}
        if step_ref is not None:
            result["step_ref"] = step_ref
        return result

    output = {
        "schema_version": "2.0",
        "source_tool": "vibe-briefing",
        "campaign": {
            "id": campaign.get("id"),
            "portfolio": campaign.get("portfolio"),
            "brand": campaign.get("brand"),
            "medium": medium,
            "cadence": campaign.get("cadence") or None,
        },
        "waterfall": waterfall,
    }

    seg = brief.get("segmentation", {})
    if seg:
        output["segmentation"] = {"sizing_use": False, **seg}

    ambiguities = flags.get("ambiguities", [])
    missing = flags.get("missing_required_fields", [])
    if ambiguities or missing:
        output["quality_flags"] = {
            "ambiguities": [_annotate_flag(a) for a in ambiguities],
            "missing_required_fields": missing,
        }

    click.echo(json.dumps(output, indent=2))
    return output


def _handle_criteria(brief: dict) -> dict:
    campaign_id = brief.get("campaign", {}).get("id", "")
    summary = brief.get("campaign_summary", "")
    targeting = brief.get("targeting", {})
    include = targeting.get("include", [])
    exclude = targeting.get("exclude", [])

    click.echo(f"Campaign: {campaign_id}")
    if summary:
        click.echo(f"\nSummary:\n{summary}")

    click.echo(f"\nInclude criteria ({len(include)}):")
    for c in include:
        threshold = f" [{c['threshold']}]" if c.get("threshold") else ""
        conf = c.get("confidence_score", 0)
        click.echo(f"  + {c['term']}{threshold} (confidence {conf:.0%})")
        click.echo(f"    {c.get('logic', '')}")

    click.echo(f"\nExclude criteria ({len(exclude)}):")
    for c in exclude:
        lb = f", lookback {c['lookback_days']}d" if c.get("lookback_days") else ""
        conf = c.get("confidence_score", 0)
        click.echo(f"  - {c['term']}{lb} (confidence {conf:.0%})")
        click.echo(f"    {c.get('logic', '')}")

    defaults = brief.get("default_exclusions", {})
    click.echo(f"\nDefault exclusions (always applied):")
    dnc = defaults.get("dnc", {})
    if dnc.get("applied"):
        click.echo(f"  - DNC: {dnc.get('scope', '')}")
    for rule in ("non_primary_subscriber", "stop_sell", "castl_compliance"):
        if defaults.get(rule):
            click.echo(f"  - {rule}")

    return {"status": "ok", "include_count": len(include), "exclude_count": len(exclude)}


def _handle_validate(brief: dict) -> dict:
    assistant = brief.get("connectors", {}).get("brief_assistant", {})
    score = assistant.get("completeness_score", 0)
    checks = assistant.get("required_fields_check", {})
    failed = assistant.get("failed_checks", [])
    guidance = assistant.get("guidance", [])

    click.echo(f"Completeness score: {score}%")
    click.echo(f"\nRequired field checks:")
    for field, passed in checks.items():
        icon = "PASS" if passed else "FAIL"
        click.echo(f"  [{icon}] {field}")

    if guidance:
        click.echo(f"\nGuidance ({len(guidance)} item(s)):")
        for g in guidance:
            click.echo(f"  ! {g}")

    return {"status": "ok", "completeness_score": score, "failed_checks": failed}


def _handle_regenerate(brief: dict) -> dict:
    regen = brief.get("connectors", {}).get("brief_regeneration", {})
    click.echo(json.dumps(regen.get("structured_sections", {}), indent=2))
    return {"status": "ok"}


def _handle_general(brief: dict) -> dict:
    summary = brief.get("campaign_summary", "No summary available.")
    campaign = brief.get("campaign", {})
    click.echo(f"Portfolio: {campaign.get('portfolio')} | Brand: {campaign.get('brand')} | "
               f"Medium: {campaign.get('medium')} | Cadence: {campaign.get('cadence')}")
    click.echo(f"\n{summary}")
    return {"status": "ok"}
