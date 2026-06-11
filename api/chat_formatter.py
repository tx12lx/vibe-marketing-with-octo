"""
api/chat_formatter.py -- Convert pipeline results to Google Chat Cards v2 JSON.

All public functions return a dict that FastAPI can serialize directly as a
JSON response body to a Google Chat webhook.

Card structure used throughout:
  cardsV2[].card.header   -- title + subtitle
  cardsV2[].card.sections -- one or more sections with widgets
    textParagraph         -- plain or monospace text blocks
    decoratedText         -- label + value rows
    buttonList            -- HITL action buttons

Reference: https://developers.google.com/chat/api/guides/message-formats/cards
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pydantic_schemas import BriefingOutput, QuantAuditLog

# ---------------------------------------------------------------------------
# Low-level card builder helpers
# ---------------------------------------------------------------------------

def _card(card_id: str, title: str, subtitle: str, sections: list[dict]) -> dict:
    return {
        "cardsV2": [{
            "cardId": card_id,
            "card": {
                "header": {"title": title, "subtitle": subtitle},
                "sections": sections,
            },
        }]
    }


def _section(header: str, widgets: list[dict], collapsible: bool = False) -> dict:
    s: dict = {"widgets": widgets}
    if header:
        s["header"] = header
    if collapsible:
        s["collapsible"] = True
    return s


def _text(text: str) -> dict:
    return {"textParagraph": {"text": text}}


def _decorated(top_label: str, text: str) -> dict:
    return {"decoratedText": {"topLabel": top_label, "text": text}}


def _button(label: str, function_name: str, parameters: Optional[list[dict]] = None) -> dict:
    action: dict = {"function": function_name}
    if parameters:
        action["parameters"] = parameters
    return {"text": label, "onClick": {"action": action}}


def _hitl_buttons() -> dict:
    return {
        "buttonList": {
            "buttons": [
                _button("Looks good!", "hitl_yes"),
                _button("Show how it was built", "hitl_review"),
                _button("Something's wrong", "hitl_no"),
            ]
        }
    }


# ---------------------------------------------------------------------------
# Waterfall text formatter
# ---------------------------------------------------------------------------

def _format_waterfall(log: "QuantAuditLog") -> str:
    """Return a monospace-friendly waterfall table as a plain text string."""
    if not log.waterfall:
        return f"Final audience: {log.final_count:,} contacts"

    name_w = min(max(len(layer.layer_name) for layer in log.waterfall), 32)
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
    lines.append(f"{'Final Audience':<{name_w}}  {log.final_count:>12,}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public formatters
# ---------------------------------------------------------------------------

def format_audit_log_card(log: "QuantAuditLog", include_hitl: bool = True) -> dict:
    """Format a QuantAuditLog as a Google Chat Card v2 with optional HITL buttons."""
    campaign = log.request.campaign_name or "Ad-Hoc Request"
    waterfall_text = _format_waterfall(log)

    info_widgets = [
        _decorated("Campaign", campaign),
        _decorated("Final Audience", f"{log.final_count:,} qualified contacts"),
    ]
    if log.optimization_note and "clean" not in log.optimization_note.lower():
        note = log.optimization_note.removeprefix("Optimization Note: ")
        info_widgets.append(_decorated("Note", note))

    sections = [
        _section("", info_widgets),
        _section(
            "Audience Breakdown",
            [_text(f"<font face='Courier New'>{waterfall_text}</font>")],
        ),
    ]

    if include_hitl:
        sections.append(_section("", [_hitl_buttons()]))

    return _card(
        "audience-result",
        "Vibe OCTO — Audience Sizing Complete",
        f"Campaign: {campaign}",
        sections,
    )


def format_brief_card(brief: "BriefingOutput", include_hitl: bool = True) -> dict:
    """Format a BriefingOutput as a Google Chat Card v2."""
    sections: list[dict] = [
        _section("", [
            _decorated("Campaign", brief.campaign_name),
            _decorated("Tier", brief.tier),
            _decorated("Confidence", f"{brief.confidence_score:.0%}"),
        ]),
        _section("Campaign Brief", [_text(brief.brief_markdown[:3000])]),
    ]

    if brief.data_sources_cited:
        sources_text = "\n".join(f"- {s}" for s in brief.data_sources_cited)
        sections.append(_section("Data Sources", [_text(sources_text)], collapsible=True))

    if include_hitl:
        sections.append(_section("", [_hitl_buttons()]))

    return _card(
        "brief-result",
        "Vibe OCTO — Campaign Brief",
        f"Campaign: {brief.campaign_name}",
        sections,
    )


def format_combined_card(
    log: "QuantAuditLog",
    brief: "BriefingOutput",
    include_hitl: bool = True,
) -> dict:
    """Format both audience sizing and brief as a single card (campaign_execution intent)."""
    campaign = log.request.campaign_name or brief.campaign_name or "Campaign"
    waterfall_text = _format_waterfall(log)

    sections: list[dict] = [
        _section("", [
            _decorated("Campaign", campaign),
            _decorated("Final Audience", f"{log.final_count:,} qualified contacts"),
            _decorated("Brief Confidence", f"{brief.confidence_score:.0%}"),
        ]),
        _section(
            "Audience Breakdown",
            [_text(f"<font face='Courier New'>{waterfall_text}</font>")],
        ),
        _section("Campaign Brief", [_text(brief.brief_markdown[:2500])], collapsible=True),
    ]

    if include_hitl:
        sections.append(_section("", [_hitl_buttons()]))

    return _card(
        "execution-result",
        "Vibe OCTO — Campaign Execution",
        f"Campaign: {campaign}",
        sections,
    )


def format_sql_card(sql: str) -> dict:
    """Format the raw SQL for the 'Show how it was built' response."""
    return _card(
        "sql-detail",
        "How Your Audience Was Built",
        "SQL waterfall query",
        [_section("", [_text(f"<font face='Courier New'>{sql[:4000]}</font>")])],
    )


def format_brief_sources_card(brief: "BriefingOutput") -> dict:
    """Format data sources for the 'Show how it was built' response for briefs."""
    sources = brief.data_sources_cited or []
    sources_text = "\n".join(f"- {s}" for s in sources) if sources else "Campaign knowledge base"
    return _card(
        "sources-detail",
        "How This Brief Was Built",
        f"Tier: {brief.tier}",
        [_section("Data Sources", [_text(sources_text)])],
    )


def format_confirmation_card(message: str) -> dict:
    """Format a simple confirmation message (e.g. after HITL yes)."""
    return _card(
        "confirmation",
        "Vibe OCTO",
        "Result saved",
        [_section("", [_text(message)])],
    )


def format_error_card(message: str, suggestion: Optional[str] = None) -> dict:
    """Format an error message as a card."""
    widgets = [_text(message)]
    if suggestion:
        widgets.append(_decorated("Suggestion", suggestion))
    return _card(
        "error",
        "Vibe OCTO — Could Not Complete Request",
        "",
        [_section("", widgets)],
    )


def format_general_answer_card(answer: str) -> dict:
    """Format a general_question answer as a card."""
    return _card(
        "general-answer",
        "Vibe OCTO — Knowledge Response",
        "",
        [_section("", [_text(answer[:3500])])],
    )


def format_greeting() -> dict:
    """Welcome card sent when the bot is added to a space."""
    return _card(
        "greeting",
        "Welcome to Vibe OCTO!",
        "AI-powered campaign audience consultant",
        [_section("", [_text(
            "Hi there! I'm Vibe OCTO, your AI marketing consultant.\n\n"
            "I can help you with:\n"
            "- Audience sizing for any campaign\n"
            "- Campaign intelligence briefs\n"
            "- General questions about campaigns and targeting\n\n"
            "Just ask me a question in plain English to get started."
        )])],
    )
