"""
Core pipeline — the only place that knows the full processing sequence.

Nothing in here is aware of terminals, spreadsheets, or any other I/O detail.
Swap the source/sink at call-site; this file never changes.
"""

from pathlib import Path
from typing import Callable

from google.api_core.exceptions import BadRequest, NotFound

from .bq_client import dry_run_query, format_bytes, get_schema, run_query
from .io.base import InputSource, OutputSink
from .models import QueryResult
from .sql_gen import generate_sql

# Optional hook: receives the generated SQL, returns True to proceed or False
# to abort. Pass None to skip confirmation entirely (e.g. in automated flows).
SqlReviewFn = Callable[[str], bool]


class Pipeline:
    """Orchestrates: read brief → fetch schema → generate SQL → run query → write result.

    Args:
        source:       Where briefs come from (CLI, Sheets, …).
        sink:         Where results go (CLI, Sheets, …).
        project:      BigQuery project ID.
        datasets:     One or more BigQuery dataset names to include in schema
                      fetching and SQL generation.
        limit:        Maximum rows returned from BigQuery.
        fetch_schema: Whether to pull live table schemas to improve SQL accuracy.
        review_sql:   Called with the generated SQL before execution.
                      Return ``True`` to run, ``False`` to abort.
                      Pass ``None`` to auto-approve (useful for automated sources).
        on_status:    Optional callback for progress messages, e.g. ``print`` or
                      a Rich console method.  Receives a plain string.
    """

    def __init__(
        self,
        source: InputSource,
        sink: OutputSink,
        project: str,
        datasets: list[str] | None = None,
        tables: list[str] | None = None,
        business_context: str = "",
        limit: int = 100,
        fetch_schema: bool = True,
        schema_cache_path: Path | None = None,
        refresh_schema: bool = False,
        review_sql: SqlReviewFn | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self.source = source
        self.sink = sink
        self.project = project
        self.datasets = datasets or []
        self.tables = tables or []
        self.business_context = business_context
        self.limit = limit
        self.fetch_schema = fetch_schema
        self.schema_cache_path = schema_cache_path
        self.refresh_schema = refresh_schema
        self.review_sql = review_sql
        self._status = on_status or (lambda _: None)

    def _validate_and_maybe_retry(
        self, sql: str, question: str, schema_context: str
    ) -> str:
        """Dry-run the SQL. If BQ rejects it, ask the LLM to fix it once."""
        self._status("Validating SQL (BigQuery dry-run)…")
        ok, err, bytes_proc = dry_run_query(sql, self.project)
        if ok:
            self._status(f"Dry-run OK — query will scan {format_bytes(bytes_proc)}")
            return sql

        self._status(f"Dry-run failed: {err.splitlines()[0] if err else 'unknown error'}")
        self._status("Retrying SQL generation with the error fed back to Claude…")
        retry_question = (
            f"Your previous SQL was rejected by BigQuery.\n\n"
            f"Previous SQL:\n{sql}\n\n"
            f"BigQuery error: {err}\n\n"
            f"Original question: {question}\n\n"
            f"Fix the SQL and return only the corrected SQL."
        )
        sql_retry = generate_sql(
            retry_question,
            schema_context,
            self.project,
            self.datasets,
            table_names=self.tables or None,
            business_context=self.business_context,
        )
        ok2, err2, bytes_proc2 = dry_run_query(sql_retry, self.project)
        if ok2:
            self._status(f"Retry succeeded — query will scan {format_bytes(bytes_proc2)}")
            return sql_retry

        # Both attempts failed — surface both for debugging
        raise RuntimeError(
            f"SQL validation failed after retry.\n\n"
            f"First attempt:\n{sql}\nError: {err}\n\n"
            f"Retry attempt:\n{sql_retry}\nError: {err2}"
        )

    def run(self) -> QueryResult:
        brief = self.source.read_brief()

        schema_context = ""
        if self.fetch_schema and self.datasets:
            table_names = self.tables or None
            label = ", ".join(self.datasets)
            if table_names:
                label += f" ({len(table_names)} tables)"
            cache_note = ""
            if self.schema_cache_path is not None and not self.refresh_schema and self.schema_cache_path.exists():
                cache_note = " (cached)"
            self._status(f"Fetching schema from {self.project}: {label}{cache_note}…")
            schema_context = get_schema(
                self.project, self.datasets,
                table_names=table_names,
                cache_path=self.schema_cache_path,
                refresh=self.refresh_schema,
            )

        self._status("Generating SQL with Claude…")
        sql = generate_sql(
            brief.question,
            schema_context,
            self.project,
            self.datasets,
            table_names=self.tables or None,
            business_context=self.business_context,
        )

        # Dry-run validation — catches bad SQL in ~200ms before burning real time.
        # If validation fails, ask the LLM to fix it (one retry).
        sql = self._validate_and_maybe_retry(
            sql, brief.question, schema_context,
        )

        if self.review_sql is not None and not self.review_sql(sql):
            raise SystemExit(0)

        self._status("Running query on BigQuery…")
        try:
            rows, columns = run_query(sql, self.project, max_results=self.limit)
        except (BadRequest, NotFound) as exc:
            raise RuntimeError(
                f"BigQuery rejected the generated SQL:\n\n{sql}\n\nError: {exc}"
            ) from exc

        result = QueryResult(brief=brief, sql=sql, rows=rows, columns=columns)
        self.sink.write_result(result)
        return result
