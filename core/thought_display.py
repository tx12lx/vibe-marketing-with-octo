"""
core/thought_display.py -- User-facing thought display for Vibe OCTO agents.

Renders each pipeline step in plain, warm business language. On UTF-8 terminals
uses Unicode box-drawing characters; falls back to ASCII on legacy Windows
consoles. No SQL, column names, schema identifiers, or stack traces are shown.
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
_FILL  = "█" if _U else "#"
_OPEN  = "░" if _U else "."
_CHECK = "✅" if _U else "[OK]"
_ARR   = "→"  if _U else "->"


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
    # Inline helpers (no box)
    # ------------------------------------------------------------------

    @staticmethod
    def progress(message: str) -> None:
        """Print a simple inline progress line (no box)."""
        print(f"  {message}")

    @classmethod
    def show_sql(cls, sql: str) -> None:
        """Display the SQL query (only when explicitly requested)."""
        divider = "  " + _H * 62
        print(f"\n  Here's how I built your audience:\n")
        print(divider)
        for line in sql.splitlines():
            print(f"  {line}")
        print(divider)
        print()

    @classmethod
    def show_hitl_summary_box(
        cls,
        intent_type: str,
        campaign_name: Optional[str] = None,
        audience_count: Optional[int] = None,
        confidence: Optional[float] = None,
        data_sources: Optional[list] = None,
    ) -> None:
        """Show warm summary box with numbered menu before the HITL prompt."""
        body: list[str] = [_blank()]
        save_label = f"  1 {_ARR} Looks good! Save this result"
        option2: Optional[str] = None

        if intent_type == "sizing_request":
            body += [
                _row(f"  {_CHECK}  Understood your request"),
                _row(f"  {_CHECK}  Built and ran your audience query"),
            ]
            if audience_count is not None:
                body.append(_row(f"  {_CHECK}  Final audience: {audience_count:,} contacts"))
            option2 = f"  2 {_ARR} Show me how the audience was built"

        elif intent_type == "campaign_execution":
            if campaign_name:
                body.append(_row(f"  {_CHECK}  Found campaign: {campaign_name[:40]}"))
            body.append(_row(f"  {_CHECK}  Applied your verified business rules"))
            if audience_count is not None:
                body.append(_row(f"  {_CHECK}  Built your audience: {audience_count:,} contacts"))
            body.append(_row(f"  {_CHECK}  Generated your campaign brief"))
            save_label = f"  1 {_ARR} Looks good! Save this as a verified blueprint"
            option2 = f"  2 {_ARR} Show me how the audience was built"

        elif intent_type in ("brief_generation", "brief_qa"):
            if campaign_name:
                body.append(_row(f"  {_CHECK}  Generated campaign brief for {campaign_name[:28]}"))
            src_count = len(data_sources) if data_sources else 0
            body.append(_row(f"  {_CHECK}  Used {src_count} data sources"))
            if confidence is not None:
                body.append(_row(f"  {_CHECK}  Confidence: {confidence:.0%}"))
            save_label = f"  1 {_ARR} Looks good! Save this brief"
            option2 = f"  2 {_ARR} Show me the data sources I used"

        else:
            body.append(_row(f"  {_CHECK}  Answered from the knowledge base"))

        body.append(_blank())
        body.append(_row("  What would you like to do next?"))
        body.append(_blank())
        body.append(_row(save_label))
        if option2:
            body.append(_row(option2))
        body.append(_row(f"  3 {_ARR} Something doesn't look right"))

        cls._box("Here's a summary of what I did:", body)

    @classmethod
    def show_brief_sources(cls, briefing_output: Optional[object]) -> None:
        """Display data sources used to build a brief."""
        print("\n  Here's what I used to build this brief:\n")
        if briefing_output is None:
            print(f"  {_CHECK}  Campaign knowledge base")
            print()
            return
        sources = getattr(briefing_output, "data_sources_cited", None) or []
        tier = getattr(briefing_output, "tier", "")
        tier_label = f" ({tier} tier)" if tier else ""
        print(f"  {_CHECK}  Campaign knowledge base{tier_label}")
        for src in sources:
            print(f"  {_CHECK}  {src}")
        if not sources:
            print(f"  {_CHECK}  Available campaign brief context")
        print()

    # ------------------------------------------------------------------
    # Pipeline step displays
    # ------------------------------------------------------------------

    @classmethod
    def intent_classified(
        cls,
        workflow: str,
        query: str,
        campaign_hint: Optional[str] = None,
        confidence: Optional[float] = None,
        knowledge_sources: Optional[list] = None,
    ) -> None:
        """Display what intent was understood, what knowledge was consulted, and confidence.

        Accepts both legacy workflow names (WORKFLOW_A/B) and the unified 5-type
        intent taxonomy (sizing_request, brief_generation, brief_qa,
        campaign_execution, general_question).
        """
        short_q = f'"{query[:50]}..."' if len(query) > 50 else f'"{query}"'
        conf_str = f"  {confidence:.0%}" if confidence is not None else ""
        sources_str = ", ".join(knowledge_sources) if knowledge_sources else ""

        _CAMPAIGN_INTENTS = {"WORKFLOW_B", "campaign_execution", "brief_generation", "brief_qa"}
        _SIZING_INTENTS = {"WORKFLOW_A", "sizing_request"}

        if workflow in _CAMPAIGN_INTENTS:
            body = [
                *_label_rows("I heard", short_q),
                *_label_rows("Campaign", campaign_hint or "Searching..."),
            ]
            if conf_str:
                body += _label_rows("Confidence", conf_str)
            if sources_str:
                body += _label_rows("Knowledge", sources_str)
            cls._box("Got it! I'm looking up the campaign for you...", body)

        elif workflow == "general_question":
            body = [
                *_label_rows("I heard", short_q),
                *_label_rows("Mode", "Answering from knowledge base"),
            ]
            if conf_str:
                body += _label_rows("Confidence", conf_str)
            if sources_str:
                body += _label_rows("Knowledge", sources_str)
            cls._box("I'll answer from our campaign knowledge base.", body)

        else:
            body = [
                *_label_rows("I heard", short_q),
            ]
            if conf_str:
                body += _label_rows("Confidence", conf_str)
            if sources_str:
                body += _label_rows("Knowledge", sources_str)
            cls._box("Understood. Let me find the best audience for your request...", body)

    @classmethod
    def knowledge_lookup(
        cls, campaign_name: str, tier: str, confidence: float
    ) -> None:
        if tier == "GOLD":
            title = "Great news! I found a proven blueprint for this campaign."
            body = [
                *_label_rows("Campaign", campaign_name),
                *_label_rows("Blueprint", "Verified from past executions"),
                *_label_rows("Confidence", f"{_bar(confidence)} {confidence:.0%}"),
            ]
        else:
            title = "Building your campaign from the data brief."
            body = [
                *_label_rows("Campaign", campaign_name),
                *_label_rows(
                    "Status",
                    f"No verified blueprint found. Confidence: {_bar(confidence)} {confidence:.0%}",
                ),
                *_label_rows("Note", "I'll do my best with the available information."),
            ]
        cls._box(title, body)

    @classmethod
    def discrepancy_check(cls, flag_count: int, flags: list[str]) -> None:
        if not flag_count:
            return
        noun = "thing" if flag_count == 1 else "things"
        title = f"I noticed {flag_count} {noun} to flag before we proceed."
        body = [
            *_label_rows(
                "What I found",
                f"{flag_count} targeting {noun} differ from established patterns for this campaign.",
            ),
            *_label_rows(
                "What's next",
                "Please review the details below. No action needed unless something looks wrong.",
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
                "Applying",
                f"{filter_count} targeting rules and {exclusion_count} audience exclusions",
            ),
            *_label_rows("Method", "7-step audience funnel with control group"),
        ]
        cls._box("Building your audience now. This may take a moment...", body)

    @classmethod
    def results_ready(
        cls,
        final_count: int,
        waterfall: list,  # list[WaterfallLayer] — not imported to avoid circular dep
        note: Optional[str] = None,
    ) -> None:
        """Display a warm results box with the full waterfall breakdown."""
        body: list[str] = [
            _row("  Here's how we filtered down your audience:"),
        ]

        if waterfall:
            name_w = min(max(len(layer.layer_name) for layer in waterfall), 30)
            divider_row = _row("  " + _H * (name_w + 21))
            body.append(divider_row)

            for i, layer in enumerate(waterfall):
                count_str = f"{layer.audience_count:>12,}"
                if i == 0:
                    pct_str = ""
                else:
                    prev = waterfall[i - 1].audience_count
                    if prev > 0:
                        pct = (layer.audience_count - prev) / prev * 100
                        pct_str = f"   ({pct:+.0f}%)"
                    else:
                        pct_str = ""
                line = f"  {layer.layer_name:<{name_w}} {count_str}{pct_str}"
                body.append(_row(line))

            body.append(divider_row)
            final_line = f"  {'Final Audience':<{name_w}} {final_count:>12,}"
            body.append(_row(final_line))
        else:
            body.append(_row(f"  Final Audience: {final_count:,} qualified contacts"))

        if note and "clean" not in note.lower():
            note_text = note.removeprefix("Optimization Note: ")
            body.append(_blank())
            body += _label_rows("Note", note_text)

        cls._box("Your Audience is Ready!", body)

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
                "Targeting logic, audience insights, and strategic recommendations...",
            ),
        ]
        cls._box("I'm putting together your campaign brief now...", body)

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
            _row("  Please take a moment to review everything before we save this."),
        ]
        cls._box("All done! Here's everything I prepared for you.", body)

    @classmethod
    def campaign_approved(cls, campaign_name: str) -> None:
        body = [
            *_label_rows("Campaign", campaign_name),
            *_label_rows("Saved to", "App Registry (verified_app_registry.json)"),
            *_label_rows(
                "Next time",
                "I'll use this as a reference blueprint for even better results.",
            ),
        ]
        cls._box("Wonderful! I've saved this as a verified blueprint.", body)

    @classmethod
    def campaign_rejected(cls, campaign_name: str) -> None:
        body = [
            *_label_rows("Campaign", campaign_name),
            *_label_rows("Logged", "Your correction has been saved"),
            *_label_rows(
                "Self-learning",
                "Targeting patterns and glossary updated based on your feedback",
            ),
            *_label_rows(
                "Next time",
                "Your correction will be applied as an absolute override on the next run.",
            ),
        ]
        cls._box("Thank you for the feedback! I've recorded your correction.", body)

    # ------------------------------------------------------------------
    # Error translation
    # ------------------------------------------------------------------

    @classmethod
    def error(cls, message: str, action: Optional[str] = None) -> None:
        body = [
            *_label_rows("What happened", message),
            *_label_rows(
                "What to do",
                action or "Please reach out to the OCTO team for assistance.",
            ),
        ]
        cls._box("I ran into an issue and wasn't able to complete this.", body)

    @classmethod
    def feedback_acknowledging(cls, campaign_name: str, raw_correction: str) -> None:
        short = f'"{raw_correction[:60]}..."' if len(raw_correction) > 60 else f'"{raw_correction}"'
        body = [
            *_label_rows("Campaign", campaign_name),
            *_label_rows("Correction", short),
            _blank(),
            _row("  Let me make sure I understand this correctly before saving."),
        ]
        cls._box(
            "Thank you for the feedback. Let me interpret this for you.",
            body,
        )

    @classmethod
    def show_rules_being_applied(cls, summary: str) -> None:
        """Display verified business rules that are being applied to this execution."""
        lines = summary.splitlines()
        body: list[str] = []
        for line in lines:
            body.append(_row(line))
        if not body:
            return
        cls._box("Applying your verified business rules to this campaign.", body)

    @classmethod
    def translate_nexus_error(cls, error_summary: str) -> None:
        lower = error_summary.lower()
        if "unknown column" in lower or "unrecognized name" in lower:
            msg = (
                "I couldn't find a data field referenced in the targeting rules. "
                "The campaign brief may reference a column that no longer exists."
            )
            action = (
                "Check if the column name in the brief matches the current data environment, "
                "or contact the OCTO team."
            )
        elif "validation" in lower or "field error" in lower:
            msg = "Some targeting instructions couldn't be translated into a valid format."
            action = "Check the campaign brief for incomplete or conflicting information."
        elif "timeout" in lower or "deadline" in lower:
            msg = "The audience query took too long to complete."
            action = "Try again in a moment, or simplify the targeting criteria."
        elif "permission" in lower or "access denied" in lower or "403" in lower:
            msg = "I don't have permission to access the required data."
            action = "Check that your credentials are configured correctly, or contact your IT administrator."
        elif "not found" in lower or "no such" in lower:
            msg = "I couldn't find the campaign or data requested."
            action = "Check that the campaign code is correct, or run a full knowledge base refresh."
        else:
            msg = "I was unable to complete the audience sizing request."
            action = "Please reach out to the OCTO team for assistance."
        cls.error(msg, action)
