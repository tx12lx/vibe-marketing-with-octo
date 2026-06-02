"""
mvp_orchestrator.py — Vibe Marketing with OCTO
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

# Load both agent environments before any agent code is imported.
# override=False means the first file wins on conflicts.
load_dotenv(_NEXUS_DIR / ".env")
load_dotenv(_QUANT_DIR / ".env", override=False)

for _p in [str(_ROOT), str(_NEXUS_DIR), str(_QUANT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from nexus_agent import NexusAgent  # noqa: E402
from quant_agent import QuantAgent  # noqa: E402
from pydantic_schemas import QuantAuditLog  # noqa: E402


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
# Agent registry — the ONLY place agent classes are registered.
# ---------------------------------------------------------------------------
_AGENT_REGISTRY: dict[str, type] = {
    "nexus": NexusAgent,
    "quant": QuantAgent,
}


# ---------------------------------------------------------------------------
# Router — stateless, workflow-dispatch only
# ---------------------------------------------------------------------------

def route(
    nexus: NexusAgent,
    quant: QuantAgent,
    workflow: str,
    payload: dict,
) -> Optional[QuantAuditLog]:
    """Dispatch a classified request to the correct pipeline branch.

    workflow 'WORKFLOW_A': Ad-Hoc Exploratory Request — natural language audience
                           sizing. Runs a direct single-row count via Quant.

    workflow 'WORKFLOW_B': Structured Campaign Execution Request — named campaign
                           brief. Retrieves all active, non-cancelled deployment
                           records from bq_plan_camp_deploy_mdc using:
                             camp_id = 'AAL', sub_camp_id = 'AALBAU',
                             UPPER(campaign) = 'AAL MONTHLY EM',
                             UPPER(data_status) <> 'CANCELLED'
                           Nexus reads every returned field as the functional
                           business requirements of the campaign, applies the
                           Targeting Criteria isolation sieve (discarding copy
                           splits, language ratios, and creative version rules),
                           resolves any cross-record targeting discrepancies into
                           a unified instruction set, prints the Final Recommended
                           Targeting Criteria block, then hands the sieved payload
                           to Quant for the 7-stage waterfall audit with one
                           correction retry.
                           When the sieved exclusions list contains a GCH recency
                           suppression entry, Quant compiles the three-table LEFT
                           JOIN anti-join under the fixed GCH alias contract:
                             a_gch = bq_campaign_segment
                             b_gch = bq_campaign_communication  (MOB_BAN source)
                             c_gch = bq_campaign_description
                           TARGETING KEY LAW: inner SELECT must always be
                             SELECT DISTINCT b_gch.MOB_BAN — never a_gch or c_gch.
                           DATE CASTING LAW : lookback predicate must always be
                             DATE(a_gch.IN_HOME_DT) >= DATE_SUB(CURRENT_DATE(),
                             INTERVAL X DAY) — bare IN_HOME_DT comparisons are
                             a DATETIME/DATE type mismatch.
    """
    if workflow == "WORKFLOW_A":
        request = nexus.build_sizing_request_from_nl(payload["query"])
        if request is None:
            return None
        result = quant.direct_count(request)
        if isinstance(result, QuantAuditLog):
            return result
        print(f"\n  [ERROR]: {result.error_summary}")
        return None

    elif workflow == "WORKFLOW_B":
        request = nexus.build_sizing_request_from_brief(payload)
        if request is None:
            return None
        return nexus.route_with_retry(request, quant)

    else:
        print(f"\n[ERROR]: Unknown workflow '{workflow}'")
        return None


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
# Interactive console — dual-intent engine
# ---------------------------------------------------------------------------

def _run_console(nexus: NexusAgent, quant: QuantAgent) -> None:
    while True:
        print("  How can the OCTO team help you today?\n")
        query = input("  > ").strip()
        print()

        if not query:
            continue

        if query.lower() in ("q", "quit", "exit"):
            print("  Session closed.\n")
            break

        print("  [NEXUS AGENT] -> Analyzing intent...\n")
        workflow, payload = nexus.classify_and_route(query)
        log = route(nexus, quant, workflow, payload if payload is not None else {"query": query})
        if log:
            print(_format_audit_log(log))
        print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _silence_google_noise()
    os.system("cls" if os.name == "nt" else "clear")
    nexus: NexusAgent = _AGENT_REGISTRY["nexus"]()
    quant: QuantAgent = _AGENT_REGISTRY["quant"]()
    _run_console(nexus, quant)


if __name__ == "__main__":
    main()
