"""
mvp_orchestrator.py — Vibe Marketing with OCTO
Central decoupled router.

Design principle: this file is the only wiring layer. It knows which agents
exist and which mode maps to which agent method — nothing else. Agent cores
are fully independent and test in isolation.

Scaling to new agents requires only two changes here:
  1. Import the new agent class.
  2. Add it to _AGENT_REGISTRY.
Zero changes to Nexus, Quant, or pydantic_schemas.

To add Vibe OCTO Strategist (telecom standards benchmarking):
  from strategist_agent import StrategistAgent
  _AGENT_REGISTRY["strategist"] = StrategistAgent

To scale to RAG pattern across 1,000+ briefs:
  from nexus_rag_agent import NexusRAGAgent
  _AGENT_REGISTRY["nexus"] = NexusRAGAgent
"""
from __future__ import annotations

import contextlib
import io
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
from pydantic_schemas import NexusErrorPayload, QuantAuditLog  # noqa: E402

# ---------------------------------------------------------------------------
# Boot cleanliness — suppress Google Cloud auth / quota noise before any
# agent code imports google.auth or google.cloud at initialisation time.
# ---------------------------------------------------------------------------

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
# Adding a new agent here is sufficient; no downstream code changes needed.
# ---------------------------------------------------------------------------
_AGENT_REGISTRY: dict[str, type] = {
    "nexus": NexusAgent,
    "quant": QuantAgent,
}


# ---------------------------------------------------------------------------
# Router — stateless, mode-dispatch only
# ---------------------------------------------------------------------------

def route(
    nexus: NexusAgent,
    quant: QuantAgent,
    mode: str,
    payload: dict,
) -> Optional[QuantAuditLog]:
    """Dispatch a request to the correct pipeline branch.

    mode 'size_campaign' : Path 1 — automated sizing from a pre-loaded brief.
                           Runs the full waterfall audit with one correction retry.
    mode 'nl_query'      : Path 2 — natural language audience description.
                           Bypasses the audit loop; executes a direct single-row count.

    Adding a new mode (e.g. 'strategist_benchmark') requires adding one elif
    here and a corresponding method on the relevant agent — nothing else.
    """
    if mode == "size_campaign":
        print("\n[NEXUS AGENT] -> Analyzing intent...")
        print("  Recognized campaign brief sizing request. Extracting targeting parameters,")
        print("  filter layers, and exclusion rules...\n")
        request = nexus.build_sizing_request_from_brief(payload)
        if request is None:
            return None
        return nexus.route_with_retry(request, quant)

    elif mode == "nl_query":
        print("\n[NEXUS AGENT] -> Analyzing intent...")
        print("  Recognized ad hoc sizing request. Extracting parameters, filters, and")
        print("  cross-entity exclusions...\n")
        request = nexus.build_sizing_request_from_nl(payload["query"])
        if request is None:
            return None
        result = quant.direct_count(request)
        if isinstance(result, QuantAuditLog):
            return result
        print(f"\n  [ERROR]: {result.error_summary}")
        return None

    else:
        print(f"\n[ERROR]: Unknown routing mode '{mode}'")
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
# Interactive console
# ---------------------------------------------------------------------------

def _run_console(nexus: NexusAgent, quant: QuantAgent) -> None:
    while True:
        print("  How can the OCTO team help you today?\n")
        print("  [1] Size Campaign      — automated sizing from pre-loaded briefs")
        print("  [2] Natural Language   — describe your audience in plain English")
        print("  [q] Quit")

        choice = input("\n  Select: ").strip().lower()
        print()

        if choice == "q":
            print("  Session closed.\n")
            break

        elif choice == "1":
            # ------------------------------------------------------------------
            # Path 1 — Size Campaign
            # ------------------------------------------------------------------
            if not nexus.briefs:
                print("  No briefs loaded. Check BQ connectivity or local glossary.\n")
                continue

            print("  Pre-loaded Campaigns")
            print("  " + "-" * 62)
            for i, brief in enumerate(nexus.briefs, 1):
                name = brief.get("campaign_name", "?")
                code = brief.get("campaign_code", "?")
                cadence = brief.get("cadence", "?") or "—"
                medium = brief.get("medium", "?") or "—"
                print(f"  [{i}] {name:<32} {code:<14} {cadence:<10} {medium}")
            print()

            sel = input("  Select campaign number: ").strip()
            try:
                selected = nexus.briefs[int(sel) - 1]
            except (ValueError, IndexError):
                print("  Invalid selection.\n")
                continue

            print(f"\n  Sizing '{selected.get('campaign_name', '?')}' ...\n")
            log = route(nexus, quant, "size_campaign", selected)
            if log:
                print(_format_audit_log(log))

        elif choice == "2":
            # ------------------------------------------------------------------
            # Path 2 — Natural Language Query
            # ------------------------------------------------------------------
            query = input("  Describe your audience: ").strip()
            if not query:
                print()
                continue
            print("\n  Processing with Nexus ...\n")
            log = route(nexus, quant, "nl_query", {"query": query})
            if log:
                print(_format_audit_log(log))

        else:
            print("  Invalid selection. Enter 1, 2, or q.\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    _silence_google_noise()
    os.system("cls" if os.name == "nt" else "clear")
    _init_buf = io.StringIO()
    with contextlib.redirect_stderr(_init_buf), contextlib.redirect_stdout(_init_buf):
        nexus: NexusAgent = _AGENT_REGISTRY["nexus"]()
        quant: QuantAgent = _AGENT_REGISTRY["quant"]()
    _run_console(nexus, quant)


if __name__ == "__main__":
    main()
