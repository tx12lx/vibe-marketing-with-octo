"""CLI adapter — reads a brief from the terminal, writes results to stdout + JSON file."""

import sys
from pathlib import Path

from rich.console import Console

from ..formatter import format_chat_card, format_text_report
from ..models import DataBrief, QueryResult

console = Console()


class CliSource:
    """Reads a DataBrief from a CLI argument or an interactive prompt.

    Args:
        question: Pre-supplied question string (from ``--query``).
                  When *None* the user is prompted interactively.
    """

    def __init__(self, question: str | None = None) -> None:
        self._question = question

    def read_brief(self) -> DataBrief:
        if self._question:
            return DataBrief(question=self._question)

        try:
            question = input("Question: ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]Cancelled.[/yellow]")
            sys.exit(0)

        if not question:
            console.print("[red]No question provided.[/red]")
            sys.exit(1)

        return DataBrief(question=question)


class CliSink:
    """Prints an ASCII report to the terminal and writes a Google Chat card JSON file.

    Args:
        output_path: Destination for the card JSON (default: ``report_card.json``).
    """

    def __init__(self, output_path: Path = Path("report_card.json")) -> None:
        self.output_path = output_path

    def write_result(self, result: QueryResult) -> None:
        text_report = format_text_report(
            result.brief.question, result.sql, result.rows, result.columns
        )
        card_json = format_chat_card(
            result.brief.question, result.sql, result.rows, result.columns
        )

        console.print(text_report)

        self.output_path.write_text(card_json, encoding="utf-8")
        console.print(
            f"\n[bold green]Google Chat card JSON saved to:[/bold green] "
            f"{self.output_path.resolve()}"
        )
        console.print(
            "[dim]To share: POST the card JSON to a Google Chat webhook "
            "or use the Chat API.[/dim]"
        )
