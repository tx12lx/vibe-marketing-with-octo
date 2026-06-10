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
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent
_NEXUS_DIR = _ROOT / "Vibe OCTO Nexus"
_QUANT_DIR = _ROOT / "Vibe OCTO Quant"
_BRIEFING_DIR = _ROOT / "Vibe OCTO Briefing"
_FEEDBACK_DIR = _ROOT / "Vibe OCTO Feedback"
_SCHEMA_DISCOVERY_DIR = _ROOT / "schema_discovery"
_GLOSSARY_PATH = _ROOT / "glossary.json"
_QUERY_CATALOG_PATH = _ROOT / "query_catalog.json"
_BUSINESS_RULES_PATH = _ROOT / "business_rules.json"
_ARTIFACTS_DIR = _ROOT / "knowledge_base" / "artifacts"

# Keywords that trigger dynamic glossary/catalog injection.
_GLOSSARY_KEYWORDS = {"PFE", "KI", "TWA", "AALBAU", "NAKED", "FFH", "MNH", "MNP"}

# Load all agent environments before any agent code is imported.
# override=False means the first file wins on conflicts.
load_dotenv(_NEXUS_DIR / ".env")
load_dotenv(_QUANT_DIR / ".env", override=False)
load_dotenv(_FEEDBACK_DIR / ".env", override=False)

for _p in [str(_FEEDBACK_DIR), str(_BRIEFING_DIR), str(_QUANT_DIR), str(_NEXUS_DIR), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from nexus_agent import NexusAgent  # noqa: E402
from quant_agent import QuantAgent  # noqa: E402
from briefing_agent import BriefingAgent  # noqa: E402
from feedback_agent import FeedbackAgent  # noqa: E402
from core.knowledge_context import KnowledgeContext  # noqa: E402
from pydantic_schemas import AdHocSizingRequest, BriefingOutput, BusinessRule, FeedbackInput, IntentClassification, NexusErrorPayload, QuantAuditLog, UniversalJSONSpec  # noqa: E402
from schema_discovery.discovery_layer import SchemaDiscoveryLayer, SchemaColumn, SchemaSnapshot  # noqa: E402
from knowledge_base.tier_index import GoldTierIndex  # noqa: E402
from hitl.audit_loop import HITLAuditLoop  # noqa: E402
from core.glossary import GlossaryManager  # noqa: E402
from core.brief_fetcher import BriefFetcher  # noqa: E402
from core.thought_display import ThoughtDisplay  # noqa: E402
from core.business_rules_registry import BusinessRulesRegistry  # noqa: E402


_ADC_REAUTH_CMD = (
    "gcloud auth application-default login "
    "--scopes=https://www.googleapis.com/auth/cloud-platform,"
    "https://www.googleapis.com/auth/spreadsheets.readonly,"
    "https://www.googleapis.com/auth/drive.readonly"
)


def _check_sheets_credentials() -> bool:
    """Probe Sheets API at startup and print actionable guidance on failure.

    Returns True if access is confirmed (or inconclusive due to a 403 on a
    restricted sheet — scope is present). Returns False when a 401 confirms
    the ADC token is missing the spreadsheets.readonly scope.
    """
    fetcher = BriefFetcher()
    ok, reason = fetcher.probe_sheets_access()
    if ok:
        return True

    sep = "!" * 68
    print(f"\n{sep}")
    print("  SHEETS ACCESS ERROR — Brief data will be EMPTY until fixed.")
    print(sep)
    print()
    print(f"  {reason}")
    print()
    print("  Run this command in a terminal, then restart Vibe OCTO:")
    print()
    print(f"      python refresh_adc_scopes.py")
    print()
    print("  OR run gcloud directly:")
    print()
    print(f"      {_ADC_REAUTH_CMD}")
    print()
    print(sep)
    print()
    return False


def _silence_google_noise() -> None:
    """Mute Google Cloud SDK and urllib3 log chatter below ERROR level."""
    for name in (
        "google", "google.auth", "google.auth.transport",
        "google.cloud", "urllib3", "grpc",
        # brief_fetcher warnings are redundant — the ingester already
        # collects fetch_errors and the orchestrator prints them at startup.
        "core.brief_fetcher",
    ):
        logging.getLogger(name).setLevel(logging.ERROR)
    warnings.filterwarnings("ignore", category=UserWarning)
    warnings.filterwarnings("ignore", category=ResourceWarning)


# ---------------------------------------------------------------------------
# Disk-based artifact loaders (used at startup instead of live BQ ingestion)
# ---------------------------------------------------------------------------

def _load_adobe_schema_from_disk(artifacts_path: Path) -> Optional[SchemaSnapshot]:
    """Build a SchemaSnapshot from the pre-built adobe_schema.json artifact.

    Returns None if the file is missing or malformed so callers can fall back
    to a live SchemaDiscoveryLayer fetch.
    """
    adobe_path = artifacts_path / "adobe_schema.json"
    if not adobe_path.exists():
        return None
    try:
        data = json.loads(adobe_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    project = data.get("project", "bi-srv-hsmdet-pr-7b9def")
    dataset = data.get("dataset", "adobe")
    snapshot_at = data.get("snapshot_at", "")

    columns: list[SchemaColumn] = []
    for view_name, view_data in data.get("views", {}).items():
        is_view = view_data.get("type") == "VIEW"
        for col in view_data.get("columns", []):
            columns.append(SchemaColumn(
                table_name=view_name,
                column_name=col.get("name", ""),
                data_type=col.get("type", ""),
                is_nullable=col.get("nullable", True),
                description="",
                is_view=is_view,
            ))

    return SchemaSnapshot(
        project=project,
        datasets=[dataset],
        columns=columns,
        fetched_at=snapshot_at,
        cache_hit=True,
    )


def _print_kb_status(
    gold_index: GoldTierIndex,
    snapshot: SchemaSnapshot,
    rule_count: int,
    knowledge_ctx: Optional["KnowledgeContext"] = None,
) -> None:
    """Print the warm startup banner showing knowledge base readiness."""
    kb_meta: dict = {}
    try:
        kb_meta = json.loads((_ROOT / "semantic_knowledge_index.json").read_text(encoding="utf-8"))
    except Exception:
        pass

    total = (kb_meta.get("gold_count") or 0) + (kb_meta.get("bronze_count") or 0)
    gold = kb_meta.get("gold_count") or 0
    generated_at = kb_meta.get("generated_at") or ""
    ts_display = generated_at[:16].replace("T", " ") + " UTC" if generated_at else "unknown"
    view_count = len({c.table_name for c in snapshot.columns})
    ctx_campaigns = knowledge_ctx.campaign_count if knowledge_ctx is not None else gold

    sep = "=" * 66
    print()
    print(sep)
    print("  Welcome to Vibe Marketing with OCTO!")
    print(sep)
    print()
    print("  Knowledge base loaded successfully:")
    print(f"    [OK] {total:,} campaigns ready")
    print(f"    [OK] {gold} GOLD tier blueprints")
    print(f"    [OK] {rule_count} verified business rules")
    print(f"    [OK] Adobe schema: {view_count} views ready")
    print(f"    [OK] Full knowledge context: {ctx_campaigns} campaigns injected into all agents")
    print()
    print(f"  Knowledge base last refreshed: {ts_display}")
    print()
    print("  To refresh knowledge base manually:")
    print("    python -m knowledge_base.vibe_octo_knowledge --full-refresh")
    print()
    print(sep)
    print()


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


def _build_dynamic_context(query_text: str, gold_index=None) -> str:
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

    business_terms = glossary.get("business_terms", {})
    matched_terms = {k: v for k, v in business_terms.items() if k.upper() in detected}
    if matched_terms:
        parts.append(
            "BUSINESS TERM DEFINITIONS (authoritative — override any inferred meaning):\n"
        )
        for key, defn in matched_terms.items():
            parts.append(
                f"  '{key}' — Business Meaning: {defn.get('business_meaning', '')}\n"
            )
            if defn.get("sql_filter"):
                parts.append(f"       SQL Filter: {defn['sql_filter']}\n")
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

    # Compound override: NAKED + FFH together means "naked FFH customers" —
    # FFH/Home Solutions customers with no mobility, NOT naked mobility customers.
    if "NAKED" in detected and ("FFH" in detected or "MNH" in detected or "MNP" in detected):
        print(
            "  [LEARNING DISCOVERY] -> Detected NAKED + FFH context: injecting naked FFH override."
        )
        parts.append(
            "COMPOUND CONTEXT OVERRIDE — NAKED FFH (both 'naked' and 'FFH'/'home' signals present):\n"
            "  The request is about FFH (Home Solutions) customers with NO linked mobility plan.\n"
            "  This is the REVERSE of naked mobility — do NOT use bq_fda_mob_mobility_base.\n\n"
            "  MANDATORY BASE TABLE : `bi-srv-hsmdet-pr-7b9def.adobe.bq_dly_dbm_customer_profl`\n"
            "  MANDATORY FILTER     : mnh_mob_ban IS NULL\n"
            "  Ignore any prior injection of mnh_ffh_ban for this request — that filter applies\n"
            "  only to the mobility base and is irrelevant here.\n\n"
        )

    # FFH / MNP gold campaign examples from knowledge index.
    if gold_index is not None and ("FFH" in detected or "MNH" in detected or "MNP" in detected):
        _FFH_SIGNALS = {"FFH", "MNP", "DBM", "CUSTOMER_PROFL", "HOME_SOL", "HOME SOLUTIONS"}
        ffh_examples = [
            rec for rec in gold_index._index.values()
            if any(
                sig in (rec.targeting_summary or "").upper()
                or sig in (rec.campaign_name or "").upper()
                for sig in _FFH_SIGNALS
            )
        ][:3]
        if ffh_examples:
            print(
                "  [LEARNING DISCOVERY] -> Injected FFH/MNP gold campaign examples from knowledge index."
            )
            parts.append("GOLD CAMPAIGN EXAMPLES — FFH / MNP targeting (authoritative reference):\n")
            for rec in ffh_examples:
                ts = (rec.targeting_summary or "")[:400]
                parts.append(f"  Campaign : {rec.campaign_name}\n")
                parts.append(f"  Targeting: {ts}\n\n")

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
    "feedback": FeedbackAgent,
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
# Error recovery helpers
# ---------------------------------------------------------------------------

def _is_column_error(error_summary: str) -> bool:
    lower = error_summary.lower()
    return (
        "unknown column" in lower
        or "unrecognized name" in lower
        or "column not found" in lower
    )


def _save_execution_corrections(
    corrections: list[str],
    request: AdHocSizingRequest,
    rules_registry: "BusinessRulesRegistry",
) -> int:
    """Parse correction strings accumulated during recovery and persist as BusinessRules.

    Returns the number of rules saved.
    """
    # Infer the table from the request text so the saved rule is table-scoped.
    all_text = " ".join(filter(None, [
        request.target_population or "",
        " ".join(request.filters or []),
        " ".join(corrections),
    ])).lower()
    if any(s in all_text for s in ("ffh", "home solutions", "naked ffh", "dbm", "mnh_mob_ban",
                                    "ex_standard_ex", "ffh_stopsell", "bacct_num")):
        table_hint = "bq_dly_dbm_customer_profl"
    else:
        table_hint = "bq_fda_mob_mobility_base"

    now_iso = datetime.now(tz=timezone.utc).isoformat()
    saved = 0
    for correction in corrections:
        if not correction.strip():
            continue
        rule_id = f"correction_{uuid.uuid4().hex[:8]}"
        desc = f"Column correction applied during execution: {correction[:120]}"
        if table_hint:
            desc = f"[{table_hint}] {desc}"
        rule = BusinessRule(
            rule_id=rule_id,
            created_at=now_iso,
            verified_by="user_correction_during_execution",
            raw_correction=correction,
            rule_description=desc,
            rule_type="general",
            structured_value={"note": correction, "table": table_hint or "unknown"},
            scope="universal",
            confidence=1.0,
            source="user_correction_during_execution",
            clarification_rounds=0,
            applies_to_future=True,
            overrides_acc_summary=False,
            priority=1,
        )
        try:
            rules_registry.add_rule(rule)
            saved += 1
        except Exception:
            pass
    return saved


def _direct_count_with_recovery(
    quant: QuantAgent,
    request: AdHocSizingRequest,
    rules_registry: Optional["BusinessRulesRegistry"] = None,
) -> Union[QuantAuditLog, NexusErrorPayload]:
    """Run direct_count with up to 3 HITL recovery attempts on column-not-found errors.

    Fix 2: Corrections are applied immediately to each retry (accumulated in optimization_context).
    Fix 3: After successful recovery, corrections are saved to business_rules.json.
    """
    result = quant.direct_count(request)
    corrections_made: list[str] = []

    for _ in range(3):
        if isinstance(result, QuantAuditLog):
            # Fix 3: Persist corrections that led to this success.
            if corrections_made and rules_registry is not None:
                saved = _save_execution_corrections(corrections_made, request, rules_registry)
                if saved:
                    corrections_text = " ".join(corrections_made).lower()
                    table = "bq_dly_dbm_customer_profl" if any(
                        s in corrections_text
                        for s in ("ex_standard_ex", "ffh_stopsell", "bacct_num", "serv_prov")
                    ) else "bq_fda_mob_mobility_base"
                    ThoughtDisplay.correction_rules_saved(table, saved)
            return result

        if not _is_column_error(result.error_summary):
            return result

        correction = ThoughtDisplay.column_not_found_ask(result.error_summary)
        if not correction:
            return result

        corrections_made.append(correction)
        # Fix 2: Accumulate all corrections so every retry benefits from all prior guidance.
        ctx = ((request.optimization_context or "") + "\n" + correction).strip()
        request = request.model_copy(update={"optimization_context": ctx})
        result = quant.direct_count(request)

    # All retries exhausted -- show graceful recovery instead of crashing.
    ThoughtDisplay.graceful_recovery()
    return result


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
    rules_registry: Optional["BusinessRulesRegistry"] = None,
) -> tuple[Optional[UniversalJSONSpec], Optional[QuantAuditLog], Optional[BriefingOutput]]:
    """Dispatch a classified request to the correct pipeline branch.

    workflow 'WORKFLOW_A': Ad-Hoc Exploratory Request — natural language audience
                           sizing. Runs a direct single-row count via Quant.
                           BriefingAgent is not invoked on this path.
                           Returns (None, QuantAuditLog | None, None).

    workflow 'WORKFLOW_B': Structured Campaign Execution Request — named campaign
                           brief. Builds a UniversalJSONSpec via NexusAgent (Pillar 3
                           Gateway), runs the discrepancy audit, dispatches to
                           Quant via audit_from_spec(), then fans out to
                           BriefingAgent for the campaign intelligence brief.

                           On NexusErrorPayload from Quant, falls back to the
                           existing route_with_retry() for one taxonomy-guided
                           correction attempt.

                           Returns (UniversalJSONSpec, QuantAuditLog, BriefingOutput)
                           on success so the console can pass spec to HITLAuditLoop.

    Returns (spec | None, QuantAuditLog | None, BriefingOutput | None).
    """
    if workflow == "WORKFLOW_A":
        request = nexus.build_sizing_request_from_nl(payload["query"])
        if request is None:
            return None, None, None
        result = _direct_count_with_recovery(quant, request, rules_registry)
        if isinstance(result, QuantAuditLog):
            return None, result, None
        ThoughtDisplay.translate_nexus_error(result.error_summary)
        return None, None, None

    elif workflow == "WORKFLOW_B":
        # Pillar 3: build the universal inter-agent contract
        spec = nexus.build_universal_spec(payload, gold_index, schema_snapshot)
        if spec is None:
            return None, None, None

        # Surface advisory flags to the marketer before any query executes
        if spec.discrepancy_flags:
            ThoughtDisplay.discrepancy_check(len(spec.discrepancy_flags), spec.discrepancy_flags)
            _print_discrepancy_audit(spec)

        # Apply verified business rules before dispatching to Quant.
        if rules_registry is not None:
            applicable_rules = rules_registry.get_rules_for_execution(
                camp_id=spec.campaign_code,
                medium=spec.medium,
                cadence=spec.cadence,
                campaign_purpose=(spec.brief_agent_inputs or {}).get("campaign_purpose", ""),
                spec=spec,
            )
            if applicable_rules:
                ThoughtDisplay.show_rules_being_applied(
                    rules_registry.get_display_summary(applicable_rules)
                )
                spec = rules_registry.apply_rules_to_spec(spec, applicable_rules)

        # Dispatch to Quant via the UniversalJSONSpec entry point
        result = quant.audit_from_spec(spec)

        if isinstance(result, NexusErrorPayload):
            # One automated correction pass via the existing taxonomy-guided retry
            result = nexus.route_with_retry(spec.to_audience_sizing_request(), quant)

        if not isinstance(result, QuantAuditLog):
            ThoughtDisplay.error(
                "I was unable to complete the audience sizing request. "
                "The system attempted a correction but could not reconcile the targeting rules."
            )
            return None, None, None

        # Pillar 4: fan out to BriefingAgent with the same UniversalJSONSpec
        briefing.subscribe(spec)
        brief_output: BriefingOutput = briefing.execute()

        return spec, result, brief_output

    else:
        print(f"\n[ERROR]: Unknown workflow '{workflow}'")
        return None, None, None


# ---------------------------------------------------------------------------
# Unified intent-based router — replaces WORKFLOW_A / WORKFLOW_B dispatch
# ---------------------------------------------------------------------------

def route_by_intent(
    nexus: NexusAgent,
    quant: QuantAgent,
    briefing: BriefingAgent,
    intent: IntentClassification,
    query: str,
    gold_index: "GoldTierIndex",
    schema_snapshot: dict,
    rules_registry: Optional[BusinessRulesRegistry] = None,
) -> tuple[Optional[UniversalJSONSpec], Optional[QuantAuditLog], Optional[BriefingOutput]]:
    """Unified intent-based pipeline dispatcher.

    Every request goes through this function regardless of whether a named
    campaign is identified. Steps executed for every call:

      Step 4: Knowledge context assembled from GoldTierIndex + campaign brief.
      Step 3: BusinessRulesRegistry consulted whenever campaign context exists.
      Step 5: Correct agents activated based on intent type.

    Intent routing:
      sizing_request    -> QuantAgent (with or without campaign context)
      brief_generation  -> BriefingAgent (BRONZE spec built from NL if no campaign)
      brief_qa          -> BriefingAgent (same path as brief_generation)
      campaign_execution-> QuantAgent + BriefingAgent
      general_question  -> NexusAgent answers directly from knowledge base
    """
    it = intent.intent_type

    # Step 4: Assemble knowledge context when campaign is identified
    spec: Optional[UniversalJSONSpec] = None
    if intent.campaign_identified and intent.campaign_code:
        ThoughtDisplay.progress("Looking up your campaign in the knowledge base...")
        brief = nexus._find_brief_for_campaign(intent.campaign_code)
        if brief:
            ThoughtDisplay.progress("Found it! Preparing your targeting blueprint...")
            spec = nexus.build_universal_spec(brief, gold_index, schema_snapshot)
            if spec is not None and spec.discrepancy_flags:
                ThoughtDisplay.discrepancy_check(len(spec.discrepancy_flags), spec.discrepancy_flags)
                _print_discrepancy_audit(spec)
        else:
            ThoughtDisplay.progress("Couldn't locate that campaign. Switching to custom audience mode...")

    # Step 3: BusinessRulesRegistry — always consulted when campaign context is available
    if spec is not None and rules_registry is not None:
        applicable_rules = rules_registry.get_rules_for_execution(
            camp_id=spec.campaign_code,
            medium=spec.medium,
            cadence=spec.cadence,
            campaign_purpose=(spec.brief_agent_inputs or {}).get("campaign_purpose", ""),
            spec=spec,
        )
        if applicable_rules:
            ThoughtDisplay.show_rules_being_applied(
                rules_registry.get_display_summary(applicable_rules)
            )
            spec = rules_registry.apply_rules_to_spec(spec, applicable_rules)

    # Display which knowledge assets were consulted before any agent fires
    _applied_rule_count = 0
    if spec is not None and rules_registry is not None:
        # Re-use already-computed applicable_rules count when available
        try:
            _applied_rule_count = len(applicable_rules)  # type: ignore[name-defined]
        except NameError:
            pass
    _brief_req_count = 0
    if spec is not None:
        bai = spec.brief_agent_inputs or {}
        _brief_req_count = len(bai.get("brief_requirements") or [])
    ThoughtDisplay.show_knowledge_used(
        gold_campaign=spec.campaign_name if spec is not None else None,
        rules_applied=_applied_rule_count,
        brief_requirements=_brief_req_count,
        confidence=getattr(intent, "confidence_score", None),
    )

    # Step 5: Agent activation based on intent type
    if it == "sizing_request":
        if spec is not None:
            result = quant.audit_from_spec(spec)
            if isinstance(result, NexusErrorPayload):
                result = nexus.route_with_retry(spec.to_audience_sizing_request(), quant)
            if isinstance(result, QuantAuditLog):
                return spec, result, None
            for _ in range(3):
                if not _is_column_error(getattr(result, "error_summary", "")):
                    break
                correction = ThoughtDisplay.column_not_found_ask(result.error_summary)
                if not correction:
                    break
                ctx = ((spec.optimization_context or "") + "\n" + correction).strip()
                spec = spec.model_copy(update={"optimization_context": ctx})
                result = quant.audit_from_spec(spec)
                if isinstance(result, QuantAuditLog):
                    return spec, result, None
            ThoughtDisplay.error(
                "I was unable to complete the audience sizing request. "
                "The system attempted a correction but could not reconcile the targeting rules."
            )
            return None, None, None
        else:
            request = nexus.build_sizing_request_from_nl(query)
            if request is None:
                return None, None, None
            # Apply universal business rules to ad-hoc requests (campaign-scoped
            # rules are skipped because there is no campaign_code context here).
            if rules_registry is not None:
                universal_rules = [
                    r for r in rules_registry._rules
                    if r.applies_to_future and r.scope.lower() == "universal"
                ]
                if universal_rules:
                    ThoughtDisplay.show_rules_being_applied(
                        rules_registry.get_display_summary(universal_rules)
                    )
                    extra_filters = []
                    rule_notes = []
                    for rule in universal_rules:
                        rt = rule.rule_type.lower()
                        sql = rule.structured_value.get("sql") or rule.structured_value.get("filter", "")
                        if rt in ("filter_add", "exclusion_add") and sql and sql not in (request.filters or []):
                            extra_filters.append(sql)
                        elif rt in ("general", "population_note"):
                            note = rule.structured_value.get("note") or rule.rule_description
                            if note:
                                rule_notes.append(note)
                    updates: dict = {}
                    if extra_filters:
                        updates["filters"] = list(request.filters or []) + extra_filters
                    if rule_notes:
                        existing_ctx = request.optimization_context or ""
                        notes_str = "\n".join(f"[BUSINESS RULE] {n}" for n in rule_notes)
                        updates["optimization_context"] = (
                            (existing_ctx + "\n" + notes_str).strip() if existing_ctx else notes_str
                        )
                    if updates:
                        request = request.model_copy(update=updates)
            result = _direct_count_with_recovery(quant, request, rules_registry)
            if isinstance(result, QuantAuditLog):
                return None, result, None
            ThoughtDisplay.translate_nexus_error(result.error_summary)
            return None, None, None

    elif it in ("brief_generation", "brief_qa"):
        if spec is None:
            ThoughtDisplay.progress("No campaign context found. Building brief from your request...")
            request = nexus.build_sizing_request_from_nl(query)
            if request is None:
                return None, None, None
            try:
                spec = UniversalJSONSpec(
                    campaign_name=request.campaign_name,
                    campaign_code=request.campaign_code,
                    campaign_sub_code=request.campaign_sub_code,
                    cadence=request.cadence,
                    medium=request.medium,
                    campaign_tier="BRONZE",
                    knowledge_source="nl_only",
                    target_population=request.target_population,
                    filters=request.filters,
                    exclusion_layers=request.exclusion_layers,
                    optimization_context=request.optimization_context,
                    bq_project=request.bq_project,
                    bq_dataset=request.bq_dataset,
                    brief_agent_inputs={"raw_prompt": query},
                )
            except Exception:
                return None, None, None
        briefing.subscribe(spec)
        brief_output: BriefingOutput = briefing.execute()
        return spec, None, brief_output

    elif it == "campaign_execution":
        if spec is None:
            ThoughtDisplay.progress("No campaign context found. Switching to custom audience mode...")
            request = nexus.build_sizing_request_from_nl(query)
            if request is None:
                return None, None, None
            result = _direct_count_with_recovery(quant, request, rules_registry)
            if isinstance(result, QuantAuditLog):
                return None, result, None
            ThoughtDisplay.translate_nexus_error(result.error_summary)
            return None, None, None

        result = quant.audit_from_spec(spec)
        if isinstance(result, NexusErrorPayload):
            result = nexus.route_with_retry(spec.to_audience_sizing_request(), quant)
        if not isinstance(result, QuantAuditLog):
            if _is_column_error(getattr(result, "error_summary", "")):
                correction = ThoughtDisplay.column_not_found_ask(result.error_summary)
                if correction:
                    ctx = ((spec.optimization_context or "") + "\n" + correction).strip()
                    spec = spec.model_copy(update={"optimization_context": ctx})
                    result = quant.audit_from_spec(spec)
            if not isinstance(result, QuantAuditLog):
                ThoughtDisplay.error(
                    "I was unable to complete the audience sizing request. "
                    "The system attempted a correction but could not reconcile the targeting rules."
                )
                return None, None, None

        briefing.subscribe(spec)
        brief_output = briefing.execute()
        return spec, result, brief_output

    elif it == "general_question":
        answer = nexus.answer_general_question(query)
        if answer:
            sep = "=" * 66
            thin = "-" * 44
            print()
            print(sep)
            print("  VIBE OCTO — Knowledge Response")
            print(sep)
            print()
            for line in answer.splitlines():
                print(f"  {line}")
            print()
            print("  " + thin)
            print(sep)
        return None, None, None

    else:
        print(f"\n[ERROR]: Unrecognised intent type '{it}'")
        return None, None, None


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _format_audit_log(log: QuantAuditLog) -> str:
    sep = "=" * 66
    thin = "-" * 44
    lines = [
        "",
        sep,
        "  Vibe OCTO Quant — Audience Audit Log",
        sep,
        "",
        f"  Campaign : {log.request.campaign_name}",
        f"  Code     : {log.request.campaign_code}  /  {log.request.campaign_sub_code}",
        f"  Cadence  : {log.request.cadence}   |   Medium: {log.request.medium}",
        f"  Audience : {log.final_count:,} qualified contacts",
        "",
    ]

    if log.optimization_note and "clean" not in log.optimization_note.lower():
        note_text = log.optimization_note.removeprefix("Optimization Note: ")
        lines += [
            "  Note",
            "  " + thin,
            f"  {note_text}",
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
# Ad-hoc feedback helper — FeedbackAgent without a full UniversalJSONSpec
# ---------------------------------------------------------------------------

def _run_adhoc_feedback(
    spec: Optional[UniversalJSONSpec],
    log: Optional[QuantAuditLog],
    query: str,
    correction: str,
    knowledge_ctx: Optional[KnowledgeContext] = None,
) -> None:
    """Invoke FeedbackAgent for ad-hoc sizing, brief-only, and general-question corrections.

    Builds a minimal FeedbackInput from whatever context is available.  When no
    campaign spec exists the campaign_code defaults to 'AD_HOC' so FeedbackAgent
    can still extract and save universal rules that apply to future executions.
    """
    try:
        feedback_input = FeedbackInput(
            raw_correction=correction,
            campaign_code=spec.campaign_code if spec is not None else "AD_HOC",
            campaign_name=spec.campaign_name if spec is not None else "Ad-Hoc Query",
            medium=spec.medium if spec is not None else "",
            cadence=spec.cadence if spec is not None else "",
            campaign_purpose=(
                (spec.brief_agent_inputs or {}).get("campaign_purpose", "")
                if spec is not None else ""
            ),
            execution_context={
                "query": query,
                "final_audience_count": log.final_count if log is not None else None,
                "waterfall_steps": len(log.waterfall) if log is not None else 0,
                "has_campaign_context": spec is not None,
            },
            existing_rules=[],
            knowledge_tier=spec.campaign_tier if spec is not None else "BRONZE",
            raw_input_prompt=query,
        )
        agent = FeedbackAgent()
        if knowledge_ctx is not None:
            agent.set_knowledge_context(knowledge_ctx)
        agent.subscribe(feedback_input)
        agent.execute()
    except Exception:
        # FeedbackAgent failures must never crash the console loop.
        print("\n  Your feedback has been noted. I'll use it to guide future responses.\n")


# ---------------------------------------------------------------------------
# Simplified HITL — numbered menu for ad-hoc sizing, brief-only, and Q&A
# ---------------------------------------------------------------------------

def _collect_correction_and_feedback(
    spec: Optional[UniversalJSONSpec],
    log: Optional[QuantAuditLog],
    query: str,
    knowledge_ctx: Optional[KnowledgeContext] = None,
) -> None:
    print(
        "\n  No problem! I'd love to understand what went wrong"
        " so I can do better next time.\n"
        "\n  Please describe the issue in your own words -- no need"
        " to be technical.\n"
    )
    correction = input("  > ").strip()
    print("\n  Got it! Let me make sure I understand...\n")
    if correction:
        _run_adhoc_feedback(spec, log, query, correction, knowledge_ctx=knowledge_ctx)


def _simplified_hitl(
    spec: Optional[UniversalJSONSpec],
    log: Optional[QuantAuditLog],
    brief_output: Optional[BriefingOutput],
    query: str,
    intent_type: str,
    knowledge_ctx: Optional[KnowledgeContext] = None,
) -> None:
    """Numbered HITL menu for ad-hoc sizing, brief-only, and general questions."""
    audience_count = log.final_count if log is not None else None
    campaign_name = spec.campaign_name if spec is not None else None
    confidence = brief_output.confidence_score if brief_output is not None else None
    data_sources = brief_output.data_sources_cited if brief_output is not None else None

    ThoughtDisplay.show_hitl_summary_box(
        intent_type=intent_type,
        campaign_name=campaign_name,
        audience_count=audience_count,
        confidence=confidence,
        data_sources=data_sources,
    )

    has_sql = log is not None and bool(getattr(log, "sql", None))
    has_sources = brief_output is not None
    show_option2 = has_sql or has_sources

    while True:
        prompt_str = "  Enter 1, 2, or 3: " if show_option2 else "  Enter 1 or 3: "
        response = input(prompt_str).strip()

        if response == "1":
            print("\n  Wonderful! Moving on.\n")
            break

        elif response == "2" and show_option2:
            if intent_type in ("brief_generation", "brief_qa") and brief_output is not None:
                ThoughtDisplay.show_brief_sources(brief_output)
            elif has_sql:
                ThoughtDisplay.show_sql(log.sql)
            print("  Does everything look correct?\n")
            print("  What would you like to do?")
            print("  1  Looks good! Move on")
            print("  3  Something doesn't look right")
            print()
            while True:
                r2 = input("  Enter 1 or 3: ").strip()
                if r2 == "1":
                    print("\n  Wonderful! Moving on.\n")
                    break
                elif r2 == "3":
                    _collect_correction_and_feedback(spec, log, query, knowledge_ctx=knowledge_ctx)
                    break
                else:
                    print("  Please enter 1 or 3.")
            break

        elif response == "3":
            _collect_correction_and_feedback(spec, log, query, knowledge_ctx=knowledge_ctx)
            break

        else:
            if show_option2:
                print("  Please enter 1, 2, or 3.")
            else:
                print("  Please enter 1 or 3.")


# ---------------------------------------------------------------------------
# Interactive console — dual-intent engine
# ---------------------------------------------------------------------------

def _run_console(
    nexus: NexusAgent,
    quant: QuantAgent,
    briefing: BriefingAgent,
    gold_index: GoldTierIndex,
    schema_snapshot: dict,
    hitl: HITLAuditLoop,
    rules_registry: Optional[BusinessRulesRegistry] = None,
    knowledge_ctx: Optional[KnowledgeContext] = None,
) -> None:
    last_log: Optional[QuantAuditLog] = None

    while True:
        print("  How can the OCTO team help you today?\n")
        query = input("  > ").strip()
        print()

        if not query:
            continue

        if query.lower() in ("q", "quit", "exit"):
            print("  Session closed.\n")
            break

        if query.lower() in ("audit", "show query", "show sql"):
            if last_log and last_log.sql:
                ThoughtDisplay.show_sql(last_log.sql)
            else:
                print("  No query available yet. Run a campaign sizing first.\n")
            continue

        try:
            # Step 1: Glossary keyword injection (always runs; enriches session context)
            ctx = _build_dynamic_context(query, gold_index)
            if ctx:
                nexus.set_session_context(ctx)
                quant.set_session_context(ctx)

            # Step 2: Intent classification — always consults knowledge layer
            intent = nexus.classify_intent(query)

            # Steps 3-5: Unified knowledge-grounded pipeline
            spec, log, brief_output = route_by_intent(
                nexus,
                quant,
                briefing,
                intent,
                query,
                gold_index,
                schema_snapshot,
                rules_registry,
            )
            if log:
                last_log = log
                print(_format_audit_log(log))
            if brief_output:
                _print_brief(brief_output)

            # Step 7: HITL — always triggered for any execution that produced output.
            # Full HITL (spec + log) fires for campaign sizing and execution.
            # Simplified HITL fires for ad-hoc sizing, brief-only, and general questions.
            if spec is not None and log is not None:
                should_continue = hitl.prompt(spec, log, brief_output, intent_type=intent.intent_type)
                if rules_registry is not None:
                    rules_registry._load()
                if knowledge_ctx is not None:
                    knowledge_ctx.reload_rules()
                if not should_continue:
                    break
            elif spec is not None or log is not None or brief_output is not None or intent.intent_type == "general_question":
                _simplified_hitl(spec, log, brief_output, query, intent.intent_type, knowledge_ctx=knowledge_ctx)
                if rules_registry is not None:
                    rules_registry._load()
                if knowledge_ctx is not None:
                    knowledge_ctx.reload_rules()

        except Exception as _exc:
            # Fix 4: Never exit the console loop due to an agent failure.
            ThoughtDisplay.graceful_recovery()

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
    load_dotenv(_FEEDBACK_DIR / ".env", override=False)

    # Knowledge ingestion is disabled until service account is configured.
    # Run manually when needed:
    #   python -m knowledge_base.vibe_octo_knowledge --full-refresh
    gold_index = GoldTierIndex()
    gold_index.load_from_file(_ROOT / "semantic_knowledge_index.json")

    # Pillar 2: Schema Discovery — load from pre-built artifact for instant startup.
    # Falls back to a live INFORMATION_SCHEMA BQ query only if the artifact is missing.
    schema_discovery = SchemaDiscoveryLayer(
        project="bi-srv-hsmdet-pr-7b9def",
        datasets=["adobe"],
        cache_path=_ROOT / ".sdl_schema_cache.json",
    )
    snapshot = _load_adobe_schema_from_disk(_ARTIFACTS_DIR)
    if snapshot is None:
        snapshot = schema_discovery.get_snapshot()
    schema_str = schema_discovery.to_prompt_string(snapshot)

    # Instantiate agents from registry
    nexus: NexusAgent = _AGENT_REGISTRY["nexus"]()
    quant: QuantAgent = _AGENT_REGISTRY["quant"]()
    briefing: BriefingAgent = _AGENT_REGISTRY["briefing"]()

    # Build centralised KnowledgeContext (loads all 5 knowledge files once at startup).
    # All agents share this single instance; large knowledge blobs are formatted once
    # and pinned as ephemeral cached blocks on each Fuel iX / Claude API call.
    knowledge_ctx = KnowledgeContext(
        artifacts_dir=_ARTIFACTS_DIR,
        root_dir=_ROOT,
    )

    # Inject shared runtime context into all active agents
    quant.set_runtime_schema(schema_str)
    nexus.set_runtime_schema_snapshot(snapshot.to_dict())
    briefing.set_runtime_schema(schema_str)
    briefing.set_gold_index(gold_index)

    # Inject KnowledgeContext into all four runtime agents
    nexus.set_knowledge_context(knowledge_ctx)
    quant.set_knowledge_context(knowledge_ctx)
    briefing.set_knowledge_context(knowledge_ctx)

    # Pillar 5: HITLAuditLoop — Human-in-the-Loop gate and feedback flywheel.
    # Shares the same GoldTierIndex and GlossaryManager instances as the rest of
    # the session so in-memory promotions and glossary patches take effect immediately.
    hitl = HITLAuditLoop(
        gold_index=gold_index,
        glossary_manager=GlossaryManager(str(_GLOSSARY_PATH)),
        failure_log_path=_ROOT / "semantic_failure_log.json",
        registry_path=_ROOT / "verified_app_registry.json",
    )
    hitl.set_knowledge_context(knowledge_ctx)

    # Pillar 6: BusinessRulesRegistry — load verified business rules extracted by
    # FeedbackAgent after HITL NO responses. Applied automatically before Quant.
    rules_registry = BusinessRulesRegistry(_BUSINESS_RULES_PATH)
    rule_count = len(rules_registry._rules)

    _print_kb_status(gold_index, snapshot, rule_count, knowledge_ctx)

    _run_console(nexus, quant, briefing, gold_index, snapshot.to_dict(), hitl, rules_registry, knowledge_ctx)


if __name__ == "__main__":
    main()
