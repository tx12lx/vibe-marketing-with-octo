"""
api/slack_formatter.py -- Convert pipeline results to Slack Block Kit messages.

All public functions return a list of Slack block dicts suitable for the `blocks`
parameter of `chat_postMessage`. A plain-text `text` fallback should always be
supplied separately (used by Slack for push notifications).

Block Kit reference: https://api.slack.com/reference/block-kit/blocks

Formatting conventions used here:
  - Waterfall table:  triple-backtick code block for monospace alignment
  - Brief markdown:   posted as mrkdwn (Slack subset of Markdown)
  - Headers:          *bold* section headers (Slack does not render # headings)
  - Dividers:         {"type": "divider"} between major sections
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pydantic_schemas import BriefingOutput, QuantAuditLog

# Maximum characters Slack allows in a single section text block.
_SLACK_TEXT_LIMIT = 3000


# ---------------------------------------------------------------------------
# Low-level block helpers
# ---------------------------------------------------------------------------

def _header(text: str) -> dict:
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": False}}


def _section(text: str, mrkdwn: bool = True) -> dict:
    return {
        "type": "section",
        "text": {"type": "mrkdwn" if mrkdwn else "plain_text", "text": text},
    }


def _fields(*pairs: tuple[str, str]) -> dict:
    """Two-column field grid — each pair is (label, value)."""
    return {
        "type": "section",
        "fields": [
            {"type": "mrkdwn", "text": f"*{label}*\n{value}"}
            for label, value in pairs
        ],
    }


def _divider() -> dict:
    return {"type": "divider"}


def _context(text: str) -> dict:
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": text}],
    }


def _button(text: str, action_id: str, style: Optional[str] = None) -> dict:
    btn: dict = {
        "type": "button",
        "text": {"type": "plain_text", "text": text, "emoji": False},
        "action_id": action_id,
    }
    if style:  # "primary" (green) or "danger" (red)
        btn["style"] = style
    return btn


def _actions(buttons: list[dict]) -> dict:
    return {"type": "actions", "elements": buttons}


# ---------------------------------------------------------------------------
# Shared HITL buttons block (also exposed so slack_app.py can reference action IDs)
# ---------------------------------------------------------------------------

HITL_ACTION_YES = "hitl_yes"
HITL_ACTION_REVIEW = "hitl_review"
HITL_ACTION_NO = "hitl_no"


def _hitl_buttons() -> dict:
    return _actions([
        _button("Looks good", HITL_ACTION_YES, style="primary"),
        _button("Show me how it was built", HITL_ACTION_REVIEW),
        _button("Something looks wrong", HITL_ACTION_NO, style="danger"),
    ])


# ---------------------------------------------------------------------------
# Waterfall text formatter
# ---------------------------------------------------------------------------

def _format_waterfall(log: "QuantAuditLog") -> str:
    """Return a monospace-aligned waterfall string for use in a Slack code block."""
    if not log.waterfall:
        return f"Final audience: {log.final_count:,} contacts"

    name_w = min(max(len(layer.layer_name) for layer in log.waterfall), 38)
    lines: list[str] = []
    for i, layer in enumerate(log.waterfall):
        count_str = f"{layer.audience_count:>12,}"
        if i == 0:
            pct_str = ""
        else:
            prev = log.waterfall[i - 1].audience_count
            pct = (layer.audience_count - prev) / prev * 100 if prev > 0 else 0.0
            pct_str = f"  ({pct:+.0f}%)"
        lines.append(f"{layer.layer_name:<{name_w}}  {count_str}{pct_str}")

    sep = "-" * (name_w + 20)
    lines.append(sep)
    lines.append(f"{'Final Targetable Audience':<{name_w}}  {log.final_count:>12,}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public formatters
# ---------------------------------------------------------------------------

def format_sizing_result(log: "QuantAuditLog") -> list[dict]:
    """Audience sizing result with HITL buttons."""
    campaign = log.request.campaign_name or "Ad-Hoc Request"
    waterfall_text = _format_waterfall(log)

    blocks: list[dict] = [
        _header(f"Audience Sizing -- {campaign}"),
        _divider(),
        _fields(
            ("Final Audience", f"{log.final_count:,} qualified contacts"),
            ("Campaign", campaign),
        ),
    ]

    if log.optimization_note and "clean" not in log.optimization_note.lower():
        note = log.optimization_note.removeprefix("Optimization Note: ")
        blocks.append(_section(f"_Note: {note}_"))

    blocks += [
        _section(f"*Audience Breakdown*\n```\n{waterfall_text}\n```"),
        _divider(),
        _hitl_buttons(),
    ]
    return blocks


def format_brief_result(brief: "BriefingOutput") -> list[dict]:
    """Campaign brief result with HITL buttons."""
    brief_text = (
        brief.brief_markdown[:_SLACK_TEXT_LIMIT]
        if brief.brief_markdown
        else "_Brief content could not be generated. Please try again._"
    )
    blocks: list[dict] = [
        _header(f"Campaign Brief -- {brief.campaign_name}"),
        _divider(),
        _fields(
            ("Campaign", brief.campaign_name),
            ("Tier", brief.tier),
            ("Confidence", f"{brief.confidence_score:.0%}"),
        ),
        _section(brief_text),
    ]

    if brief.data_sources_cited:
        sources_text = "\n".join(f"- {s}" for s in brief.data_sources_cited)
        blocks.append(_context(f"*Data sources:* {sources_text}"))

    blocks += [_divider(), _hitl_buttons()]
    return blocks


def format_combined_result(log: "QuantAuditLog", brief: "BriefingOutput") -> list[dict]:
    """Audience sizing + brief together (campaign_execution intent)."""
    campaign = log.request.campaign_name or brief.campaign_name or "Campaign"
    waterfall_text = _format_waterfall(log)

    blocks: list[dict] = [
        _header(f"Campaign Execution -- {campaign}"),
        _divider(),
        _fields(
            ("Final Audience", f"{log.final_count:,} qualified contacts"),
            ("Brief Confidence", f"{brief.confidence_score:.0%}"),
        ),
        _section(f"*Audience Breakdown*\n```\n{waterfall_text}\n```"),
        _divider(),
        _header("Campaign Brief"),
        _section(brief.brief_markdown[:_SLACK_TEXT_LIMIT]),
        _divider(),
        _hitl_buttons(),
    ]
    return blocks


def format_sql_detail(log: "QuantAuditLog") -> list[dict]:
    """'Show me how it was built' response: the SQL waterfall query."""
    sql_text = (log.sql or "No SQL available")[:3800]
    return [
        _header("How Your Audience Was Built"),
        _section(f"*SQL Waterfall Query*\n```\n{sql_text}\n```"),
    ]


def format_sources_detail(brief: "BriefingOutput") -> list[dict]:
    """'Show me how it was built' response for briefs: data sources used."""
    sources = brief.data_sources_cited or []
    sources_text = "\n".join(f"- {s}" for s in sources) if sources else "Campaign knowledge base"
    return [
        _header("How This Brief Was Built"),
        _fields(("Tier", brief.tier), ("Confidence", f"{brief.confidence_score:.0%}")),
        _section(f"*Data Sources*\n{sources_text}"),
    ]


def format_error(message: str, suggestion: Optional[str] = None) -> list[dict]:
    """Friendly error message."""
    blocks: list[dict] = [
        _header("Could Not Complete Request"),
        _section(message),
    ]
    if suggestion:
        blocks.append(_context(f"_Suggestion: {suggestion}_"))
    return blocks


def format_general_answer(answer: str) -> list[dict]:
    """General knowledge response (plain text answer)."""
    return [
        _header("Vibe OCTO -- Knowledge Response"),
        _section(answer[:_SLACK_TEXT_LIMIT]),
    ]
