"""
Google Sheets adapter — STUB
============================

Implement this module when the tool is ready to read briefs from and write
results back to a Google Sheet.

Expected sheet layout (one row per request)
-------------------------------------------
  Col A  Question       — plain-English brief entered by the stakeholder
  Col B  Status         — blank = pending, "Running" = in-flight, "Done" = complete
  Col C  SQL            — generated query (written back by SheetsSink)
  Col D  Row count      — number of result rows (written back by SheetsSink)
  Col E  Results (JSON) — first 200 rows serialised as JSON (written back by SheetsSink)
  Col F  Timestamp      — ISO-8601 completion time (written back by SheetsSink)

Installation
------------
    pip install gspread google-auth-oauthlib

Authentication uses the same Application Default Credentials as BigQuery —
no extra key files are needed as long as the account has Sheets API access.
Enable it at: https://console.cloud.google.com/apis/library/sheets.googleapis.com
"""

from ..models import DataBrief, QueryResult


class SheetsSource:
    """Reads a DataBrief from a row in a Google Sheet.

    Args:
        spreadsheet_id: The Sheets document ID (taken from the URL).
        sheet_name:     Tab name, e.g. ``"Briefs"``.
        row:            1-based row index to process.
                        Pass ``None`` to auto-select the first row where
                        column B (Status) is blank.
    """

    def __init__(
        self,
        spreadsheet_id: str,
        sheet_name: str = "Briefs",
        row: int | None = None,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name
        self.row = row

        # TODO: initialise the gspread client
        #
        #   from google.auth import default
        #   import gspread
        #
        #   creds, _ = default(
        #       scopes=["https://www.googleapis.com/auth/spreadsheets"]
        #   )
        #   self._gc = gspread.authorize(creds)
        #   self._ws = self._gc.open_by_key(spreadsheet_id).worksheet(sheet_name)

        raise NotImplementedError(
            "SheetsSource is not yet implemented. See the TODO comments in "
            "bq_reporter/io/sheets_io.py for the implementation guide."
        )

    def _find_first_pending_row(self) -> int:
        # TODO:
        #   statuses = self._ws.col_values(2)  # Column B
        #   for i, status in enumerate(statuses[1:], start=2):  # skip header
        #       if not status.strip():
        #           return i
        #   raise ValueError("No pending rows found in the sheet.")
        raise NotImplementedError

    def read_brief(self) -> DataBrief:
        # TODO:
        #   row_index = self.row or self._find_first_pending_row()
        #   self._ws.update_cell(row_index, 2, "Running")   # Mark as in-flight
        #   values = self._ws.row_values(row_index)
        #   question = values[0]                             # Column A
        #   return DataBrief(question=question, source_id=str(row_index))
        raise NotImplementedError


class SheetsSink:
    """Writes a QueryResult back to the originating Google Sheet row.

    The row to update is taken from ``result.brief.source_id``, which is set
    by ``SheetsSource.read_brief()``.

    Args:
        spreadsheet_id: Same document ID as ``SheetsSource``.
        sheet_name:     Same tab name as ``SheetsSource``.
    """

    def __init__(self, spreadsheet_id: str, sheet_name: str = "Briefs") -> None:
        self.spreadsheet_id = spreadsheet_id
        self.sheet_name = sheet_name

        # TODO: initialise the gspread client (same pattern as SheetsSource)
        raise NotImplementedError(
            "SheetsSink is not yet implemented. See the TODO comments in "
            "bq_reporter/io/sheets_io.py for the implementation guide."
        )

    def write_result(self, result: QueryResult) -> None:
        # TODO:
        #   import json
        #   from datetime import datetime
        #
        #   row_index = int(result.brief.source_id)
        #   self._ws.update(
        #       f"B{row_index}:F{row_index}",
        #       [[
        #           "Done",                                  # B: Status
        #           result.sql,                              # C: SQL
        #           str(result.row_count),                  # D: Row count
        #           json.dumps(result.rows[:200]),           # E: Results JSON
        #           datetime.now().isoformat(),              # F: Timestamp
        #       ]],
        #   )
        raise NotImplementedError
