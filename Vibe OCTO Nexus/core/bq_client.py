from __future__ import annotations

import re
from typing import Optional

from google.cloud import bigquery
from google.oauth2 import service_account


_SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_identifier(value: str, label: str) -> None:
    if not _SAFE_IDENTIFIER.match(value):
        raise ValueError(
            f"Invalid {label} '{value}': only alphanumeric and underscore characters allowed."
        )


class BQClient:
    def __init__(self, project: str, credentials_path: Optional[str] = None):
        if credentials_path:
            creds = service_account.Credentials.from_service_account_file(
                credentials_path,
                scopes=["https://www.googleapis.com/auth/bigquery.readonly"],
            )
            self.client = bigquery.Client(project=project, credentials=creds)
        else:
            self.client = bigquery.Client(project=project)
        self.project = project

    def get_historical_briefs(
        self,
        table: str,
        camp_id: str,
        limit: int,
        sort_field: str = "list_pull_date",
        sort_order: str = "ASC",
        offset: int = 0,
        random: bool = False,
    ) -> list[dict]:
        if not random:
            _validate_identifier(sort_field, "sort_field")
            sort_order = sort_order.upper()
            if sort_order not in ("ASC", "DESC"):
                raise ValueError("sort_order must be ASC or DESC")
            order_clause = f"ORDER BY {sort_field} {sort_order}"
        else:
            order_clause = "ORDER BY RAND()"

        query = f"""
            SELECT
                campaign,
                camp_id,
                sub_camp_id,
                target_base,
                medium,
                cadence,
                campaign_purpose,
                primary_products,
                databrief_link
            FROM `{table}`
            WHERE current_ind = 1
              AND closed_ind = 0
              AND UPPER(target_base) <> 'EPP'
              AND UPPER(camp_id) = UPPER(@camp_id)
              AND DATE(list_pull_date) <= DATE_SUB(CURRENT_DATE(), INTERVAL 5 DAY)
              AND databrief_link IS NOT NULL
              AND databrief_link != ''
            {order_clause}
            LIMIT {int(limit)}
            OFFSET {int(offset)}
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("camp_id", "STRING", camp_id),
            ]
        )
        results = self.client.query(query, job_config=job_config).result()
        return [dict(row) for row in results]

    def get_briefs_by_campaign_name(self, table: str, campaign_name: str) -> list[dict]:
        """Fetch all active brief rows for an exact campaign name match.

        Uses the user-provided query pattern:
          WHERE UPPER(campaign) = UPPER(@campaign_name)
        Returns all matching rows regardless of age or limit — campaign name
        filtering is used for focused study of a specific campaign.
        """
        query = f"""
            SELECT
                campaign,
                camp_id,
                sub_camp_id,
                medium,
                campaign_purpose,
                databrief_link
            FROM `{table}`
            WHERE current_ind = 1
              AND closed_ind = 0
              AND UPPER(campaign) = UPPER(@campaign_name)
              AND databrief_link IS NOT NULL
              AND databrief_link != ''
            ORDER BY list_pull_date DESC
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("campaign_name", "STRING", campaign_name),
            ]
        )
        results = self.client.query(query, job_config=job_config).result()
        return [dict(row) for row in results]

    def get_table_schema(self, table: str) -> list[dict]:
        """Return column definitions for a BigQuery table via INFORMATION_SCHEMA.

        Args:
            table: Fully-qualified table as project.dataset.table

        Returns:
            List of dicts with column_name, data_type, description (if set).
        """
        parts = table.split(".")
        if len(parts) != 3:
            raise ValueError(f"table must be project.dataset.table, got: {table}")
        project, dataset, table_name = parts
        query = f"""
            SELECT
                column_name,
                data_type,
                is_nullable,
                COALESCE(description, '') AS description
            FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS`
            WHERE table_name = @table_name
            ORDER BY ordinal_position
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("table_name", "STRING", table_name),
            ]
        )
        results = self.client.query(query, job_config=job_config).result()
        return [dict(row) for row in results]

    def get_portfolio_count(self, table: str, camp_id: str) -> int:
        query = """
            SELECT COUNT(*) AS cnt
            FROM `{table}`
            WHERE current_ind = 1
              AND closed_ind = 0
              AND UPPER(target_base) <> 'EPP'
              AND UPPER(camp_id) = UPPER(@camp_id)
              AND DATE(list_pull_date) <= DATE_SUB(CURRENT_DATE(), INTERVAL 5 DAY)
              AND databrief_link IS NOT NULL
              AND databrief_link != ''
        """.format(table=table)
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("camp_id", "STRING", camp_id),
            ]
        )
        results = self.client.query(query, job_config=job_config).result()
        for row in results:
            return row["cnt"]
        return 0
