"""knowledge_base/config.py -- shared BigQuery project IDs for the knowledge pipeline.

PERSONAL_PROJECT_ID is your personal/sandbox GCP project (TELUS expires these
on a schedule). Update just that one line in .env when it rotates -- both the
campaign-metadata project below and your ADC quota project (see
update_personal_project.py at the repo root) read from it, so nothing in the
codebase should hardcode the project ID anywhere else.

ADOBE_PROJECT is the stable, shared team project and is not expected to rotate.
"""
from __future__ import annotations

import os

PERSONAL_PROJECT_ID = os.environ.get("PERSONAL_PROJECT_ID", "wb-tian-pr-d0dbe6")

# Override CAMPAIGN_METADATA_PROJECT explicitly only if campaign metadata ever
# needs to live somewhere other than your current personal project.
CAMPAIGN_PROJECT = os.environ.get("CAMPAIGN_METADATA_PROJECT", PERSONAL_PROJECT_ID)
CAMPAIGN_TABLE = os.environ.get(
    "CAMPAIGN_METADATA_TABLE",
    f"{CAMPAIGN_PROJECT}.wb_tian_pr_dataset.campaign_deployments",
)

ADOBE_PROJECT = os.environ.get("BQ_PROJECT_ID", "bi-srv-hsmdet-pr-7b9def")
