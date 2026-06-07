from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ExecutionLog:
    timestamp: str
    pillar: str
    milestone: str
    status: str
    duration_ms: int
    metadata: dict = field(default_factory=dict)


@dataclass
class ExecutionReport:
    run_id: str
    started_at: str
    completed_at: str
    total_ms: int
    success: bool
    logs: list[ExecutionLog]
    alerts: list[str]

    def to_json(self) -> str:
        data = {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "total_ms": self.total_ms,
            "success": self.success,
            "logs": [asdict(log) for log in self.logs],
            "alerts": self.alerts,
        }
        return json.dumps(data, indent=2)

    def to_markdown(self) -> str:
        status_icon = "OK" if self.success else "FAIL"
        lines: list[str] = [
            f"# Execution Report [{status_icon}]",
            f"**Run ID:** {self.run_id}",
            f"**Total Time:** {self.total_ms:,} ms",
            f"**Started:** {self.started_at}",
            "",
        ]

        # Group logs by pillar, preserving insertion order
        pillars: dict[str, list[ExecutionLog]] = {}
        for log in self.logs:
            pillars.setdefault(log.pillar, []).append(log)

        for pillar, entries in pillars.items():
            pillar_ok = all(e.status != "FAIL" for e in entries)
            pillar_icon = "OK" if pillar_ok else "FAIL"
            if any(e.status == "WARN" for e in entries) and pillar_ok:
                pillar_icon = "WARN"
            lines.append(f"## [{pillar_icon}] {pillar.replace('_', ' ').title()}")
            for entry in entries:
                if entry.status == "OK":
                    sym = "+"
                elif entry.status == "WARN":
                    sym = "~"
                else:
                    sym = "!"
                lines.append(
                    f"- [{sym}] {entry.milestone} [{entry.duration_ms} ms] {entry.status}"
                )
                for k, v in entry.metadata.items():
                    lines.append(f"    - {k}: {v}")
            lines.append("")

        if self.alerts:
            lines.append("## [!] Alerts")
            for alert in self.alerts:
                lines.append(f"- {alert}")

        return "\n".join(lines)


class ExecutionObserver:
    def __init__(self) -> None:
        ts = _now_iso()
        suffix = uuid.uuid4().hex[:6]
        self._run_id = f"{ts}-{suffix}"
        self._started_at = ts
        self._logs: list[ExecutionLog] = []
        self._alerts: list[str] = []

    def log(
        self,
        pillar: str,
        milestone: str,
        status: str = "OK",
        duration_ms: int = 0,
        **metadata,
    ) -> None:
        self._logs.append(
            ExecutionLog(
                timestamp=_now_iso(),
                pillar=pillar,
                milestone=milestone,
                status=status,
                duration_ms=duration_ms,
                metadata=dict(metadata),
            )
        )

    def add_alert(self, alert: str) -> None:
        self._alerts.append(alert)

    def emit_report(self) -> ExecutionReport:
        completed_at = _now_iso()
        total_ms = sum(log.duration_ms for log in self._logs)
        success = all(log.status != "FAIL" for log in self._logs)
        return ExecutionReport(
            run_id=self._run_id,
            started_at=self._started_at,
            completed_at=completed_at,
            total_ms=total_ms,
            success=success,
            logs=list(self._logs),
            alerts=list(self._alerts),
        )

    def emit_json(self) -> str:
        return self.emit_report().to_json()

    def emit_markdown(self) -> str:
        return self.emit_report().to_markdown()

    def save_report(self, output_path: Path) -> None:
        output_path.write_text(self.emit_json(), encoding="utf-8")
