#!/usr/bin/env python3
"""Re-authorize ADC credentials with Sheets and Drive scopes.

Run once in a terminal:
    python refresh_adc_scopes.py

Opens a browser. Sign in with your TELUS work account (tian.xia@telus.com).
The ADC file is updated in place with a new refresh token that includes
the spreadsheets.readonly and drive.readonly scopes needed by BriefFetcher.

This script uses the same OAuth2 client that is already in your ADC file,
so no new GCP credentials need to be created.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

ADC_PATH = Path(os.environ.get("APPDATA", "")) / "gcloud" / "application_default_credentials.json"
if not ADC_PATH.exists():
    # fallback for non-Windows or custom location
    ADC_PATH = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def main() -> None:
    if not ADC_PATH.exists():
        print(f"ERROR: ADC file not found at {ADC_PATH}")
        print("Run 'gcloud auth application-default login' first to create it.")
        sys.exit(1)

    existing = json.loads(ADC_PATH.read_text(encoding="utf-8"))
    if existing.get("type") != "authorized_user":
        print(f"ERROR: ADC type is '{existing.get('type')}', expected 'authorized_user'.")
        print("This script only works with gcloud user ADC credentials.")
        sys.exit(1)

    client_config = {
        "installed": {
            "client_id": existing["client_id"],
            "client_secret": existing["client_secret"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    print("Opening browser for authentication...")
    print(f"Sign in with your TELUS work account.")
    print()

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)

    new_adc = {
        "account": existing.get("account", ""),
        "client_id": existing["client_id"],
        "client_secret": existing["client_secret"],
        "refresh_token": creds.refresh_token,
        "type": "authorized_user",
        "universe_domain": existing.get("universe_domain", "googleapis.com"),
    }
    # Preserve the quota project (set via `gcloud auth application-default
    # set-quota-project`) -- without this, refreshing scopes silently wipes
    # it and brings back the "no quota project" warning.
    if existing.get("quota_project_id"):
        new_adc["quota_project_id"] = existing["quota_project_id"]
    ADC_PATH.write_text(json.dumps(new_adc, indent=2), encoding="utf-8")

    print(f"\nADC updated: {ADC_PATH}")
    print("Verifying Sheets access via CSV export...")

    import urllib3
    urllib3.disable_warnings()
    import warnings
    import google.auth
    import google.auth.transport.requests as atr
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        creds_verify, _ = google.auth.default()
    req = atr.Request()
    creds_verify.refresh(req)
    session = atr.AuthorizedSession(creds_verify)
    session.verify = False
    # Use a known databrief sheet from the campaign_knowledge table
    test_url = (
        "https://docs.google.com/spreadsheets/d/"
        "1tY6E32g1EBEeYWgWWevXte4Upwukla-OiM-C6uPHeQk"
        "/export?format=csv&gid=1871460492"
    )
    resp = session.get(test_url, timeout=15, verify=False)
    if resp.status_code == 200:
        print("Sheets CSV export: OK — brief data is accessible")
    elif resp.status_code == 401:
        print("WARNING: HTTP 401 — scope may not have been granted. Try running this script again.")
    else:
        print(f"Sheets CSV export: HTTP {resp.status_code} (unexpected)")

    print("\nDone. Restart Vibe OCTO for the new credentials to take effect.")


if __name__ == "__main__":
    main()
