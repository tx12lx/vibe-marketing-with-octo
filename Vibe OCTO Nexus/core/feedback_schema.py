"""Feedback loop schema and helpers.

Every interaction that produces a result — a sizing query run, a brief validated,
a human correction — should be logged here. This log accumulates as the knowledge base
that a future 'retrain' command will process to make the brain smarter over time.

Feedback entry lifecycle:
  1. vibe-briefing ask "..."          → auto-logs ask_result entry
  2. downstream tool completes        → tool calls: vibe-briefing feedback --tool sizing_tool --campaign ...
  3. human corrects an output         → human calls: vibe-briefing feedback --tool human --correction ...
  4. (future) vibe-briefing retrain   → reads all feedback_log entries, updates glossary + confidence scores

The feedback_log array lives inside each brief JSON file at the top level.
It is append-only — entries are never deleted, only added.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

FEEDBACK_SCHEMA_VERSION = "1.0"


def create_entry(
    source_tool: str,
    action: str,
    human_query: str = "",
    result: dict | None = None,
    corrections: list | None = None,
    confirmed_correct: bool = False,
    sql_executed: str | None = None,
    row_count: int | None = None,
) -> dict:
    """Create a single feedback log entry.

    Args:
        source_tool: Which tool generated this feedback
                     (sizing_tool | brief_assistant | brief_regeneration | human | ask).
        action: The action that was taken (size_audience | validate_brief | etc.).
        human_query: The original natural language question, if any.
        result: The output produced (status, data, errors).
        corrections: List of correction dicts if a human is correcting an output.
        confirmed_correct: True if the human confirmed the output was correct.
        sql_executed: The SQL that was run, if applicable.
        row_count: Number of rows returned by a sizing query, if applicable.

    Returns:
        Feedback entry dict ready to append to feedback_log.
    """
    return {
        "entry_id": str(uuid.uuid4()),
        "schema_version": FEEDBACK_SCHEMA_VERSION,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "source": {
            "tool": source_tool,
        },
        "trigger": {
            "human_query": human_query,
            "action": action,
        },
        "result": {
            "status": (result or {}).get("status", "unknown"),
            "data": result or {},
            "sql_executed": sql_executed,
            "row_count": row_count,
        },
        "learning_signal": {
            # Populated by humans or downstream tools to guide retraining
            "corrections": corrections or [],
            "confirmed_correct": confirmed_correct,
            "confidence_adjustment": None,  # e.g. +0.1 or -0.2 on a specific term
            "new_terms": [],                # new glossary terms discovered from this interaction
        },
    }


def append_to_brief(brief_json_path: str | Path, entry: dict) -> None:
    """Append a feedback entry to the feedback_log of a saved brief JSON file."""
    path = Path(brief_json_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if "feedback_log" not in data or not isinstance(data["feedback_log"], list):
        data["feedback_log"] = []
    data["feedback_log"].append(entry)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_all_feedback(briefs_dir: str | Path) -> list[dict]:
    """Collect all feedback log entries across all brief JSON files.

    Used by the (future) retrain command to process accumulated learning signals.
    """
    entries = []
    for path in Path(briefs_dir).glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            campaign_id = data.get("campaign", {}).get("id", str(path.stem))
            for entry in data.get("feedback_log", []):
                entries.append({**entry, "_campaign_id": campaign_id, "_source_file": str(path)})
        except Exception:
            continue
    return entries
