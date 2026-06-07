"""Phase 1 Stabilization — integration tests for all three wins."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# Make root-level imports available when run directly.
sys.path.insert(0, str(Path(__file__).parent))

from knowledge_base.column_mapper import ColumnMapper, ColumnResolutionError
from core.execution_logger import ExecutionObserver
from core.glossary_curator import GlossaryCurator

_CONFIG = Path(__file__).parent / "knowledge_base" / "column_mapping.json"


# ===========================================================================
# WIN 1: ColumnMapper
# ===========================================================================

def test_column_mapper() -> None:
    mapper = ColumnMapper(_CONFIG)

    # --- exact matches ---
    bq_cols = [
        "camp_id", "sub_camp_id", "campaign_name", "targeting_summary",
        "segment_summary", "cadence", "medium", "campaign_purpose",
        "primary_products", "databrief_link", "is_active",
    ]
    mapping = mapper.resolve_columns(bq_cols)
    assert mapping["camp_id"] == "camp_id"
    assert mapping["active_flag"] == "is_active"
    assert mapping["medium"] == "medium"
    print("  [+] exact matches: OK")

    # --- case-insensitive matching ---
    bq_cols_upper = [col.upper() for col in bq_cols]
    mapping_upper = mapper.resolve_columns(bq_cols_upper)
    assert mapping_upper["camp_id"].upper() == "CAMP_ID"
    assert mapping_upper["active_flag"].upper() == "IS_ACTIVE"
    print("  [+] case-insensitive: OK")

    # --- fuzzy matching (pattern substring of BQ column name) ---
    bq_fuzzy = [
        "cmp_id",          # matches pattern "cmp_id" for camp_id
        "sub_camp_id", "campaign_name", "target_note", "segment_summary",
        "cadence", "media_type", "campaign_purpose", "product_list",
        "brief_link", "active",
    ]
    mapping_fuzzy = mapper.resolve_columns(bq_fuzzy)
    assert mapping_fuzzy["camp_id"] == "cmp_id"
    assert mapping_fuzzy["targeting_summary"] == "target_note"
    assert mapping_fuzzy["medium"] == "media_type"
    print("  [+] fuzzy matching: OK")

    # --- missing required column raises ColumnResolutionError ---
    bq_missing = ["campaign_name", "medium"]  # missing most required columns
    try:
        mapper.resolve_columns(bq_missing)
        raise AssertionError("Expected ColumnResolutionError was not raised")
    except ColumnResolutionError as exc:
        msg = str(exc)
        assert "Column resolution failed" in msg
        assert "camp_id" in msg
        assert "Available BQ columns" in msg
    print("  [+] ColumnResolutionError on missing required columns: OK")

    # --- validate_required_columns ---
    full_mapping = {col: col for col in [
        "camp_id", "sub_camp_id", "campaign_name", "targeting_summary",
        "segment_summary", "cadence", "medium", "campaign_purpose",
        "primary_products", "databrief_link", "active_flag",
    ]}
    assert mapper.validate_required_columns(full_mapping) is True
    partial = {"camp_id": "camp_id"}
    assert mapper.validate_required_columns(partial) is False
    print("  [+] validate_required_columns: OK")

    print("test_column_mapper: PASS")


# ===========================================================================
# WIN 2: ExecutionObserver
# ===========================================================================

def test_execution_observer() -> None:
    observer = ExecutionObserver()

    observer.log("ingestion", "bq_fetch", status="OK", duration_ms=421, rows_fetched=1047)
    observer.log("ingestion", "brief_fetch", status="WARN", duration_ms=312,
                 successful=287, failed=8)
    observer.log("ingestion", "complete", status="OK", duration_ms=733,
                 gold_count=38, silver_count=287, bronze_count=722)
    observer.log("schema_discovery", "cache_check", status="OK", duration_ms=5,
                 cache_hit=False)
    observer.log("schema_discovery", "fetch_schema", status="OK", duration_ms=145,
                 columns=87)
    observer.log("schema_discovery", "complete", status="OK", duration_ms=150)
    observer.add_alert("8 brief fetch failures (codes: PFE, KI, TWA, ...)")
    observer.add_alert("Glossary staging queue has 1 pending entry")

    # --- total_ms ---
    report = observer.emit_report()
    assert report.total_ms == 421 + 312 + 733 + 5 + 145 + 150, (
        f"total_ms mismatch: {report.total_ms}"
    )
    print("  [+] total_ms calculation: OK")

    # --- success flag ---
    assert report.success is True
    print("  [+] success=True when no FAIL: OK")

    # --- alerts tracked ---
    assert len(report.alerts) == 2
    assert "PFE" in report.alerts[0]
    print("  [+] alerts tracked: OK")

    # --- JSON output ---
    raw_json = report.to_json()
    data = json.loads(raw_json)
    assert data["total_ms"] == report.total_ms
    assert len(data["logs"]) == 6
    assert data["logs"][0]["pillar"] == "ingestion"
    assert data["logs"][0]["metadata"]["rows_fetched"] == 1047
    print("  [+] JSON output valid: OK")

    # --- Markdown output ---
    md = report.to_markdown()
    assert "Execution Report" in md
    assert "ingestion" in md.lower() or "Ingestion" in md
    assert "bq_fetch" in md
    assert "rows_fetched" in md
    assert "[!] Alerts" in md
    assert "PFE" in md
    print("  [+] Markdown output valid: OK")

    # --- success=False when a FAIL is logged ---
    observer2 = ExecutionObserver()
    observer2.log("workers", "build_spec", status="FAIL", duration_ms=50)
    observer2.log("workers", "retry", status="OK", duration_ms=10)
    report2 = observer2.emit_report()
    assert report2.success is False
    print("  [+] success=False when FAIL present: OK")

    # --- save_report writes readable JSON ---
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.json"
        observer.save_report(out)
        assert out.exists()
        saved = json.loads(out.read_text(encoding="utf-8"))
        assert saved["run_id"] == report.run_id
    print("  [+] save_report: OK")

    print("test_execution_observer: PASS")


# ===========================================================================
# WIN 3: GlossaryCurator
# ===========================================================================

def test_glossary_curator() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        staging_path = Path(tmp) / "staging.json"
        curator = GlossaryCurator(staging_path)

        # --- stage an entry ---
        entry = {
            "term": "prime_sub",
            "category": "column_name",
            "confidence_score": 0.3,
            "source": "failure_inference",
            "note": "User corrected: column is prime_sub not primary_sub",
        }
        eid = curator.add_to_staging(
            entry=entry,
            inferred_from="failure_log_2026_06_05_143200",
            reason="User correction",
        )
        assert len(curator.list_staged()) == 1
        assert curator.list_staged()[0].entry_id == eid
        print("  [+] add_to_staging: OK")

        # --- stage a second entry ---
        eid2 = curator.add_to_staging(
            entry={"term": "mob_ban", "category": "column_name", "confidence_score": 0.8},
            inferred_from="failure_log_2026_06_06_090000",
            reason="Observed in HITL correction",
        )
        assert len(curator.list_staged()) == 2

        # --- approve first entry ---
        approved_entry = curator.approve(eid, curator="alice@telus.com", reason="Confirmed")
        assert approved_entry["term"] == "prime_sub"
        assert len(curator.list_staged()) == 1
        assert curator.list_staged()[0].entry_id == eid2
        print("  [+] approve: OK")

        # --- reject second entry ---
        curator.reject(eid2, curator="bob@telus.com", reason="Duplicate term")
        assert len(curator.list_staged()) == 0
        print("  [+] reject: OK")

        # --- audit trail ---
        summary = curator.get_status_summary()
        assert summary["staged_count"] == 0
        assert summary["approved_count"] == 1
        assert summary["rejected_count"] == 1
        assert summary["pending_curation"] is False
        print("  [+] get_status_summary: OK")

        # --- persistence: reload from file, state preserved ---
        curator2 = GlossaryCurator(staging_path)
        summary2 = curator2.get_status_summary()
        assert summary2["approved_count"] == 1
        assert summary2["rejected_count"] == 1
        assert summary2["staged_count"] == 0
        print("  [+] persistence / reload: OK")

        # --- stage count in summary after new entry ---
        curator2.add_to_staging(
            entry={"term": "pending_term", "category": "column_name"},
            inferred_from="test",
            reason="test",
        )
        summary3 = curator2.get_status_summary()
        assert summary3["staged_count"] == 1
        assert summary3["pending_curation"] is True
        print("  [+] pending_curation flag: OK")

    print("test_glossary_curator: PASS")


# ===========================================================================
# Runner
# ===========================================================================

if __name__ == "__main__":
    failures: list[str] = []

    for name, fn in [
        ("test_column_mapper", test_column_mapper),
        ("test_execution_observer", test_execution_observer),
        ("test_glossary_curator", test_glossary_curator),
    ]:
        print(f"\n--- {name} ---")
        try:
            fn()
        except Exception as exc:
            print(f"  [!] FAIL: {exc}")
            failures.append(name)

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        sys.exit(1)
    else:
        print("All tests: PASS")
