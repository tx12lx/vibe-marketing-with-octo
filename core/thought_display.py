"""
core/thought_display.py -- User-facing thought display for Vibe OCTO agents.

Renders each pipeline step in plain, warm business language. On UTF-8 terminals
uses Unicode box-drawing characters; falls back to ASCII on legacy Windows
consoles. No SQL, column names, schema identifiers, or stack traces are shown.
"""
from __future__ import annotations

import sys
import threading
from typing import Callable, Optional

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
_CHECK = "✅" if _U else "[OK]"


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
    """Terminal display for Vibe OCTO pipeline steps, plus an optional live stream.

    All methods are classmethods that print directly to stdout (unchanged terminal
    behavior). Additionally, each step emits one short, plain-English line to a
    per-thread "stream sink" if one is set (see set_stream_sink()) -- this is what
    lets api/web_app.py's /query endpoint show the tool's reasoning in the browser
    in real time (see Phase 3 of the plan) without touching how the terminal /
    CLI experience already works. Thread-local, matching agents/nexus_agent.py and
    agents/quant_agent.py's session-context fix: one request's pipeline runs on one
    worker thread for its duration, so each request's sink is independent of every
    other concurrent request's.
    """

    _local = threading.local()

    @classmethod
    def set_stream_sink(cls, sink: Callable[[str], None]) -> None:
        """Route this thread's plain-English progress lines to `sink` as well as
        printing them as usual. Call clear_stream_sink() when the request is done."""
        cls._local.sink = sink

    @classmethod
    def clear_stream_sink(cls) -> None:
        cls._local.sink = None

    @classmethod
    def _emit(cls, message: str) -> None:
        """Send one short, plain-English line to this thread's stream sink, if any.
        Never raises -- a broken or slow sink must never break the underlying pipeline."""
        sink = getattr(cls._local, "sink", None)
        if sink is None:
            return
        try:
            sink(message)
        except Exception:
            pass

    @staticmethod
    def _box(title: str, body: list[str]) -> None:
        parts = [_top(), _row(title), _div()] + body + [_bot()]
        print("\n" + "\n".join(parts))

    @classmethod
    def progress(cls, message: str) -> None:
        """Print a simple inline progress line (no box); also stream it verbatim --
        every existing progress() call is already a short, plain-English sentence."""
        print(f"  {message}")
        cls._emit(message)

    # ------------------------------------------------------------------
    # Pipeline step displays
    # ------------------------------------------------------------------

    @classmethod
    def intent_classified(
        cls,
        intent_type: str,
        query: str,
        campaign_hint: Optional[str] = None,
        confidence: Optional[float] = None,
        knowledge_sources: Optional[list] = None,
    ) -> None:
        """Display what intent was understood, what knowledge was consulted, and confidence."""
        short_q = f'"{query[:50]}..."' if len(query) > 50 else f'"{query}"'
        conf_str = f"  {confidence:.0%}" if confidence is not None else ""
        sources_str = ", ".join(knowledge_sources) if knowledge_sources else ""

        if intent_type == "general_question":
            body = [
                *_label_rows("I heard", short_q),
                *_label_rows("Mode", "Answering from knowledge base"),
            ]
            if conf_str:
                body += _label_rows("Confidence", conf_str)
            if sources_str:
                body += _label_rows("Knowledge", sources_str)
            cls._box("I'll answer from our campaign knowledge base.", body)
            cls._emit("Got it -- let me check what we already know about that.")
        else:
            body = [*_label_rows("I heard", short_q)]
            if campaign_hint:
                body += _label_rows("Campaign", campaign_hint)
            if conf_str:
                body += _label_rows("Confidence", conf_str)
            if sources_str:
                body += _label_rows("Knowledge", sources_str)
            cls._box("Understood. Let me find the best audience for your request...", body)
            cls._emit("Got it -- that's an audience sizing question. Let me get to work on it.")

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
    def execution_plan(
        cls,
        target_population: str,
        table_label: str,
        filters: Optional[list[str]] = None,
        skipped_steps: Optional[list[str]] = None,
        applied_rules: Optional[list[str]] = None,
    ) -> None:
        """Show the table selection and key filters before the waterfall query executes."""
        body = [
            *_label_rows("Query target", target_population[:80]),
            *_label_rows("Data source", table_label),
        ]
        if filters:
            parts = [f[:50] + ("..." if len(f) > 50 else "") for f in filters[:4]]
            body += _label_rows("Key filters", " | ".join(parts))
        if applied_rules:
            body.append(_blank())
            for rule in applied_rules[:3]:
                body.append(_row(f"  {_CHECK}  {rule[:58]}"))
        if skipped_steps:
            body.append(_blank())
            body.append(_row("  Skipping steps not applicable to this table:"))
            for step in skipped_steps[:3]:
                body.append(_row(f"    {step[:60]}"))
        body += [_blank(), _row("  Running the waterfall now...")]
        cls._box("Here is my plan before I run the query:", body)
        cls._emit(f"Found the right data ({table_label}) and worked out the filters -- running the numbers now...")

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
        cls._emit("Hit a snag -- let me explain what happened.")

    @classmethod
    def translate_nexus_error(cls, error_summary: str) -> None:
        lower = error_summary.lower()
        if "unknown column" in lower or "unrecognized name" in lower:
            msg = (
                "I couldn't find a data field referenced in the targeting rules. "
                "The request may reference a column that no longer exists."
            )
            action = (
                "Check if the column name matches the current data environment, "
                "or contact the OCTO team."
            )
        elif "validation" in lower or "field error" in lower:
            msg = "Some targeting instructions couldn't be translated into a valid format."
            action = "Check the request for incomplete or conflicting information."
        elif "timeout" in lower or "deadline" in lower:
            msg = "The audience query took too long to complete."
            action = "Try again in a moment, or simplify the targeting criteria."
        elif "permission" in lower or "access denied" in lower or "403" in lower:
            msg = "I don't have permission to access the required data."
            action = "Check that your credentials are configured correctly, or contact your IT administrator."
        elif "not found" in lower or "no such" in lower:
            msg = "I couldn't find the data requested."
            action = "Check that the request is correct, or run a full knowledge base refresh."
        else:
            msg = "I was unable to complete the audience sizing request."
            action = "Please reach out to the OCTO team for assistance."
        cls.error(msg, action)
