"""
hitl/audit_loop.py — Pillar 5: Human-in-the-Loop Audit & Additive App Registry Flywheel

HITLAuditLoop is the sole gate for GOLD tier promotion. A human must answer Y
at the audit prompt for a record to be marked confirmed. A N response captures
the correction in structured form and updates the registry with an absolute
blueprint override that NexusAgent applies on the next execution cycle.

Design constraints:
- All file writes use atomic read-modify-write via os.replace() — no partial writes.
- No BigQuery interaction of any kind.
- No external dependencies beyond the standard library and project-local schemas.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

_HITL_DIR = Path(__file__).resolve().parent
_ROOT_DIR = _HITL_DIR.parent
_NEXUS_DIR = _ROOT_DIR / "Vibe OCTO Nexus"

for _p in [str(_ROOT_DIR), str(_NEXUS_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pydantic_schemas import (  # noqa: E402
    BriefingOutput,
    QuantAuditLog,
    SemanticFailureLog,
    UniversalJSONSpec,
)

if TYPE_CHECKING:
    from core.glossary import GlossaryManager
    from knowledge_base.tier_index import GoldTierIndex

# SQL noise tokens excluded from glossary gap detection.
_SQL_NOISE: frozenset[str] = frozenset({
    "and", "or", "not", "in", "is", "null", "true", "false",
    "select", "from", "where", "upper", "lower", "trim",
    "interval", "day", "month", "year", "case", "when", "then", "else",
    "end", "like", "between", "distinct", "join", "left", "inner",
    "outer", "on", "as", "with", "having", "group", "order", "by",
    "asc", "desc", "limit", "cast", "coalesce", "date",
})


class HITLAuditLoop:
    def __init__(
        self,
        gold_index: "GoldTierIndex",
        glossary_manager: "GlossaryManager",
        failure_log_path: Path,
        registry_path: Path,
    ) -> None:
        self._gold_index = gold_index
        self._glossary_manager = glossary_manager
        self._failure_log_path = Path(failure_log_path)
        self._registry_path = Path(registry_path)

    def prompt(
        self,
        spec: UniversalJSONSpec,
        audit_log: QuantAuditLog,
        briefing_output: Optional[BriefingOutput] = None,
    ) -> bool:
        """Present the HITL gate and dispatch to the YES or NO handler.

        Returns True to continue the console loop, False to exit it.
        The console loop must break when this returns False.
        """
        response = input(
            "\nDo you want to approve this campaign blueprint for production execution? (Y/N): "
        ).strip().upper()

        if response == "Y":
            self._handle_yes(spec, audit_log, briefing_output)
            return True
        else:
            self._handle_no(spec, audit_log)
            return False

    # ------------------------------------------------------------------
    # YES path — confirm, promote, flywheel
    # ------------------------------------------------------------------

    def _handle_yes(
        self,
        spec: UniversalJSONSpec,
        audit_log: QuantAuditLog,
        briefing_output: Optional[BriefingOutput],
    ) -> None:
        targeting_summary = self._build_targeting_summary(spec, audit_log)
        segment_summary = self._build_segment_summary(audit_log)

        registry_entry: dict = {
            "validated_at": datetime.now(tz=timezone.utc).isoformat(),
            "camp_id": spec.campaign_code,
            "sub_camp_id": spec.campaign_sub_code,
            "campaign_name": spec.campaign_name,
            "raw_input_prompt": (
                spec.brief_agent_inputs.get("raw_prompt", "")
                if spec.brief_agent_inputs
                else ""
            ),
            "universal_json_spec": spec.model_dump(),
            "targeting_summary": targeting_summary,
            "segment_summary": segment_summary,
            "final_count": audit_log.final_count,
            "hitl_confirmed": True,
        }

        self._upsert_registry(
            spec.campaign_code, spec.campaign_sub_code, registry_entry
        )

        self._gold_index.promote_in_memory(
            camp_id=spec.campaign_code,
            sub_camp_id=spec.campaign_sub_code,
            targeting_summary=targeting_summary,
            segment_summary=segment_summary,
        )

        print(
            f"\n[FLYWHEEL] Campaign '{spec.campaign_name}' validated and saved "
            "to local App Registry."
        )
        print(
            "[FLYWHEEL] It will be compiled as a permanent GOLD tier blueprint "
            "on the next scheduled refresh."
        )

    # ------------------------------------------------------------------
    # NO path — capture correction, log failure, update registry override
    # ------------------------------------------------------------------

    def _handle_no(self, spec: UniversalJSONSpec, audit_log: QuantAuditLog) -> None:
        correction = input(
            "\nPlease enter your manual correction or operational override instructions: "
        ).strip()

        failure_type = self._infer_failure_type(correction, spec)
        glossary_gaps = self._find_glossary_gaps(
            spec.filters, spec.exclusion_layers or []
        )

        log_entry = SemanticFailureLog(
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            campaign_code=spec.campaign_code,
            worker_id="quant_v1",
            raw_input=json.dumps(spec.model_dump(), default=str),
            generated_output=audit_log.sql,
            correction_description=correction,
            inferred_failure_type=failure_type,
            glossary_gaps=glossary_gaps,
        )

        self._append_failure_log(log_entry)
        self._glossary_manager.patch_from_failure(log_entry)

        if spec.gold_blueprint_id:
            self._gold_index.deprioritize(spec.campaign_code)

        # Write the correction as an absolute blueprint override in the registry.
        # This is the entry NexusAgent reads at the start of the next build_universal_spec()
        # cycle to short-circuit conflicting zero-shot reasoning.
        override_entry: dict = {
            "recorded_at": datetime.now(tz=timezone.utc).isoformat(),
            "camp_id": spec.campaign_code,
            "sub_camp_id": spec.campaign_sub_code,
            "campaign_name": spec.campaign_name,
            "correction_text": correction,
            "hitl_confirmed": False,
            "universal_json_spec": spec.model_dump(),
            "rejected_filters": spec.filters,
            "rejected_exclusion_layers": spec.exclusion_layers or [],
            "live_schema_snapshot_summary": {
                "column_count": len(
                    spec.runtime_schema_snapshot.get("columns", [])
                ) if spec.runtime_schema_snapshot else 0,
                "fetched_at": (
                    spec.runtime_schema_snapshot.get("fetched_at", "")
                    if spec.runtime_schema_snapshot else ""
                ),
            },
        }
        self._upsert_registry(
            spec.campaign_code, spec.campaign_sub_code, override_entry
        )

        print(
            "\n[AUDIT] Override instructions captured. "
            "Semantic failure logged to 'semantic_failure_log.json'."
        )
        print(
            "[AUDIT] 'verified_app_registry.json' updated with operator correction "
            "as absolute blueprint override."
        )
        print(
            "[FLYWHEEL] Semantic failure logged. Glossary updated. "
            "Zero-code self-learning applied."
        )
        print("\n[AUDIT] Memory layer updated. Session closed.\n")

    # ------------------------------------------------------------------
    # Registry: safe atomic upsert by camp_id + sub_camp_id
    # ------------------------------------------------------------------

    def _upsert_registry(
        self, camp_id: str, sub_camp_id: str, entry: dict
    ) -> None:
        """Read-modify-write the registry, replacing any existing record for this key.

        Uses os.replace() for atomicity — the old file is never partially overwritten.
        """
        data = self._read_registry()
        records: list[dict] = data.get("records", [])

        key_camp = camp_id.upper()
        key_sub = sub_camp_id.upper()

        updated = False
        for i, rec in enumerate(records):
            if (
                rec.get("camp_id", "").upper() == key_camp
                and rec.get("sub_camp_id", "").upper() == key_sub
            ):
                records[i] = entry
                updated = True
                break

        if not updated:
            records.append(entry)

        data["records"] = records
        self._write_atomic(self._registry_path, data)

    def _read_registry(self) -> dict:
        if self._registry_path.exists():
            try:
                return json.loads(
                    self._registry_path.read_text(encoding="utf-8")
                )
            except Exception:
                pass
        return {"schema_version": "1.0", "records": []}

    # ------------------------------------------------------------------
    # Failure log: flat JSON array, append-only
    # ------------------------------------------------------------------

    def _append_failure_log(self, log_entry: SemanticFailureLog) -> None:
        records: list[dict] = []
        if self._failure_log_path.exists():
            try:
                raw = json.loads(
                    self._failure_log_path.read_text(encoding="utf-8")
                )
                if isinstance(raw, list):
                    records = raw
            except Exception:
                records = []

        records.append(log_entry.model_dump())
        self._write_atomic(self._failure_log_path, records)

    # ------------------------------------------------------------------
    # Atomic write helper
    # ------------------------------------------------------------------

    @staticmethod
    def _write_atomic(target_path: Path, data: object) -> None:
        """Serialize data to JSON and replace target_path atomically."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=target_path.parent, suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
            os.replace(tmp_name, str(target_path))
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # Summary builders
    # ------------------------------------------------------------------

    def _build_targeting_summary(
        self, spec: UniversalJSONSpec, audit_log: QuantAuditLog
    ) -> str:
        parts: list[str] = [spec.target_population]
        if spec.filters:
            parts.append("Filters: " + "; ".join(spec.filters))
        if spec.exclusion_layers:
            parts.append("Exclusions: " + "; ".join(spec.exclusion_layers))
        return " | ".join(parts)

    def _build_segment_summary(self, audit_log: QuantAuditLog) -> str:
        if not audit_log.waterfall:
            return f"Final count: {audit_log.final_count:,}."
        base = audit_log.waterfall[0].audience_count
        steps = len(audit_log.waterfall)
        return (
            f"{steps}-step waterfall. "
            f"Base universe {base:,}. "
            f"Final count {audit_log.final_count:,}."
        )

    # ------------------------------------------------------------------
    # Failure type inference
    # ------------------------------------------------------------------

    def _infer_failure_type(
        self, correction: str, spec: UniversalJSONSpec
    ) -> str:
        lower = correction.lower()

        # schema_gap: correction text contains a snake_case token that is absent
        # from the live schema — indicates the user is flagging a missing/unknown column.
        if spec.runtime_schema_snapshot:
            known_cols: set[str] = {
                c.get("column_name", "").lower()
                for c in spec.runtime_schema_snapshot.get("columns", [])
                if isinstance(c, dict) and c.get("column_name")
            }
            if known_cols:
                col_tokens = re.findall(
                    r"\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b", lower
                )
                if col_tokens and any(t not in known_cols for t in col_tokens):
                    return "schema_gap"

        if "missing" in lower and "exclusion" in lower:
            return "missing_exclusion"
        if "tier" in lower or "blueprint" in lower:
            return "tier_mismatch"
        if "wrong column" in lower or (
            "column" in lower and "filter" not in lower
        ):
            return "wrong_column"
        if "filter" in lower:
            return "wrong_filter"
        return "wrong_filter"

    # ------------------------------------------------------------------
    # Glossary gap detection
    # ------------------------------------------------------------------

    def _find_glossary_gaps(
        self, filters: list[str], exclusion_layers: list[str]
    ) -> list[str]:
        """Return filter/exclusion tokens not found in the loaded glossary terms."""
        known_terms: set[str] = set(self._glossary_manager.terms.keys())
        blob = " ".join(list(filters) + list(exclusion_layers)).lower()
        tokens = re.findall(r"\b([a-z][a-z0-9_]{2,})\b", blob)

        gaps: list[str] = []
        seen: set[str] = set()
        for tok in tokens:
            if tok not in _SQL_NOISE and tok not in known_terms and tok not in seen:
                seen.add(tok)
                gaps.append(tok)

        return gaps[:10]
