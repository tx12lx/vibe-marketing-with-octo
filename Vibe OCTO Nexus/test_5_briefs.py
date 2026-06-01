#!/usr/bin/env python3
"""Smoke test: fetch 5 random AAL briefs from BQ, translate via Fuel iX, validate."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent))

from core.bq_client import BQClient
from core.brief_fetcher import BriefFetcher, BriefFetchError
from core.claude_client import ClaudeClient
from core.glossary import GlossaryManager
from core.json_formatter import add_translation_metadata, save_output
from core.validators import VibeBriefingOutput

SEP = "-" * 64


def main():
    load_dotenv()

    bq_project = os.getenv("BQ_PROJECT", "bi-srv-hsmdet-pr-7b9def")
    bq_table = os.getenv("BQ_TABLE", "bi-srv-hsmdet-pr-7b9def.campaign_data.bq_plan_camp_deploy_mdc")
    portfolio = os.getenv("DEFAULT_PORTFOLIO", "AAL")

    config_dir = Path(__file__).parent / "config"
    system_prompt = (config_dir / "prompts" / "system_prompt.txt").read_text(encoding="utf-8")
    translate_prompt = (config_dir / "prompts" / "translate_prompt.txt").read_text(encoding="utf-8")
    bq_schema = json.loads((config_dir / "bq_schema.json").read_text(encoding="utf-8"))

    glossary_mgr = GlossaryManager(str(config_dir / "glossary_seed.json"))
    glossary_version = glossary_mgr.data.get("glossary_metadata", {}).get("version", "seed")

    print(f"BQ table:  {bq_table}")
    print(f"Portfolio: {portfolio}")
    print(f"Glossary:  seed ({len(glossary_mgr.terms)} terms)")
    print(f"Model:     {os.getenv('FUELIX_MODEL', 'claude-sonnet-4')}")
    print(SEP)

    print("Connecting to BigQuery...")
    bq = BQClient(bq_project)
    rows = bq.get_historical_briefs(bq_table, camp_id=portfolio, limit=5, random=True)
    print(f"Fetched {len(rows)} random rows")
    print(SEP)

    fetcher = BriefFetcher()
    claude = ClaudeClient()
    output_dir = Path(__file__).parent / "output" / "test_run"
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []

    for i, row in enumerate(rows, 1):
        campaign = str(row.get("campaign", f"unknown_{i}")).strip()
        url = str(row.get("databrief_link", "")).strip()
        safe_name = campaign.replace("/", "_").replace(" ", "_")[:50]

        print(f"\n[{i}/5] {campaign}")
        print(f"  URL: {url}")

        entry = {"campaign": campaign, "url": url}

        # --- Fetch ---
        try:
            brief_content = fetcher.fetch(url)
            brief_content = brief_content.strip()
        except BriefFetchError as exc:
            print(f"  FETCH FAILED: {exc}")
            entry["status"] = "fetch_failed"
            entry["error"] = str(exc)
            results.append(entry)
            continue

        if not brief_content:
            print(f"  SKIP: empty brief")
            entry["status"] = "empty_brief"
            results.append(entry)
            continue

        print(f"  Brief: {len(brief_content)} chars")

        # --- Build metadata ---
        medium_raw = (row.get("medium") or "").strip()
        medium_list = [m.strip() for m in medium_raw.split(",") if m.strip()]

        metadata = {
            "campaign_id": campaign,
            "portfolio": portfolio,
            "purpose": (row.get("campaign_purpose") or "").strip(),
            "cadence": (row.get("cadence") or "").strip(),
            "medium": medium_list,
            "target_base": (row.get("target_base") or "").strip(),
            "primary_products": (row.get("primary_products") or "").strip(),
        }

        # --- Translate ---
        try:
            raw = claude.translate_brief(
                brief_content=brief_content,
                system_prompt=system_prompt,
                translate_prompt_template=translate_prompt,
                bq_schema=json.dumps(bq_schema, indent=2),
                glossary_json=glossary_mgr.to_json_string(),
                metadata=metadata,
            )
            raw = add_translation_metadata(raw, glossary_version=glossary_version)
        except Exception as exc:
            print(f"  TRANSLATE FAILED: {exc}")
            entry["status"] = "translate_failed"
            entry["error"] = str(exc)
            results.append(entry)
            continue

        # --- Save raw output ---
        out_path = output_dir / f"brief_{i:02d}_{safe_name}.json"
        save_output(raw, str(out_path))

        # --- Validate ---
        try:
            validated = VibeBriefingOutput.model_validate(raw)
            r = validated.execution_readiness
            high_ambig = [a for a in validated.qc_flags.ambiguities if a.severity == "high"]

            print(f"  Validation: PASSED")
            print(f"  Completeness: {r.completeness_score:.0f}% | Confidence: {r.overall_confidence:.2f} | Ready: {r.ready_to_execute}")
            if high_ambig:
                print(f"  WARNING: {len(high_ambig)} high-severity ambiguity(ies):")
                for a in high_ambig:
                    print(f"    [{a.flag_id}] {a.field}: {a.issue}")
            if validated.qc_flags.glossary_misses:
                print(f"  INFO: {len(validated.qc_flags.glossary_misses)} glossary miss(es)")
            if r.blockers:
                print(f"  Blockers: {'; '.join(r.blockers)}")
            print(f"  Output: {out_path}")

            entry.update({
                "status": "ok",
                "completeness": r.completeness_score,
                "confidence": r.overall_confidence,
                "ready_to_execute": r.ready_to_execute,
                "high_ambiguities": len(high_ambig),
                "glossary_misses": len(validated.qc_flags.glossary_misses),
                "output_file": str(out_path),
            })

        except ValidationError as exc:
            print(f"  Validation: FAILED ({exc.error_count()} error(s))")
            for err in exc.errors()[:5]:
                loc = ".".join(str(l) for l in err["loc"])
                print(f"    {loc}: {err['msg']}")
            print(f"  Raw output saved: {out_path}")
            entry.update({
                "status": "validation_failed",
                "validation_errors": exc.error_count(),
                "output_file": str(out_path),
            })

        results.append(entry)

    # --- Summary ---
    print(f"\n{SEP}")
    print("SUMMARY")
    print(SEP)
    ok = [r for r in results if r["status"] == "ok"]
    not_ok = [r for r in results if r["status"] != "ok"]
    print(f"Passed: {len(ok)}/5   Failed: {len(not_ok)}/5\n")

    status_labels = {
        "ok": "PASS",
        "fetch_failed": "FAIL fetch",
        "empty_brief": "FAIL empty",
        "translate_failed": "FAIL translate",
        "validation_failed": "FAIL validate",
    }
    for r in results:
        label = status_labels.get(r["status"], r["status"])
        print(f"  [{label:<14}] {r['campaign']}")
        if r["status"] == "ok":
            print(f"                   completeness={r['completeness']:.0f}%  confidence={r['confidence']:.2f}  ready={r['ready_to_execute']}")

    if ok:
        print()
        avg_c = sum(r["completeness"] for r in ok) / len(ok)
        avg_conf = sum(r["confidence"] for r in ok) / len(ok)
        ready = sum(1 for r in ok if r["ready_to_execute"])
        print(f"  Avg completeness : {avg_c:.1f}%")
        print(f"  Avg confidence   : {avg_conf:.2f}")
        print(f"  Ready to execute : {ready}/{len(ok)}")

    print()


if __name__ == "__main__":
    main()
