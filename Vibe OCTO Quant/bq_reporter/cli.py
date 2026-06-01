"""
CLI entry point — wires argparse arguments into Pipeline(CliSource, CliSink).

To switch to Google Sheets in a future version, replace CliSource/CliSink with
SheetsSource/SheetsSink here and leave everything else untouched.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax

load_dotenv()

from .io.cli_io import CliSink, CliSource
from .models import DataBrief, QueryResult
from .pipeline import Pipeline
from .structured_sql import build_sql_from_criteria

console = Console()


_TABLES_FILE = Path(__file__).parent.parent / "tables.txt"
_CONTEXT_FILE = Path(__file__).parent.parent / "business_context.md"
_SCHEMA_CACHE_FILE = Path(__file__).parent.parent / ".schema_cache.json"


def _load_tables_file() -> list[str]:
    """Read table names from tables.txt — one per line, # lines ignored."""
    if not _TABLES_FILE.exists():
        return []
    tables = []
    for line in _TABLES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            tables.append(line)
    return tables


def _load_business_context() -> str:
    """Read business_context.md verbatim; empty string if missing."""
    if not _CONTEXT_FILE.exists():
        return ""
    return _CONTEXT_FILE.read_text(encoding="utf-8")


def _run_structured(args) -> None:
    """Structured-criteria path: load JSON, build SQL, dry-run, execute, format.

    Bypasses the LLM entirely. Designed for upstream tools (e.g. Monday.com
    campaign-study tool) that produce structured criteria.
    """
    from .bq_client import dry_run_query, format_bytes, run_query

    criteria = json.loads(Path(args.criteria_json).read_text(encoding="utf-8"))
    console.print(f"[dim]>> Loaded criteria from {args.criteria_json}[/dim]")
    sql = build_sql_from_criteria(criteria)

    if not args.no_confirm and not _review_sql(sql):
        return

    console.print("[dim]>> Validating SQL (BigQuery dry-run)…[/dim]")
    ok, err, bytes_proc = dry_run_query(sql, args.project)
    if not ok:
        console.print(f"[red]Dry-run failed:[/red] {err}")
        sys.exit(1)
    console.print(f"[dim]>> Dry-run OK — query will scan {format_bytes(bytes_proc)}[/dim]")

    console.print("[dim]>> Running query on BigQuery…[/dim]")
    rows, columns = run_query(sql, args.project, max_results=args.limit)

    brief = DataBrief(question=f"(structured) {Path(args.criteria_json).name}")
    result = QueryResult(brief=brief, sql=sql, rows=rows, columns=columns)
    CliSink(output_path=Path(args.output)).write_result(result)


def _review_sql(sql: str) -> bool:
    """Show the generated SQL and ask the user to confirm before running."""
    console.print("\n[bold]Generated SQL:[/bold]")
    console.print(Syntax(sql, "sql", theme="monokai", word_wrap=True))
    console.print()
    try:
        answer = input("Run this query? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]Cancelled.[/yellow]")
        return False
    return answer != "n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Translate plain English to BigQuery SQL and generate a shareable report."
    )
    parser.add_argument(
        "--query", "-q",
        help="Plain English question (skips interactive prompt)",
    )
    parser.add_argument(
        "--criteria-json",
        help="Path to a JSON file with structured criteria. When set, the LLM "
             "is bypassed and SQL is built deterministically. Designed for "
             "upstream tools (e.g. Monday.com campaign-study tool) to feed "
             "structured criteria directly. See structured_sql.py for schema.",
    )
    parser.add_argument(
        "--project", "-p",
        default=os.getenv("BQ_PROJECT_ID"),
        help="BigQuery project ID (or set BQ_PROJECT_ID in .env)",
    )
    parser.add_argument(
        "--dataset", "-d",
        default=os.getenv("BQ_DATASET"),
        help="BigQuery dataset(s) — comma-separated for multiple, e.g. sales,marketing (or set BQ_DATASET in .env)",
    )
    parser.add_argument(
        "--tables", "-t",
        default=os.getenv("BQ_TABLES"),
        help="Override tables.txt: comma-separated table/view names to include in schema. "
             "Falls back to tables.txt if not set, or all views if that file is absent.",
    )
    parser.add_argument(
        "--output", "-o",
        default="report_card.json",
        help="Output path for the Google Chat card JSON (default: report_card.json)",
    )
    parser.add_argument(
        "--no-confirm",
        action="store_true",
        help="Run query without asking for confirmation",
    )
    parser.add_argument(
        "--no-schema",
        action="store_true",
        help="Skip fetching BQ schema (faster, SQL may be less accurate)",
    )
    parser.add_argument(
        "--refresh-schema",
        action="store_true",
        help="Force re-fetch of schema even if cache is fresh (cache TTL: 24h)",
    )
    parser.add_argument(
        "--limit", "-l",
        type=int,
        default=100,
        help="Max rows to return (default: 100)",
    )
    args = parser.parse_args()

    if not args.project:
        console.print(
            "[red]Error:[/red] BigQuery project not set. "
            "Use [bold]--project[/bold] or add [bold]BQ_PROJECT_ID[/bold] to your .env file."
        )
        sys.exit(1)

    # Structured-criteria path: bypass LLM entirely
    if args.criteria_json:
        _run_structured(args)
        return

    if not args.query:
        console.print(
            Panel("[bold]BQ Reporter[/bold]\nType your question and press Enter.", expand=False)
        )

    datasets = [d.strip() for d in (args.dataset or "").split(",") if d.strip()]
    # --tables flag / BQ_TABLES env var takes precedence; otherwise load tables.txt
    if args.tables:
        tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    else:
        tables = _load_tables_file()

    pipeline = Pipeline(
        source=CliSource(question=args.query),
        sink=CliSink(output_path=Path(args.output)),
        project=args.project,
        datasets=datasets,
        tables=tables,
        business_context=_load_business_context(),
        limit=args.limit,
        fetch_schema=not args.no_schema,
        schema_cache_path=_SCHEMA_CACHE_FILE,
        refresh_schema=args.refresh_schema,
        review_sql=None if args.no_confirm else _review_sql,
        on_status=lambda msg: console.print(f"[dim]>> {msg}[/dim]"),
    )

    pipeline.run()


if __name__ == "__main__":
    main()
