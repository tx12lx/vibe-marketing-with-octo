import json
from datetime import datetime


def _ascii_table(columns: list[str], rows: list[dict]) -> str:
    if not rows:
        return "(no rows returned)"

    widths = {col: len(col) for col in columns}
    for row in rows:
        for col in columns:
            widths[col] = max(widths[col], len(str(row.get(col, ""))))

    sep = "+-" + "-+-".join("-" * widths[col] for col in columns) + "-+"
    header = "| " + " | ".join(col.ljust(widths[col]) for col in columns) + " |"
    lines = [sep, header, sep]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(col, "")).ljust(widths[col]) for col in columns) + " |")
    lines.append(sep)
    return "\n".join(lines)


def format_text_report(question: str, sql: str, rows: list[dict], columns: list[str]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    row_label = f"{len(rows)} row{'s' if len(rows) != 1 else ''}"
    table = _ascii_table(columns, rows)

    return (
        f"{'=' * 60}\n"
        f"BIGQUERY REPORT  |  {now}\n"
        f"{'=' * 60}\n\n"
        f"QUESTION\n{question}\n\n"
        f"SQL\n{sql}\n\n"
        f"RESULTS ({row_label})\n"
        f"{table}\n"
    )


def format_chat_card(question: str, sql: str, rows: list[dict], columns: list[str]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    row_count = len(rows)
    display_rows = rows[:50]  # Cards V2 has payload size limits

    if display_rows and columns:
        results_widget = {
            "table": {
                "columnHeaders": [{"text": col} for col in columns],
                "rows": [
                    {"cells": [{"text": str(row.get(col, ""))} for col in columns]}
                    for row in display_rows
                ],
                "columnCount": len(columns),
            }
        }
    else:
        results_widget = {"textParagraph": {"text": "<i>No results returned.</i>"}}

    extra_widgets = []
    if row_count > 50:
        extra_widgets.append({
            "textParagraph": {
                "text": f"<i>Showing 50 of {row_count} rows. Full results printed to terminal.</i>"
            }
        })

    card = {
        "cardsV2": [
            {
                "cardId": "bq_report",
                "card": {
                    "header": {
                        "title": "BigQuery Report",
                        "subtitle": now,
                    },
                    "sections": [
                        {
                            "header": "Question",
                            "widgets": [{"textParagraph": {"text": question}}],
                        },
                        {
                            "header": "SQL Query",
                            "widgets": [{"textParagraph": {"text": f"<code>{sql}</code>"}}],
                        },
                        {
                            "header": f"Results — {row_count} row{'s' if row_count != 1 else ''}",
                            "widgets": [results_widget] + extra_widgets,
                        },
                    ],
                },
            }
        ]
    }

    return json.dumps(card, indent=2, default=str)
