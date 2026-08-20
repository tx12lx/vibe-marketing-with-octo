#!/usr/bin/env python3
"""update_personal_project.py -- point everything at your current personal GCP project.

Run this once whenever your personal project rotates (TELUS expires them
periodically):
  1. Update PERSONAL_PROJECT_ID in .env to the new project ID.
  2. Run:  python update_personal_project.py

This sets the new project as your ADC quota project (fixes the "no quota
project" warning). Campaign-metadata queries pick up the same value
automatically the next time the app starts, since knowledge_base/config.py
reads PERSONAL_PROJECT_ID from the same .env file -- no other change needed.

Requires the gcloud CLI to already be installed and signed in
(gcloud auth application-default login).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

_ROOT = Path(__file__).resolve().parent


def main() -> None:
    env_values = dotenv_values(_ROOT / ".env")
    project_id = (env_values.get("PERSONAL_PROJECT_ID") or "").strip()

    if not project_id:
        print("ERROR: PERSONAL_PROJECT_ID is not set in .env.")
        print("Add a line like:  PERSONAL_PROJECT_ID=your-new-project-id")
        sys.exit(1)

    print(f"Setting your ADC quota project to: {project_id}")
    result = subprocess.run(
        ["gcloud", "auth", "application-default", "set-quota-project", project_id],
    )

    if result.returncode != 0:
        print("\nThat didn't work -- see the error above.")
        print("Common causes: the project doesn't exist yet / isn't approved yet,")
        print("or your account doesn't have the needed permission on it.")
        sys.exit(result.returncode)

    print("\nDone. Restart the web app for this to take effect.")
    print(f"Campaign-metadata queries will also use '{project_id}' automatically")
    print("(same PERSONAL_PROJECT_ID, read by knowledge_base/config.py).")


if __name__ == "__main__":
    main()
