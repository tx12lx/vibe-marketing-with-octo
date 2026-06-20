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
import time
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Union

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

# Pin ROOT at sys.path[0] so root core/ package wins over any agent-subdir core/.
# Previous entries are cleaned out first to prevent stale ordering from a prior import.
_SEARCH_DIRS = [str(_FEEDBACK_DIR), str(_BRIEFING_DIR), str(_QUANT_DIR), str(_NEXUS_DIR), str(_ROOT)]
for _p in _SEARCH_DIRS:
    while _p in sys.path:
        sys.path.remove(_p)
for _p in _SEARCH_DIRS:
    sys.path.insert(0, _p)
# Final order: ROOT NEXUS QUANT BRIEFING FEEDBACK ... (ROOT at 0)

from agents.nexus_agent import NexusAgent  # noqa: E402
from agents.quant_agent import QuantAgent  # noqa: E402
from agents.briefing_agent import BriefingAgent  # noqa: E402
from agents.feedback_agent import FeedbackAgent  # noqa: E402
from core.knowledge_context import KnowledgeContext  # noqa: E402
from pydantic_schemas import AdHocSizingRequest, BriefingOutput, BusinessRule, FeedbackInput, FeedbackOutput, IntentClassification, NexusErrorPayload, QuantAuditLog, UniversalJSONSpec  # noqa: E402
from schema_discovery.discovery_layer import SchemaDiscoveryLayer, SchemaColumn, SchemaSnapshot  # noqa: E402
from knowledge_base.tier_index import GoldTierIndex  # noqa: E402
from hitl.audit_loop import HITLAuditLoop  # noqa: E402
from core.glossary import GlossaryManager  # noqa: E402
from core.brief_fetcher import BriefFetcher  # noqa: E402
from core.thought_display import ThoughtDisplay  # noqa: E402
from core.business_rules_registry import BusinessRulesRegistry  # noqa: E402
from core.resilience import run_startup_health_check  # noqa: E402
from core.audit_logger import AuditLogger, HITL_YES, HITL_NO, HITL_REVIEW_YES, HITL_REVIEW_NO  # noqa: E402


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
    """Print a plain-English startup summary."""
    kb_meta: dict = {}
    try:
        kb_meta = json.loads((_ARTIFACTS_DIR / "semantic_knowledge_index.json").read_text(encoding="utf-8"))
    except Exception:
        pass

    summary = kb_meta.get("ingestion_summary") or kb_meta
    total = summary.get("total_campaigns") or (summary.get("gold_count") or 0) + (summary.get("bronze_count") or 0)
    generated_at = kb_meta.get("generated_at") or ""

    date_display = "unknown"
    stale_count = 0
    _STALE_DAYS = 7
    now = datetime.now(tz=timezone.utc)
    if generated_at:
        try:
            dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            date_display = dt.strftime(f"%B {dt.day}, %Y")
        except Exception:
            date_display = generated_at[:10]
    for c in kb_meta.get("campaigns", []):
        ts = c.get("last_ingested_at") or c.get("ingested_at", "")
        if not ts:
            continue
        try:
            ingested = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if ingested.tzinfo is None:
                ingested = ingested.replace(tzinfo=timezone.utc)
            if (now - ingested).days > _STALE_DAYS:
                stale_count += 1
        except Exception:
            pass

    print()
    print("Vibe Marketing with OCTO is ready.")
    campaign_line = f"{total:,} campaigns loaded" if total else "Campaigns loaded"
    print(f"{campaign_line}, data last updated {date_display}.")
    if stale_count > 0:
        print(f"Note: {stale_count} campaign(s) haven't been updated in over a week. To refresh, run:")
        print("  python -m knowledge_base.vibe_octo_knowledge --incremental")
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
# Dynamic agent registry — auto-discovered from agents/ at startup.
# Adding a new agent requires only one new file in agents/.
# ---------------------------------------------------------------------------

def _discover_agents(agents_dir: Path) -> tuple[dict[str, type], dict[str, type]]:
    """Scan agents/ for BaseAgent subclasses and build registries.

    Returns:
      agent_registry   — {worker_id: AgentClass} for direct instantiation
      intent_routing   — {intent_type: AgentClass} for intent-based dispatch
    """
    import importlib
    import inspect
    from core.base_agent import BaseAgent as _BaseAgent

    agent_registry: dict[str, type] = {}
    intent_routing: dict[str, type] = {}

    if not agents_dir.exists():
        return agent_registry, intent_routing

    for fpath in sorted(agents_dir.glob("*.py")):
        if fpath.name.startswith("_"):
            continue
        module_name = f"agents.{fpath.stem}"
        try:
            mod = importlib.import_module(module_name)
        except Exception as exc:
            logging.warning("_discover_agents: could not import %s: %s", module_name, exc)
            continue
        for _name, obj in inspect.getmembers(mod, inspect.isclass):
            if (
                obj is not _BaseAgent
                and issubclass(obj, _BaseAgent)
                and hasattr(obj, "WORKER_ID")
                and obj.__module__ == module_name
            ):
                worker_id = obj.WORKER_ID
                # Use the worker_id stem (strip version suffix) as registry key
                key = worker_id.split("_")[0]
                agent_registry[key] = obj
                for intent in (obj.HANDLED_INTENTS or frozenset()):
                    # Prefer higher-priority (longer WORKER_ID) if two agents handle the same intent
                    if intent not in intent_routing:
                        intent_routing[intent] = obj

    return agent_registry, intent_routing


_AGENTS_DIR = _ROOT / "agents"
_AGENT_REGISTRY, _INTENT_ROUTING = _discover_agents(_AGENTS_DIR)

# Verify all required agents were discovered; fall back to explicit import if not.
_REQUIRED = {"nexus": NexusAgent, "quant": QuantAgent, "briefing": BriefingAgent, "feedback": FeedbackAgent}
for _k, _cls in _REQUIRED.items():
    if _k not in _AGENT_REGISTRY:
        logging.warning("_discover_agents: expected agent '%s' not found — using explicit import", _k)
        _AGENT_REGISTRY[_k] = _cls


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
    allow_interactive: bool = True,
) -> Union[QuantAuditLog, NexusErrorPayload]:
    """Run direct_count with up to 3 HITL recovery attempts on column-not-found errors.

    Fix 2: Corrections are applied immediately to each retry (accumulated in optimization_context).
    Fix 3: After successful recovery, corrections are saved to business_rules.json.
    When allow_interactive=False (API mode) a single attempt is made with no input() calls.
    """
    result = quant.direct_count(request)
    if not allow_interactive:
        return result

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
# Audit helpers
# ---------------------------------------------------------------------------

def _agent_called_for_intent(intent_type: str) -> str:
    """Map intent type to the agent WORKER_ID(s) that handle it."""
    _map = {
        "sizing_request":   "quant_v1",
        "campaign_execution": "quant_v1,briefing_v1",
        "brief_generation": "briefing_v1",
        "brief_qa":         "briefing_v1",
        "general_question": "nexus_v1",
    }
    return _map.get(intent_type, "nexus_v1")


# ---------------------------------------------------------------------------
# Intent-specific context builders
# ---------------------------------------------------------------------------

def _build_sizing_context(intent, query, nexus, gold_index, schema_snapshot, rules_registry, session_memory):
    """Build a sizing-oriented UniversalJSONSpec for sizing_request / campaign_execution."""
    if not intent.campaign_identified or not intent.campaign_code:
        return None
    ThoughtDisplay.progress("Looking up your campaign in the knowledge base...")
    brief = nexus._find_brief_for_campaign(intent.campaign_code)
    if not brief:
        ThoughtDisplay.progress("Couldn't locate that campaign. Switching to custom audience mode...")
        return None
    ThoughtDisplay.progress("Found it! Preparing your targeting blueprint...")
    return nexus.build_universal_spec(brief, gold_index, schema_snapshot)


def _build_briefing_context(intent, query, nexus, gold_index, schema_snapshot, rules_registry, session_memory):
    """Build a briefing-oriented UniversalJSONSpec for brief_generation / brief_qa.

    Priority order:
      1. Session memory -- prior sizing result for same campaign (multi-step chain)
      2. Gold index -- curated campaign intelligence library
      3. BQ deployment records -- live metadata, no SQL filter extraction
      4. Last resort -- campaign name alone (BRONZE tier); never returns None when
         campaign_code is non-empty
    """
    campaign_code = intent.campaign_code if intent.campaign_identified else None

    # 1. Session memory: incorporate prior sizing into the brief
    if session_memory is not None and not session_memory.is_empty:
        prior = session_memory.get_prior_sizing(campaign_code)
        if prior is not None and prior.spec is not None and prior.log is not None:
            filters_text = "\n".join(f"  - {f}" for f in (prior.spec.filters or []))
            augmented_prompt = (
                f"{query}\n\nCONTEXT FROM PRIOR SIZING (incorporate into the brief):\n"
                f"Campaign: {prior.campaign_name}\n"
                f"Final Audience: {prior.log.final_count:,} contacts\n"
                f"Targeting Criteria Applied:\n{filters_text}"
            )
            ThoughtDisplay.progress(
                f"Using prior sizing result ({prior.log.final_count:,} contacts) "
                "to enrich the brief..."
            )
            try:
                return UniversalJSONSpec(
                    campaign_name=prior.spec.campaign_name,
                    campaign_code=prior.spec.campaign_code,
                    campaign_sub_code=prior.spec.campaign_sub_code,
                    cadence=prior.spec.cadence,
                    medium=prior.spec.medium,
                    campaign_tier=prior.spec.campaign_tier,
                    knowledge_source=prior.spec.knowledge_source,
                    gold_blueprint_id=prior.spec.gold_blueprint_id,
                    target_population=prior.spec.target_population,
                    brief_agent_inputs={
                        "raw_prompt": augmented_prompt,
                        "prior_sizing_count": prior.log.final_count,
                        "prior_sizing_filters": prior.spec.filters,
                    },
                )
            except Exception as exc:
                print(f"  [briefing ctx step 1] session-memory spec failed: {exc.__class__.__name__}: {exc}")

    # 2. Gold index (knowledge library) -- highest-quality curated source for briefs.
    # Primary anchor: intent-identified campaign found by name/code lookup.
    # Semantic fallback: if name lookup misses, AI embedding search resolves the campaign.
    # Related campaigns: all semantically relevant campaigns are gathered and passed to
    # the BriefingAgent so it can draw on the full knowledge library, not just one record.
    if campaign_code:
        ThoughtDisplay.progress("Looking up your campaign in the knowledge base...")
        gold_record = gold_index.search(campaign_code)
        if gold_record is None:
            # Name lookup missed — use semantic search to resolve from the full query
            related_fallback = nexus.retrieve_related_campaigns(query, top_k=1)
            if related_fallback:
                fb = related_fallback[0]
                fb_camp_id = fb.get("camp_id", "")
                fb_sub_camp_id = fb.get("sub_camp_id", "")
                gold_record = gold_index.lookup(fb_camp_id, fb_sub_camp_id)
                if gold_record is None:
                    gold_record = gold_index.search(fb_camp_id)
        if gold_record is not None:
            related = nexus.retrieve_related_campaigns(query, top_k=10)
            ThoughtDisplay.progress(
                f"Found '{gold_record.campaign_name}' in the knowledge library "
                f"({len(related)} related campaigns identified by AI). "
                "Building brief from stored campaign intelligence..."
            )
            try:
                return UniversalJSONSpec(
                    campaign_name=gold_record.campaign_name or campaign_code,
                    campaign_code=gold_record.camp_id,
                    campaign_sub_code=gold_record.sub_camp_id,
                    cadence=gold_record.cadence or "ad-hoc",
                    medium=gold_record.medium or "unspecified",
                    campaign_tier="GOLD",
                    knowledge_source="bq_metadata",
                    gold_blueprint_id=f"{gold_record.camp_id}::{gold_record.sub_camp_id}",
                    target_population=gold_record.campaign_purpose or gold_record.campaign_name,
                    brief_agent_inputs={
                        "raw_prompt": query,
                        "related_campaigns": related,
                    },
                )
            except Exception as exc:
                print(f"  [briefing ctx step 2] gold-index spec failed: {exc.__class__.__name__}: {exc}")

    # 3. BQ deployment records (brief-oriented, no SQL filter extraction)
    if campaign_code:
        try:
            brief = nexus._find_brief_for_campaign(campaign_code)
            if brief:
                spec = nexus.build_brief_context_from_bq(brief, gold_index, query)
                if spec is not None:
                    return spec
        except Exception as exc:
            print(f"  [briefing ctx step 3] BQ lookup failed: {exc.__class__.__name__}: {exc}")

    # 4. Last resort: campaign name or raw query (BRONZE -- BriefingAgent will do its best).
    # Falls back to the raw user query when no campaign code was identified so the
    # system always produces a brief rather than returning None.
    name = campaign_code or query.strip()
    if name:
        return UniversalJSONSpec(
            campaign_name=name,
            campaign_code=name,
            campaign_sub_code=name,
            cadence="ad-hoc",
            medium="unspecified",
            campaign_tier="BRONZE",
            knowledge_source="nl_only",
            target_population=name,
            brief_agent_inputs={"raw_prompt": query},
        )

    return None


# Registry: maps intent_type -> context builder function.
# To support a new intent, add one entry here -- zero changes to route_by_intent.
_CONTEXT_BUILDERS: dict[str, Callable] = {
    "sizing_request":     _build_sizing_context,
    "campaign_execution": _build_sizing_context,
    "brief_generation":   _build_briefing_context,
    "brief_qa":           _build_briefing_context,
    "general_question":   lambda *_: None,
}


# ---------------------------------------------------------------------------
# Unified intent-based router
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
    allow_interactive: bool = True,
    session_memory=None,
    knowledge_ctx: Optional[KnowledgeContext] = None,
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

    # Step 4: Build intent-appropriate context via the registry.
    # Each intent type has its own builder; new intents register here, not in route_by_intent.
    context_builder = _CONTEXT_BUILDERS.get(it)
    spec: Optional[UniversalJSONSpec] = None
    if context_builder is not None:
        spec = context_builder(intent, query, nexus, gold_index, schema_snapshot, rules_registry, session_memory)
        if spec is not None and spec.discrepancy_flags:
            ThoughtDisplay.discrepancy_check(len(spec.discrepancy_flags), spec.discrepancy_flags)
            _print_discrepancy_audit(spec)

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

    # Display which knowledge assets were consulted before any agent fires.
    # Brief intents are skipped here — their knowledge box is shown after the
    # brief is generated so it reflects what the AI model actually used.
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
    if it not in ("brief_generation", "brief_qa"):
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
            if allow_interactive:
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
            # Semantic router: inject domain-filtered schemas so Quant only sees relevant tables.
            if knowledge_ctx is not None and intent.data_domains:
                filtered = knowledge_ctx.retrieve_schema_for_domains(intent.data_domains)
                if filtered and filtered != "<execution_schemas/>":
                    quant.set_domain_schemas(filtered)
            result = _direct_count_with_recovery(quant, request, rules_registry, allow_interactive=allow_interactive)
            if isinstance(result, QuantAuditLog):
                return None, result, None
            ThoughtDisplay.translate_nexus_error(result.error_summary)
            return None, None, None

    elif it in ("brief_generation", "brief_qa"):
        if spec is None:
            # Safety net: _build_briefing_context returned None despite four fallback steps
            # (which now include a raw-query fallback). This should be extremely rare.
            # Create a BRONZE spec from the raw query for any brief request so the system
            # always attempts brief generation rather than returning a silent failure.
            name = (intent.campaign_code or query).strip()
            if name:
                print(f"  [route_by_intent] WARNING: _build_briefing_context returned None. "
                      f"Using safety-net BRONZE spec from query.")
                spec = UniversalJSONSpec(
                    campaign_name=name,
                    campaign_code=name,
                    campaign_sub_code=name,
                    cadence="ad-hoc",
                    medium="unspecified",
                    campaign_tier="BRONZE",
                    knowledge_source="nl_only",
                    target_population=name,
                    brief_agent_inputs={"raw_prompt": query},
                )
        if spec is None:
            return None, None, None
        if not allow_interactive and hasattr(briefing, "set_non_interactive"):
            briefing.set_non_interactive()
        briefing.subscribe(spec)
        brief_output: BriefingOutput = briefing.execute()
        return spec, None, brief_output

    elif it == "campaign_execution":
        if spec is None:
            ThoughtDisplay.progress("No campaign context found. Switching to custom audience mode...")
            request = nexus.build_sizing_request_from_nl(query)
            if request is None:
                return None, None, None
            if knowledge_ctx is not None and intent.data_domains:
                filtered = knowledge_ctx.retrieve_schema_for_domains(intent.data_domains)
                if filtered and filtered != "<execution_schemas/>":
                    quant.set_domain_schemas(filtered)
            result = _direct_count_with_recovery(quant, request, rules_registry, allow_interactive=allow_interactive)
            if isinstance(result, QuantAuditLog):
                return None, result, None
            ThoughtDisplay.translate_nexus_error(result.error_summary)
            return None, None, None

        result = quant.audit_from_spec(spec)
        if isinstance(result, NexusErrorPayload):
            result = nexus.route_with_retry(spec.to_audience_sizing_request(), quant)
        if not isinstance(result, QuantAuditLog):
            if allow_interactive and _is_column_error(getattr(result, "error_summary", "")):
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

        if not allow_interactive and hasattr(briefing, "set_non_interactive"):
            briefing.set_non_interactive()
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
# Brief output formatters
# ---------------------------------------------------------------------------

def _print_targeting_criteria(brief: BriefingOutput) -> None:
    """Print only the structured targeting criteria (universe + exclusions)."""
    sep = "=" * 66
    thin = "-" * 44
    print()
    print(sep)
    print(f"  DATA BRIEF — {brief.campaign_name.upper()}")
    print(sep)
    print()
    if brief.structured_universe:
        print("  Initial Universe")
        print("  " + thin)
        print(f"  {brief.structured_universe}")
        print()
    if brief.structured_exclusions:
        print("  Exclusion Criteria")
        print("  " + thin)
        for i, excl in enumerate(brief.structured_exclusions, start=1):
            _print_wrapped(f"  {i}. {excl}", indent=5)
        print()
    tier_label = f"Tier: {brief.tier}"
    conf_label = f"Confidence: {brief.confidence_score:.0%}"
    print(f"  {tier_label:<22}  {conf_label}")
    print(sep)


def _print_final_brief(brief: BriefingOutput) -> None:
    """Print the complete finalized data brief — targeting criteria plus segments if present."""
    if brief.error_reason:
        ThoughtDisplay.error(f"Brief generation failed: {brief.error_reason}")
        return
    sep = "=" * 66
    thin = "-" * 44
    print()
    print(sep)
    print(f"  FINALIZED DATA BRIEF — {brief.campaign_name.upper()}")
    print(sep)
    print()

    print("  Initial Universe")
    print("  " + thin)
    universe = brief.structured_universe or "(not available)"
    _print_wrapped(f"  {universe}", indent=2)
    print()

    if brief.structured_exclusions:
        print("  Exclusion Criteria")
        print("  " + thin)
        for i, excl in enumerate(brief.structured_exclusions, start=1):
            _print_wrapped(f"  {i}. {excl}", indent=5)
        print()

    if brief.structured_segments:
        print("  Segmentation Criteria")
        print("  " + thin)
        for seg in brief.structured_segments:
            _print_wrapped(f"  {seg.name}: {seg.description}", indent=4)
        print()

    tier_label = f"Tier: {brief.tier}"
    conf_label = f"Confidence: {brief.confidence_score:.0%}"
    print(f"  {tier_label:<22}  {conf_label}")
    print(sep)
    ThoughtDisplay.show_knowledge_used(
        gold_campaign=brief.campaign_name,
        knowledge_sources=brief.knowledge_sources_used,
        confidence=brief.confidence_score,
    )


def _print_brief(brief: BriefingOutput) -> None:
    """Legacy display — used for non-brief intents that still produce a BriefingOutput.
    For brief_generation and brief_qa the new _print_final_brief is used instead."""
    _print_final_brief(brief)


def _print_wrapped(text: str, indent: int = 0, width: int = 66) -> None:
    """Print text with word-wrapping, preserving leading indent on continuation lines."""
    import textwrap
    # Determine the leading spaces already in text so the first line prints as-is.
    stripped = text.lstrip(" ")
    leading = len(text) - len(stripped)
    first_indent = " " * leading
    continuation = " " * indent
    wrapped = textwrap.fill(
        stripped,
        width=width - leading,
        initial_indent=first_indent,
        subsequent_indent=continuation,
    )
    print(wrapped)


# ---------------------------------------------------------------------------
# Interactive targeting criteria refinement loop
# ---------------------------------------------------------------------------

def _run_targeting_criteria_loop(
    spec: UniversalJSONSpec,
    brief_output: BriefingOutput,
    briefing: "BriefingAgent",
) -> BriefingOutput:
    """Show targeting criteria to the user and allow iterative refinement.

    Loops until the user confirms the criteria are correct.
    Returns the final confirmed BriefingOutput.
    """
    current = brief_output

    while True:
        if not current.structured_universe and not current.structured_exclusions:
            ThoughtDisplay.error(
                "Targeting criteria could not be parsed from the brief output. "
                "Please try generating the brief again."
            )
            return current

        _print_targeting_criteria(current)

        print()
        print("  Do these targeting criteria look correct?")
        print("  1  Yes, they look good")
        print("  2  No, I'd like to make a change")
        print()

        while True:
            response = input("  Enter 1 or 2: ").strip()
            if response == "1":
                print("\n  Targeting criteria confirmed.\n")
                return current
            elif response == "2":
                print(
                    "\n  Please describe the change you'd like to make — for example,\n"
                    "  'add an exclusion for customers contacted in the past 60 days'\n"
                    "  or 'the universe should only include English-language customers'.\n"
                )
                correction = input("  > ").strip()
                if correction:
                    print("\n  Updating targeting criteria...\n")
                    updated = briefing.refine_targeting(
                        universe=current.structured_universe or "",
                        exclusions=current.structured_exclusions or [],
                        user_correction=correction,
                    )
                    if updated.error_reason:
                        ThoughtDisplay.error(
                            f"Could not apply the change: {updated.error_reason}. "
                            "Please try rephrasing."
                        )
                    else:
                        current = updated
                break
            else:
                print("  Please enter 1 or 2.")


# ---------------------------------------------------------------------------
# Segmentation step — asked after targeting criteria are confirmed
# ---------------------------------------------------------------------------

def _run_segmentation_step(
    spec: UniversalJSONSpec,
    confirmed_brief: BriefingOutput,
    briefing: "BriefingAgent",
) -> BriefingOutput:
    """Ask the user whether segmentation criteria is needed.

    If yes, collects the segmentation basis, generates mutually exclusive segments,
    and returns a BriefingOutput with structured_segments populated.
    If no, returns the confirmed_brief unchanged.
    """
    print()
    print("  Would you like segmentation criteria as well?")
    print("  1  Yes, generate segmentation criteria")
    print("  2  No, the targeting criteria is all I need")
    print()

    while True:
        response = input("  Enter 1 or 2: ").strip()
        if response == "2":
            print()
            return confirmed_brief
        elif response == "1":
            print(
                "\n  Please describe the basis for segmentation — for example,\n"
                "  'by prior mobility tenure' or 'by whether the customer has\n"
                "  previously churned vs never had the service'.\n"
            )
            basis = input("  > ").strip()
            if not basis:
                return confirmed_brief
            print("\n  Generating segmentation criteria...\n")
            segmented = briefing.generate_segments(
                universe=confirmed_brief.structured_universe or "",
                exclusions=confirmed_brief.structured_exclusions or [],
                segmentation_basis=basis,
            )
            if segmented.error_reason:
                ThoughtDisplay.error(
                    f"Segmentation generation failed: {segmented.error_reason}. "
                    "The targeting criteria have been saved without segmentation."
                )
                return confirmed_brief
            # Merge: keep confirmed targeting criteria, add generated segments
            return confirmed_brief.model_copy(update={
                "structured_segments": segmented.structured_segments,
                "brief_markdown": segmented.brief_markdown,
                "knowledge_sources_used": (
                    (confirmed_brief.knowledge_sources_used or [])
                    + (segmented.knowledge_sources_used or [])
                ) or None,
            })
        else:
            print("  Please enter 1 or 2.")


# ---------------------------------------------------------------------------
# Ad-hoc feedback helper — FeedbackAgent without a full UniversalJSONSpec
# ---------------------------------------------------------------------------

def _run_adhoc_feedback(
    spec: Optional[UniversalJSONSpec],
    log: Optional[QuantAuditLog],
    query: str,
    correction: str,
    knowledge_ctx: Optional[KnowledgeContext] = None,
    non_interactive: bool = False,
) -> Optional[FeedbackOutput]:
    """Invoke FeedbackAgent for ad-hoc sizing, brief-only, and general-question corrections.

    Builds a minimal FeedbackInput from whatever context is available.  When no
    campaign spec exists the campaign_code defaults to 'AD_HOC' so FeedbackAgent
    can still extract and save universal rules that apply to future executions.
    Returns FeedbackOutput so callers can surface the interpretation summary or
    a clarifying question to the user.
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
        if non_interactive:
            agent.set_non_interactive()
        agent.subscribe(feedback_input)
        return agent.execute()
    except Exception as _exc:
        import logging as _logging
        _logging.getLogger(__name__).warning("_run_adhoc_feedback error: %s", _exc)
        return None


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
) -> str:
    """Numbered HITL menu for ad-hoc sizing, brief-only, and general questions.

    Returns an outcome string for audit logging:
      "yes"        — user confirmed result (option 1)
      "review_yes" — user reviewed then confirmed (option 2 -> 1)
      "review_no"  — user reviewed then reported issue (option 2 -> 3)
      "no"         — user reported issue directly (option 3)
    """
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
            return HITL_YES

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
                    return HITL_REVIEW_YES
                elif r2 == "3":
                    _collect_correction_and_feedback(spec, log, query, knowledge_ctx=knowledge_ctx)
                    return HITL_REVIEW_NO
                else:
                    print("  Please enter 1 or 3.")

        elif response == "3":
            _collect_correction_and_feedback(spec, log, query, knowledge_ctx=knowledge_ctx)
            return HITL_NO

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
    audit_logger: Optional[AuditLogger] = None,
) -> None:
    last_log: Optional[QuantAuditLog] = None
    session_id = uuid.uuid4().hex
    audit_user = os.getenv("USERNAME") or os.getenv("USER") or "unknown"

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

            _pipeline_start = time.perf_counter()

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

            _pipeline_ms = int((time.perf_counter() - _pipeline_start) * 1000)

            if log:
                last_log = log
                print(_format_audit_log(log))

            # Brief generation follows a guided interactive flow:
            #   1. Targeting criteria refinement loop (confirm or revise)
            #   2. Segmentation step (optional)
            #   3. Final brief display
            # All other intents that produce a brief use the legacy display.
            if brief_output is not None and intent.intent_type in ("brief_generation", "brief_qa"):
                if brief_output.error_reason or (
                    not brief_output.structured_universe and not brief_output.structured_exclusions
                ):
                    reason = brief_output.error_reason or (
                        "Targeting criteria could not be parsed. Please try again."
                    )
                    ThoughtDisplay.error(f"Brief generation failed: {reason}")
                else:
                    confirmed_brief = _run_targeting_criteria_loop(spec, brief_output, briefing)
                    final_brief = _run_segmentation_step(spec, confirmed_brief, briefing)
                    _print_final_brief(final_brief)
                    brief_output = final_brief
            elif intent.intent_type in ("brief_generation", "brief_qa") and brief_output is None:
                ThoughtDisplay.error(
                    "Brief generation did not produce output. "
                    "Please try again or check the system logs for details."
                )
            elif brief_output is not None:
                _print_brief(brief_output)

            # Step 7: HITL — always triggered for any execution that produced output.
            # Full HITL (spec + log) fires for campaign sizing and execution.
            # Simplified HITL fires for ad-hoc sizing, brief-only, and general questions.
            hitl_outcome: Optional[str] = None
            if spec is not None and log is not None:
                should_continue = hitl.prompt(spec, log, brief_output, intent_type=intent.intent_type)
                hitl_outcome = HITL_YES if should_continue else HITL_NO
                if rules_registry is not None:
                    rules_registry._load()
                if knowledge_ctx is not None:
                    knowledge_ctx.reload_rules()
                if audit_logger is not None:
                    try:
                        audit_logger.log(
                            session_id=session_id,
                            user=audit_user,
                            intent_type=intent.intent_type,
                            campaign_id=spec.campaign_code if spec else None,
                            sql=log.sql if log else None,
                            agent_called=_agent_called_for_intent(intent.intent_type),
                            hitl_outcome=hitl_outcome,
                            duration_ms=_pipeline_ms,
                        )
                    except Exception:
                        pass
                if not should_continue:
                    break
            elif spec is not None or log is not None or brief_output is not None or intent.intent_type == "general_question":
                hitl_outcome = _simplified_hitl(spec, log, brief_output, query, intent.intent_type, knowledge_ctx=knowledge_ctx)
                if rules_registry is not None:
                    rules_registry._load()
                if knowledge_ctx is not None:
                    knowledge_ctx.reload_rules()
                if audit_logger is not None:
                    try:
                        audit_logger.log(
                            session_id=session_id,
                            user=audit_user,
                            intent_type=intent.intent_type,
                            campaign_id=spec.campaign_code if spec else None,
                            sql=log.sql if log else None,
                            agent_called=_agent_called_for_intent(intent.intent_type),
                            hitl_outcome=hitl_outcome,
                            duration_ms=_pipeline_ms,
                        )
                    except Exception:
                        pass
            else:
                # No output produced (error path or unrecognised intent)
                if audit_logger is not None:
                    try:
                        audit_logger.log(
                            session_id=session_id,
                            user=audit_user,
                            intent_type=intent.intent_type,
                            campaign_id=None,
                            sql=None,
                            agent_called=_agent_called_for_intent(intent.intent_type),
                            hitl_outcome=None,
                            duration_ms=_pipeline_ms,
                        )
                    except Exception:
                        pass

        except Exception as _exc:
            # Fix 4: Never exit the console loop due to an agent failure.
            ThoughtDisplay.graceful_recovery()

        print()


# ---------------------------------------------------------------------------
# Shared runtime container — used by terminal mode and the API server
# ---------------------------------------------------------------------------

class VibeRuntime:
    """All shared objects initialized once at startup, shared across all requests."""

    def __init__(
        self,
        nexus: NexusAgent,
        quant: QuantAgent,
        briefing: BriefingAgent,
        gold_index: "GoldTierIndex",
        schema_snapshot: "SchemaSnapshot",
        hitl: "HITLAuditLoop",
        rules_registry: BusinessRulesRegistry,
        knowledge_ctx: KnowledgeContext,
        audit_logger: Optional[AuditLogger] = None,
    ) -> None:
        self.nexus = nexus
        self.quant = quant
        self.briefing = briefing
        self.gold_index = gold_index
        self.schema_snapshot = schema_snapshot  # SchemaSnapshot object; call .to_dict() as needed
        self.hitl = hitl
        self.rules_registry = rules_registry
        self.knowledge_ctx = knowledge_ctx
        self.audit_logger = audit_logger


class RequestResult:
    """Pipeline result returned by process_core_request(). No terminal I/O side effects."""

    def __init__(
        self,
        intent: IntentClassification,
        spec: Optional[UniversalJSONSpec],
        log: Optional[QuantAuditLog],
        brief_output: Optional[BriefingOutput],
        error: Optional[str] = None,
    ) -> None:
        self.intent = intent
        self.spec = spec
        self.log = log
        self.brief_output = brief_output
        self.error = error  # set when pipeline failed without producing output


def build_runtime() -> VibeRuntime:
    """Initialize all shared runtime objects. Called by both main() and the API server."""
    load_dotenv(_NEXUS_DIR / ".env")
    load_dotenv(_QUANT_DIR / ".env", override=False)
    load_dotenv(_BRIEFING_DIR / ".env", override=False)
    load_dotenv(_FEEDBACK_DIR / ".env", override=False)

    gold_index = GoldTierIndex()
    gold_index.load_from_file(_ARTIFACTS_DIR / "semantic_knowledge_index.json")

    schema_discovery = SchemaDiscoveryLayer(
        project="bi-srv-hsmdet-pr-7b9def",
        datasets=["adobe", "campaign_data", "gch_current"],
        cache_path=_ROOT / ".sdl_schema_cache.json",
    )
    snapshot = _load_adobe_schema_from_disk(_ARTIFACTS_DIR)
    if snapshot is None:
        snapshot = schema_discovery.get_snapshot()
    schema_str = schema_discovery.to_prompt_string(snapshot)

    nexus: NexusAgent = _AGENT_REGISTRY["nexus"]()
    quant: QuantAgent = _AGENT_REGISTRY["quant"]()
    briefing: BriefingAgent = _AGENT_REGISTRY["briefing"]()

    knowledge_ctx = KnowledgeContext(
        artifacts_dir=_ARTIFACTS_DIR,
        root_dir=_ROOT,
    )

    quant.set_runtime_schema(schema_str)
    nexus.set_runtime_schema_snapshot(snapshot.to_dict())
    briefing.set_runtime_schema(schema_str)
    briefing.set_gold_index(gold_index)

    nexus.set_knowledge_context(knowledge_ctx)
    quant.set_knowledge_context(knowledge_ctx)
    briefing.set_knowledge_context(knowledge_ctx)

    hitl = HITLAuditLoop(
        gold_index=gold_index,
        glossary_manager=GlossaryManager(str(_GLOSSARY_PATH)),
        failure_log_path=_ROOT / "semantic_failure_log.json",
        registry_path=_ROOT / "verified_app_registry.json",
    )
    hitl.set_knowledge_context(knowledge_ctx)

    rules_registry = BusinessRulesRegistry(_BUSINESS_RULES_PATH)
    audit_logger = AuditLogger(logs_dir=_ROOT / "logs")

    return VibeRuntime(
        nexus=nexus,
        quant=quant,
        briefing=briefing,
        gold_index=gold_index,
        schema_snapshot=snapshot,
        hitl=hitl,
        rules_registry=rules_registry,
        knowledge_ctx=knowledge_ctx,
        audit_logger=audit_logger,
    )


def generate_stuck_explanation(
    nexus: NexusAgent,
    query: str,
    intent,
    spec,
    brief_output,
) -> str:
    """Call the AI to explain why a request produced no result and ask one follow-up question.

    Fully request-agnostic -- collects whatever context is available and delegates
    all reasoning to NexusAgent.explain_stuck_request().
    """
    intent_type = intent.intent_type if intent else "unknown"
    knowledge_sources = list(intent.knowledge_sources_consulted or []) if intent else []
    campaign_identified = intent.campaign_identified if intent else False

    error_details = ""
    if brief_output is not None and getattr(brief_output, "error_reason", None):
        error_details = brief_output.error_reason
    elif not knowledge_sources:
        error_details = "No matching data sources were found for this request."

    return nexus.explain_stuck_request(
        original_query=query,
        intent_type=intent_type,
        knowledge_sources=knowledge_sources,
        error_details=error_details,
        campaign_identified=campaign_identified,
    )


def process_core_request(
    query: str,
    rt: VibeRuntime,
    session_id: Optional[str] = None,
    user: str = "unknown",
    session_memory=None,
    session_corrections: Optional[list] = None,
) -> RequestResult:
    """Execute the full pipeline for a single query without any terminal I/O.

    Suitable for API mode. Column errors are returned as RequestResult.error
    rather than triggering input(). HITL decisions are left to the caller.
    Writes an audit entry immediately after the pipeline (hitl_outcome=None
    because HITL button clicks arrive asynchronously in API mode).
    """
    _log = logging.getLogger(__name__)
    _start = time.perf_counter()
    try:
        ctx = _build_dynamic_context(query, rt.gold_index)
        # Inject session corrections so every agent in this request sees what the user
        # already told us was wrong. get_dynamic_context() builds the correction block.
        if rt.knowledge_ctx is not None and session_corrections:
            corrections_ctx = rt.knowledge_ctx.get_dynamic_context(
                query=query,
                session_corrections=session_corrections,
            )
            ctx = ((ctx + "\n" + corrections_ctx).strip() if ctx else corrections_ctx)
        if ctx:
            rt.nexus.set_session_context(ctx)
            rt.quant.set_session_context(ctx)

        intent = rt.nexus.classify_intent(query)
        spec, log, brief_output = route_by_intent(
            rt.nexus,
            rt.quant,
            rt.briefing,
            intent,
            query,
            rt.gold_index,
            rt.schema_snapshot.to_dict(),
            rt.rules_registry,
            allow_interactive=False,
            session_memory=session_memory,
            knowledge_ctx=rt.knowledge_ctx,
        )
        result = RequestResult(intent=intent, spec=spec, log=log, brief_output=brief_output)
        if rt.audit_logger is not None:
            try:
                rt.audit_logger.log(
                    session_id=session_id or "api",
                    user=user,
                    intent_type=intent.intent_type,
                    campaign_id=spec.campaign_code if spec else None,
                    sql=log.sql if log else None,
                    agent_called=_agent_called_for_intent(intent.intent_type),
                    hitl_outcome=None,
                    duration_ms=int((time.perf_counter() - _start) * 1000),
                )
            except Exception:
                pass
        return result
    except Exception as exc:
        _log.exception("process_core_request failed for query %r: %s", query[:80], exc)
        _fallback = IntentClassification(
            intent_type="general_question",
            confidence=0.0,
            campaign_identified=False,
        )
        return RequestResult(
            intent=_fallback,
            spec=None,
            log=None,
            brief_output=None,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _silence_google_noise()
    os.system("cls" if os.name == "nt" else "clear")

    rt = build_runtime()

    _print_kb_status(rt.gold_index, rt.schema_snapshot, len(rt.rules_registry._rules), rt.knowledge_ctx)

    # Phase 5B: startup health gate — checks Fuel iX, BigQuery ADC, and knowledge index.
    # Session proceeds on OK/WARN; blocked only on FAIL.
    _fuelix_api_key = os.getenv("FUELIX_API_KEY", "")
    _bq_project = os.getenv("BQ_PROJECT_ID", "bi-srv-hsmdet-pr-7b9def")
    run_startup_health_check(
        api_key=_fuelix_api_key,
        bq_project=_bq_project,
        artifacts_dir=_ARTIFACTS_DIR,
    )

    _run_console(
        rt.nexus,
        rt.quant,
        rt.briefing,
        rt.gold_index,
        rt.schema_snapshot.to_dict(),
        rt.hitl,
        rt.rules_registry,
        rt.knowledge_ctx,
        rt.audit_logger,
    )


if __name__ == "__main__":
    main()
