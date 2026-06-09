"""
core/thought_display.py -- User-facing thought display for Vibe OCTO agents.

Renders each pipeline step in plain business language. On UTF-8 terminals uses
Unicode box-drawing characters; falls back to ASCII on legacy Windows consoles.
No SQL, column names, schema identifiers, or stack traces are surfaced.
"""
from __future__ import annotations

import sys
from typing import Optional

_W = 68        # total box line width including borders
_INNER = 64    # usable content chars per row: border + space + 64 + space + border
_LW = 13       # label column width (padded with spaces before ": ")


def _utf8_stdout() -> bool:
    enc = getattr(sys.stdout, "encoding", "") or ""
    return enc.lower().replace("-", "") in ("utf8", "utf16", "utf32")


_U = _utf8_stdout()
_TL   = "┌" if _U else "+"
_TR   = "┐" if _U else "+"
_BL   = "└" if _U else "+"
_BR   = "┘" if _U else "+"
_ML   = "├" if _U else "+"
_MR   = "┤" if _U else "+"
_H    = "─" if _U else "-"
_V    = "│" if _U else "|"
_FILL = "█" if _U else "#"
_OPEN = "░" if _U else "."


def _bar(pct: float, width: int = 10) -> str:
    filled = round(max(0.0, min(1.0, pct)) * width)
    return _FILL * filled + _OPEN * (width - filled)


def _top() -> str:
    return _TL + _H * (_W - 2) + _TR


def _div() -> str:
    return _ML + _H * (_W - 2) + _MR


def _bot() -> str:
    return _BL + _H * (_W - 2) + _BR


def _row(content: str) -> str:
    return _V + " " + content[:_INNER].ljust(_INNER) + " " + _V


def _blank() -> str:
    return _row("")


def _label_rows(label: str, value: str) -> list[str]:
    """Return one or more complete box rows formatted as 'Label   : value'."""
    prefix = f"{label:<{_LW}}: "
    avail = _INNER - len(prefix)
    if avail < 8:
        return [_row((prefix + value)[:_INNER])]

    words = value.split()
    if not words:
        return [_row(prefix.rstrip())]

    out: list[str] = []
    line = ""
    indent = " " * len(prefix)
    for w in words:
        candidate = f"{line} {w}".strip() if line else w
        if len(candidate) <= avail:
            line = candidate
        else:
            pfx = prefix if not out else indent
            out.append(_row(pfx + line))
            line = w[:avail]
    if line:
        pfx = prefix if not out else indent
        out.append(_row(pfx + line))
    return out or [_row(prefix.rstrip())]


class ThoughtDisplay:
    """Stateless terminal display for Vibe OCTO pipeline steps.

    All methods are classmethods that print directly to stdout.
    """

    @staticmethod
    def _box(title: str, body: list[str]) -> None:
        parts = [_top(), _row(title), _div()] + body + [_bot()]
        print("\n" + "\n".join(parts))

    # ------------------------------------------------------------------
    # Pipeline step displays
    # ------------------------------------------------------------------

    @classmethod
    def intent_classified(
        cls, workflow: str, query: str, campaign_hint: Optional[str] = None
    ) -> None:
        short_q = f'"{query[:50]}..."' if len(query) > 50 else f'"{query}"'
        if workflow == "WORKFLOW_B":
            body = [
                *_label_rows("I heard", short_q),
                *_label_rows("Campaign", campaign_hint or "Searching..."),
                *_label_rows("Next step", "Building a full audience targeting blueprint."),
                _row(f"{'Confidence':<{_LW}}: {_bar(0.85)} 85%"),
            ]
            cls._box("VIBE OCTO -- CAMPAIGN REQUEST RECOGNIZED", body)
        else:
            body = [
                *_label_rows("I heard", short_q),
                *_label_rows("Next step", "Running a custom audience query based on your description."),
                _row(f"{'Confidence':<{_LW}}: {_bar(0.65)} 65%"),
            ]
            cls._box("VIBE OCTO -- CUSTOM AUDIENCE LOOKUP", body)

    @classmethod
    def knowledge_lookup(
        cls, campaign_name: str, tier: str, confidence: float
    ) -> None:
        if tier == "GOLD":
            title = "KNOWLEDGE CHECK -- GOLD TIER BLUEPRINT FOUND"
            source = "Verified blueprint from past executions"
        else:
            title = "KNOWLEDGE CHECK -- BUILDING FROM CAMPAIGN BRIEF"
            source = "No verified blueprint. Using campaign brief data."
        body = [
            *_label_rows("Campaign", campaign_name),
            *_label_rows("Source", source),
            _row(f"{'Confidence':<{_LW}}: {_bar(confidence)} {confidence:.0%}"),
        ]
        cls._box(title, body)

    @classmethod
    def discrepancy_check(cls, flag_count: int, flags: list[str]) -> None:
        if not flag_count:
            return
        noun = "item" if flag_count == 1 else "items"
        title = f"PATTERN CHECK -- {flag_count} {noun.upper()} FLAGGED FOR REVIEW"
        body = [
            *_label_rows(
                "What I found",
                f"{flag_count} targeting {noun} differ from established patterns for this campaign.",
            ),
            *_label_rows(
                "What's next",
                "Showing you the differences below. No action needed unless something looks wrong.",
            ),
        ]
        cls._box(title, body)

    @classmethod
    def sql_generation(
        cls, campaign_name: str, filter_count: int, exclusion_count: int
    ) -> None:
        body = [
            *_label_rows("Campaign", campaign_name),
            *_label_rows(
                "Filters",
                f"{filter_count} targeting rules, {exclusion_count} audience exclusions",
            ),
            *_label_rows("Method", "7-step audience funnel with control group"),
            *_label_rows("What's next", "Running the query against the customer database."),
        ]
        cls._box("AUDIENCE QUERY -- BUILDING DATABASE QUERY", body)

    @classmethod
    def results_ready(cls, final_count: int, note: Optional[str] = None) -> None:
        note_text = note or "All filter layers passed without unusual drops."
        note_text = note_text.removeprefix("Optimization Note: ")
        body = [
            *_label_rows("Final count", f"{final_count:,} qualified contacts"),
            *_label_rows("Analysis", note_text),
        ]
        cls._box("AUDIENCE SIZING COMPLETE", body)

    @classmethod
    def brief_generating(cls, campaign_name: str, tier: str) -> None:
        knowledge = (
            "GOLD tier verified intelligence"
            if tier == "GOLD"
            else "available campaign brief context"
        )
        body = [
            *_label_rows("Campaign", campaign_name),
            *_label_rows("Using", knowledge),
            *_label_rows(
                "Generating",
                "Strategic recommendations, targeting logic, execution checklist...",
            ),
        ]
        cls._box("BRIEF -- COMPOSING CAMPAIGN INTELLIGENCE REPORT", body)

    @classmethod
    def hitl_gate(
        cls, campaign_name: str, final_count: int, tier: str
    ) -> None:
        tier_label = (
            "GOLD (verified blueprint)"
            if tier == "GOLD"
            else f"{tier} (built from brief)"
        )
        body = [
            *_label_rows("Campaign", campaign_name),
            *_label_rows("Audience", f"{final_count:,} qualified contacts"),
            *_label_rows("Tier", tier_label),
            _blank(),
            _row("  Approve to save this blueprint for production execution."),
            _row("  Reject to record a correction -- the system will learn."),
        ]
        cls._box("READY FOR YOUR REVIEW", body)

    @classmethod
    def campaign_approved(cls, campaign_name: str) -> None:
        body = [
            *_label_rows("Campaign", campaign_name),
            *_label_rows("Saved to", "App Registry (verified_app_registry.json)"),
            *_label_rows(
                "Next cycle",
                "This approval will be compiled as a permanent GOLD tier blueprint at the next scheduled refresh.",
            ),
        ]
        cls._box("APPROVED -- BLUEPRINT SAVED", body)

    @classmethod
    def campaign_rejected(cls, campaign_name: str) -> None:
        body = [
            *_label_rows("Campaign", campaign_name),
            *_label_rows("Logged", "Correction saved to semantic failure log"),
            *_label_rows("Self-learning", "Targeting patterns and glossary updated"),
            *_label_rows(
                "Next cycle",
                "Your correction will be applied as an absolute override on the next execution of this campaign.",
            ),
        ]
        cls._box("CORRECTION RECORDED -- SESSION CLOSED", body)

    # ------------------------------------------------------------------
    # Error translation
    # ------------------------------------------------------------------

    @classmethod
    def error(cls, message: str) -> None:
        body = [
            *_label_rows("What happened", message),
            *_label_rows("What to do", "Please reach out to the OCTO team for assistance."),
        ]
        cls._box("UNABLE TO COMPLETE REQUEST", body)

    @classmethod
    def translate_nexus_error(cls, error_summary: str) -> None:
        lower = error_summary.lower()
        if "unknown column" in lower or "unrecognized name" in lower:
            msg = (
                "I couldn't find a data field referenced in the targeting rules. "
                "The campaign brief may reference a column that doesn't exist in the current data environment."
            )
        elif "validation" in lower or "field error" in lower:
            msg = (
                "Some targeting instructions could not be translated into a valid format. "
                "The campaign brief may have incomplete or conflicting data."
            )
        elif "timeout" in lower or "deadline" in lower:
            msg = "The database query took too long to complete. Please try again or simplify the request."
        elif "permission" in lower or "access denied" in lower or "403" in lower:
            msg = "I don't have permission to access the required data. Please check that your credentials are configured correctly."
        elif "not found" in lower or "no such" in lower:
            msg = "I could not find the campaign or data requested. The campaign code may be incorrect or the record may not exist yet."
        else:
            msg = (
                "I was unable to complete the audience sizing request. "
                "The details have been logged for the OCTO team to review."
            )
        cls.error(msg)
