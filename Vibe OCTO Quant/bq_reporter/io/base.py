"""
Protocol definitions for the input and output ports.

Any class that implements ``read_brief`` satisfies ``InputSource``.
Any class that implements ``write_result`` satisfies ``OutputSink``.
No inheritance required — new adapters just need the right method signatures.
"""

from typing import Protocol, runtime_checkable

from ..models import DataBrief, QueryResult


@runtime_checkable
class InputSource(Protocol):
    """Yields one DataBrief per call.

    Concrete implementations: ``CliSource``, ``SheetsSource``.
    """

    def read_brief(self) -> DataBrief: ...


@runtime_checkable
class OutputSink(Protocol):
    """Consumes a QueryResult and delivers it to the user.

    Concrete implementations: ``CliSink``, ``SheetsSink``.
    """

    def write_result(self, result: QueryResult) -> None: ...
