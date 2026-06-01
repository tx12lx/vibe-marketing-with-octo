from dataclasses import dataclass, field


@dataclass
class DataBrief:
    """A plain-English request from any input source.

    ``source_id`` is an opaque token the sink can use to write results
    back to the right place (e.g. a Google Sheet row number, a ticket ID).
    ``context`` carries any extra metadata the source wants to forward —
    date ranges, filters, author name — without the pipeline needing to
    know what it means.
    """

    question: str
    source_id: str | None = None
    context: dict = field(default_factory=dict)


@dataclass
class QueryResult:
    """Everything produced by running a DataBrief through the pipeline."""

    brief: DataBrief
    sql: str
    rows: list[dict]
    columns: list[str]

    @property
    def row_count(self) -> int:
        return len(self.rows)
