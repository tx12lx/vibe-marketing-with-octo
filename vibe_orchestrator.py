"""
vibe_orchestrator.py — Vibe Marketing with OCTO
Central decoupled router.

Design principle: this file is the only wiring layer. It knows which agents
exist and which workflow maps to which agent method — nothing else. Agent cores
are fully independent and test in isolation.

Scaling to new agents requires only two changes here:
  1. Import the new agent class.
  2. Add it to _AGENT_REGISTRY.
Zero changes to Nexus, Quant, or pydantic_schemas.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import warnings
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent
_NEXUS_DIR = _ROOT / "Vibe OCTO Nexus"
_QUANT_DIR = _ROOT / "Vibe OCTO Quant"
_BRIEFING_DIR = _ROOT / "Vibe OCTO Briefing"
_SCHEMA_DISCOVERY_DIR = _ROOT / "schema_discovery"
_GLOSSARY_PATH = _ROOT / "glossary.json"
_QUERY_CATALOG_PATH = _ROOT / "query_catalog.json"

# Keywords that trigger dynamic glossary/catalog injection.
_GLOSSARY_KEYWORDS = {"PFE", "KI", "TWA", "AALBAU"}

# Load both agent environments before any agent code is imported.
# override=False means the first file wins on conflicts.
load_dotenv(_NEXUS_DIR / ".env")
load_dotenv(_QUANT_DIR / ".env", override=False)

for _p in [str(_ROOT), str(_NEXUS_DIR), str(_QUANT_DIR), str(_BRIEFING_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from nexus_agent import NexusAgent  # noqa: E402
from quant_agent import QuantAgent  # noqa: E402
from briefing_agent import BriefingAgent  # noqa: E402
from pydantic_schemas import BriefingOutput, NexusErrorPayload, QuantAuditLog, UniversalJSONSpec  # noqa: E402
from schema_discovery.discovery_layer import SchemaDiscoveryLayer  # noqa: E402
from knowledge_base.ingester import KnowledgeBaseIngester  # noqa: E402
from knowledge_base.tier_index import GoldTierIndex  # noqa: E402


def _silence_google_noise() -> None:
    """Mute Google Cloud SDK and urllib3 log chatter below ERROR level."""
    for name in (
        "google", "google.auth", "google.auth.transport",
        "google.cloud", "urllib3", "grpc",
    ):
        logging.getLogger(name).setLevel(logging.ERROR)
    warnings.filterwarnings("ignore", category=UserWarning)
    warnings.filterwarnings("ignore", category=ResourceWarning)


# ---------------------------------------------------------------------------
# Dynamic knowledge config loaders
# ---------------------------------------------------------------------------

def _load_glossary() -> dict:
    try:
        return json.loads(_GLOSSARY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load_query_catalog() -> list:
    try:
        return json.loads(_QUERY_CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _build_dynamic_context(query_text: str) -> str:
    """Scan the user prompt for known glossary keywords and assemble an injection block.

    Prints [LEARNING DISCOVERY] headers for each asset pulled. Returns an empty
    string when no keywords are found so callers can skip injection cheaply.
    """
    text_upper = query_text.upper()
    detected = [k for k in _GLOSSARY_KEYWORDS if k in text_upper]
    if not detected:
        return ""

    glossary = _load_glossary()
    catalog = _load_query_catalog()
    if not glossary:
        return ""

    print(
        f"\n  [LEARNING DISCOVERY] -> Dynamically pulled glossary mappings for keys: "
        f"{', '.join(detected)}"
    )

    parts: list[str] = [
        "=== ACTIVE SESSION GUARDRAILS (loaded from knowledge configs) ===\n\n"
    ]

    global_scope = glossary.get("GLOBAL_SCOPE", {})
    if global_scope:
        parts.append("GLOBAL SCOPE RULES:\n")
        for k, v in global_scope.items():
            parts.append(f"  {k}: {v}\n")
        parts.append("\n")

    acronyms = glossary.get("acronyms", {})
    matched_acronyms = {k: v for k, v in acronyms.items() if k in detected}
    if matched_acronyms:
        parts.append(
            "ACRONYM DEFINITIONS (authoritative — override any inferred meaning):\n"
        )
        for key, defn in matched_acronyms.items():
            parts.append(
                f"  {key} — Business Meaning: {defn.get('business_meaning', '')}\n"
            )
            if defn.get("special_notes"):
                parts.append(f"       Special Notes: {defn['special_notes']}\n")
            indicators = defn.get("database_indicators") or []
            if indicators:
                parts.append("       Database Indicators:\n")
                for ind in indicators:
                    parts.append(
                        f"         {ind['column']} = {ind['value']} ({ind['meaning']})\n"
                    )
        parts.append("\n")

    campaigns = glossary.get("campaigns", {})
    matched_campaigns = {k: v for k, v in campaigns.items() if k in detected}
    if matched_campaigns:
        parts.append("CAMPAIGN CONFIGURATIONS:\n")
        for key, cfg in matched_campaigns.items():
            parts.append(f"  {key}:\n")
            for ck, cv in cfg.items():
                parts.append(f"    {ck}: {cv}\n")
        parts.append("\n")

    if "AALBAU" in detected and catalog:
        blueprint = next(
            (q for q in catalog if q.get("target_campaign") == "AALBAU"), None
        )
        if blueprint:
            print(
                "  [LEARNING DISCOVERY] -> Injected structural SQL blueprint from Query Catalog."
            )
            parts.append(
                "SQL STRUCTURAL BLUEPRINT (authoritative reference template — "
                f"{blueprint.get('id')}):\n"
            )
            parts.append(f"  Intent  : {blueprint.get('intent')}\n")
            parts.append(f"  Sample  : {blueprint.get('sample_brief')}\n")
            parts.append(
                f"  SQL Template:\n{blueprint.get('sql_template', '')}\n\n"
            )

    print()
    return "".join(parts)


# ---------------------------------------------------------------------------
# Agent registry — the ONLY place agent classes are registered.
# Adding a new worker requires only: (1) implement BaseAgent, (2) add one line here.
# ---------------------------------------------------------------------------
_AGENT_REGISTRY: dict[str, type] = {
    "nexus": NexusAgent,
    "quant": QuantAgent,
    "briefing": BriefingAgent,
}


# ---------------------------------------------------------------------------
# Discrepancy audit display
# ---------------------------------------------------------------------------

def _print_discrepancy_audit(spec: UniversalJSONSpec) -> None:
    """Print advisory flags with structured formatting before any query executes.

    For logic_drift flags, appends a side-by-side comparison between the gold
    blueprint targeting summary and the current request's filters so the marketer
    can review the divergence before deciding to proceed.
    """
    flags = spec.discrepancy_flags
    if not flags:
        return

    sep = "=" * 68
    thin = "-" * 50

    print(f"\n{sep}")
    print("  [DISCREPANCY AUDIT]  Advisory Flags Detected Before Execution")
    print(sep)
    print()

    has_drift = any(f.startswith("logic_drift:") for f in flags)

    for flag in flags:
        colon_pos = flag.find(":")
        if colon_pos == -1:
            flag_label = "ADVISORY"
            flag_detail = flag
        else:
            flag_label = flag[:colon_pos].strip().upper().replace("_", " ")
            flag_detail = flag[colon_pos + 1:].strip()

        print(f"  ! [{flag_label}]")
        # Wrap detail lines at 64 chars for readability
        while len(flag_detail) > 64:
            break_at = flag_detail.rfind(" ", 0, 64)
            if break_at <= 0:
                break_at = 64
            print(f"      {flag_detail[:break_at]}")
            flag_detail = flag_detail[break_at:].lstrip()
        if flag_detail:
            print(f"      {flag_detail}")
        print()

    if has_drift:
        gold_summary = ""
        if spec.brief_agent_inputs:
            gold_summary = spec.brief_agent_inputs.get("targeting_summary", "")

        print(f"  {thin}")
        print("  LOGIC DRIFT — Targeting Summary Comparison")
        print(f"  {thin}")
        print()

        col_w = 30
        print(f"  {'GOLD BLUEPRINT (Historical)':<{col_w}}  CURRENT REQUEST FILTERS")
        print(f"  {'─' * col_w}  {'─' * col_w}")

        # Build left column: word-wrapped gold targeting summary
        gold_lines: list[str] = []
        if gold_summary:
            words = gold_summary.split()
            line = ""
            for word in words:
                candidate = (line + " " + word).strip() if line else word
                if len(candidate) <= col_w:
                    line = candidate
                else:
                    if line:
                        gold_lines.append(line)
                    line = word[:col_w]
            if line:
                gold_lines.append(line)
        else:
            gold_lines = ["(no gold blueprint available)"]

        # Build right column: filters then exclusion layers
        current_lines: list[str] = []
        for f in spec.filters or []:
            f_str = f.strip()
            while len(f_str) > col_w:
                current_lines.append(f_str[:col_w])
                f_str = f_str[col_w:]
            if f_str:
                current_lines.append(f_str)
        if spec.exclusion_layers:
            current_lines.append("-- Exclusions --")
            for excl in spec.exclusion_layers:
                e_str = excl.strip()
                while len(e_str) > col_w:
                    current_lines.append(e_str[:col_w])
                    e_str = e_str[col_w:]
                if e_str:
                    current_lines.append(e_str)

        max_rows = max(len(gold_lines), len(current_lines), 1)
        for i in range(max_rows):
            left = gold_lines[i] if i < len(gold_lines) else ""
            right = current_lines[i] if i < len(current_lines) else ""
            print(f"  {left:<{col_w}}  {right}")

        print()

    print(sep)
    print()


# ---------------------------------------------------------------------------
# Router — stateless, workflow-dispatch only
# ---------------------------------------------------------------------------

def route(
    nexus: NexusAgent,
    quant: QuantAgent,
    briefing: BriefingAgent,
    workflow: str,
    payload: dict,
    gold_index: GoldTierIndex,
    schema_snapshot: dict,
) -> tuple[Optional[QuantAuditLog], Optional[BriefingOutput]]:
    """Dispatch a classified request to the correct pipeline branch.

    workflow 'WORKFLOW_A': Ad-Hoc Exploratory Request — natural language audience
                           sizing. Runs a direct single-row count via Quant.
                           BriefingAgent is not invoked on this path.

    workflow 'WORKFLOW_B': Structured Campaign Execution Request — named campaign
                           brief. Builds a UniversalJSONSpec via NexusAgent (Pillar 3
                           Gateway), runs the discrepancy audit, dispatches to
                           Quant via audit_from_spec(), then fans out to
                           BriefingAgent for the campaign intelligence brief.

                           On NexusErrorPayload from Quant, falls back to the
                           existing route_with_retry() for one taxonomy-guided
                           correction attempt.

    Returns (QuantAuditLog | None, BriefingOutput | None).
    """
    if workflow == "WORKFLOW_A":
        request = nexus.build_sizing_request_from_nl(payload["query"])
        if request is None:
            return None, None
        result = quant.direct_count(request)
        if isinstance(result, QuantAuditLog):
            return result, None
        print(f"\n  [ERROR]: {result.error_summary}")
        return None, None

    elif workflow == "WORKFLOW_B":
        # Pillar 3: build the universal inter-agent contract
        spec = nexus.build_universal_spec(payload, gold_index, schema_snapshot)
        if spec is None:
            return None, None

        # Surface advisory flags to the marketer before any query executes
        if spec.discrepancy_flags:
            _print_discrepancy_audit(spec)

        # Dispatch to Quant via the UniversalJSONSpec entry point
        result = quant.audit_from_spec(spec)

        if isinstance(result, NexusErrorPayload):
            # One automated correction pass via the existing taxonomy-guided retry
            result = nexus.route_with_retry(spec.to_audience_sizing_request(), quant)

        if not isinstance(result, QuantAuditLog):
            return None, None

        # Pillar 4: fan out to BriefingAgent with the same UniversalJSONSpec
        briefing.subscribe(spec)
        brief_output: BriefingOutput = briefing.execute()

        return result, brief_output

    else:
        print(f"\n[ERROR]: Unknown workflow '{workflow}'")
        return None, None


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _format_audit_log(log: QuantAuditLog) -> str:
    sep = "=" * 66
    thin = "-" * 44
    lines = [
        "",
        sep,
        "  VIBE OCTO QUANT — AUDIENCE AUDIT LOG",
        sep,
        "",
        f"  Campaign : {log.request.campaign_name}",
        f"  Code     : {log.request.campaign_code}  /  {log.request.campaign_sub_code}",
        f"  Cadence  : {log.request.cadence}   |   Medium: {log.request.medium}",
        f"  Target Core Product : {log.request.target_population}",
        "",
        "  AUDIENCE WATERFALL",
        "  " + thin,
    ]

    if log.waterfall:
        max_label = max(len(lyr.layer_name) for lyr in log.waterfall)
        for lyr in log.waterfall:
            marker = "=" if "Universal Control Group" in lyr.layer_name else "-"
            lines.append(
                f"  {marker} {lyr.layer_name:<{max_label}}  {lyr.audience_count:>14,}"
            )
    else:
        lines.append("  (no waterfall rows returned — check BQ connectivity)")

    lines += [
        "",
        f"  FINAL COUNT : {log.final_count:,}",
        "",
    ]

    if log.optimization_note:
        lines += [
            "  AUDIT NOTE",
            "  " + thin,
            f"  {log.optimization_note}",
            "",
        ]

    lines.append(sep)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Brief output formatter
# ---------------------------------------------------------------------------

def _print_brief(brief: BriefingOutput) -> None:
    """Print the BriefingAgent Markdown output with a structured wrapper."""
    if not brief.brief_markdown:
        return
    sep = "=" * 66
    thin = "-" * 44
    print()
    print(sep)
    print("  VIBE OCTO BRIEFING — CAMPAIGN INTELLIGENCE BRIEF")
    print(sep)
    print()
    print(brief.brief_markdown)
    print()
    print("  " + thin)
    tier_label = f"Tier: {brief.tier}"
    conf_label = f"Confidence: {brief.confidence_score:.0%}"
    print(f"  {tier_label:<22}  {conf_label}")
    print(sep)


# ---------------------------------------------------------------------------
# Interactive console — dual-intent engine
# ---------------------------------------------------------------------------

def _run_console(
    nexus: NexusAgent,
    quant: QuantAgent,
    briefing: BriefingAgent,
    gold_index: GoldTierIndex,
    schema_snapshot: dict,
) -> None:
    while True:
        print("  How can the OCTO team help you today?\n")
        query = input("  > ").strip()
        print()

        if not query:
            continue

        if query.lower() in ("q", "quit", "exit"):
            print("  Session closed.\n")
            break

        ctx = _build_dynamic_context(query)
        if ctx:
            nexus.set_session_context(ctx)
            quant.set_session_context(ctx)

        print("  [NEXUS AGENT] -> Analyzing intent...\n")
        workflow, payload = nexus.classify_and_route(query)
        log, brief_output = route(
            nexus,
            quant,
            briefing,
            workflow,
            payload if payload is not None else {"query": query},
            gold_index,
            schema_snapshot,
        )
        if log:
            print(_format_audit_log(log))
        if brief_output:
            _print_brief(brief_output)
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _silence_google_noise()
    os.system("cls" if os.name == "nt" else "clear")

    load_dotenv(_NEXUS_DIR / ".env")
    load_dotenv(_QUANT_DIR / ".env", override=False)
    load_dotenv(_BRIEFING_DIR / ".env", override=False)

    # Pillar 1: Knowledge Base — atomic clean-slate refresh on every startup.
    # Reads BQ campaign_knowledge + verified_app_registry.json, classifies
    # GOLD/BRONZE, and writes semantic_knowledge_index.json atomically.
    ingester = KnowledgeBaseIngester(_ROOT / "ingestion_config.json")
    summary = ingester.run_full_refresh()
    print(
        f"\n[KNOWLEDGE BASE] Refresh complete: "
        f"{summary.gold_count} GOLD, {summary.bronze_count} BRONZE records indexed."
    )
    if summary.fetch_errors:
        print(
            f"[KNOWLEDGE BASE] {len(summary.fetch_errors)} brief fetch error(s) "
            f"(skipped): {summary.fetch_errors[:3]}"
        )

    gold_index = GoldTierIndex()
    gold_index.load_from_file(_ROOT / "semantic_knowledge_index.json")

    # Pillar 2: Schema Discovery — fetch live INFORMATION_SCHEMA metadata for
    # the adobe dataset and inject it into all agents before the console loop.
    # Cache TTL is 1 hour; subsequent startups within that window skip the BQ call.
    schema_discovery = SchemaDiscoveryLayer(
        project="bi-srv-hsmdet-pr-7b9def",
        datasets=["adobe"],
        cache_path=_ROOT / ".sdl_schema_cache.json",
    )
    snapshot = schema_discovery.get_snapshot()
    schema_str = schema_discovery.to_prompt_string(snapshot)
    status = "cache hit" if snapshot.cache_hit else "fresh fetch"
    print(
        f"[SCHEMA DISCOVERY] Adobe dataset schema loaded "
        f"({status}, {len(snapshot.columns)} columns)."
    )

    # Instantiate agents from registry
    nexus: NexusAgent = _AGENT_REGISTRY["nexus"]()
    quant: QuantAgent = _AGENT_REGISTRY["quant"]()
    briefing: BriefingAgent = _AGENT_REGISTRY["briefing"]()

    # Inject shared runtime context into all active agents
    quant.set_runtime_schema(schema_str)
    nexus.set_runtime_schema_snapshot(snapshot.to_dict())
    briefing.set_runtime_schema(schema_str)
    briefing.set_gold_index(gold_index)

    _run_console(nexus, quant, briefing, gold_index, snapshot.to_dict())


if __name__ == "__main__":
    main()
