from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class StagedGlossaryEntry:
    entry_id: str
    entry: dict
    inferred_from: str
    inferred_at: str
    reason: str


@dataclass
class CurationAction:
    action: str          # "approve" or "reject"
    entry_id: str
    curator: str
    reason: str
    actioned_at: str


class GlossaryCurator:
    def __init__(self, staging_path: Path) -> None:
        self._path = staging_path
        self._staged: list[StagedGlossaryEntry] = []
        self._actions: list[CurationAction] = []
        self._load_staging()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_to_staging(
        self, entry: dict, inferred_from: str, reason: str
    ) -> str:
        entry_id = str(uuid.uuid4())
        staged = StagedGlossaryEntry(
            entry_id=entry_id,
            entry=entry,
            inferred_from=inferred_from,
            inferred_at=_now_iso(),
            reason=reason,
        )
        self._staged.append(staged)
        self._persist_staging()
        return entry_id

    def list_staged(self) -> list[StagedGlossaryEntry]:
        return list(self._staged)

    def approve(
        self, entry_id: str, curator: str = "system", reason: str = ""
    ) -> dict:
        staged = self._pop_staged(entry_id)
        self._actions.append(
            CurationAction(
                action="approve",
                entry_id=entry_id,
                curator=curator,
                reason=reason,
                actioned_at=_now_iso(),
            )
        )
        self._persist_staging()
        return staged.entry

    def reject(
        self, entry_id: str, curator: str = "system", reason: str = ""
    ) -> None:
        self._pop_staged(entry_id)
        self._actions.append(
            CurationAction(
                action="reject",
                entry_id=entry_id,
                curator=curator,
                reason=reason,
                actioned_at=_now_iso(),
            )
        )
        self._persist_staging()

    def get_status_summary(self) -> dict:
        approved = sum(1 for a in self._actions if a.action == "approve")
        rejected = sum(1 for a in self._actions if a.action == "reject")
        staged_count = len(self._staged)
        return {
            "staged_count": staged_count,
            "approved_count": approved,
            "rejected_count": rejected,
            "pending_curation": staged_count > 0,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _pop_staged(self, entry_id: str) -> StagedGlossaryEntry:
        for i, entry in enumerate(self._staged):
            if entry.entry_id == entry_id:
                return self._staged.pop(i)
        raise KeyError(f"No staged entry with entry_id={entry_id!r}")

    def _load_staging(self) -> None:
        if not self._path.exists():
            return
        with open(self._path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self._staged = [
            StagedGlossaryEntry(**item) for item in data.get("staged", [])
        ]
        self._actions = [
            CurationAction(**item) for item in data.get("actions", [])
        ]

    def _persist_staging(self) -> None:
        data = {
            "schema_version": "1.0",
            "staged": [asdict(e) for e in self._staged],
            "actions": [asdict(a) for a in self._actions],
            "persisted_at": _now_iso(),
        }
        self._path.write_text(json.dumps(data, indent=2), encoding="utf-8")
