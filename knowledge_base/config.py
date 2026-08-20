"""knowledge_base/config.py -- shared BigQuery project IDs for the knowledge pipeline.

CAMPAIGN_PROJECT points at a personal/sandbox GCP project (TELUS expires these
on a schedule). When it rotates, update CAMPAIGN_METADATA_PROJECT in .env --
nothing in this codebase should hardcode the project ID anywhere else.

ADOBE_PROJECT is the stable, shared team project and is not expected to rotate.
"""
from __future__ import annotations

import os

CAMPAIGN_PROJECT = os.environ.get("CAMPAIGN_METADATA_PROJECT", "wb-tian-pr-d0dbe6")
CAMPAIGN_TABLE = os.environ.get(
    "CAMPAIGN_METADATA_TABLE",
    f"{CAMPAIGN_PROJECT}.wb_tian_pr_dataset.campaign_deployments",
)

ADOBE_PROJECT = os.environ.get("BQ_PROJECT_ID", "bi-srv-hsmdet-pr-7b9def")
