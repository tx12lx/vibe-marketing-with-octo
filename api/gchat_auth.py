"""
api/google_auth.py -- Google Chat API service builder.

Credentials are resolved in order:
  1. Explicit service account file path (credentials_path argument)
  2. GOOGLE_CHAT_CREDENTIALS env var pointing to a service account JSON file
  3. Application Default Credentials (ADC) — picks up the gcloud ADC token

For testing with ngrok: ADC from `gcloud auth application-default login` is
sufficient.  For production deployment on a TELUS VM: use a service account
JSON file with the Chat API scope enabled.

Requires: google-auth, google-api-python-client (added to pyproject.toml).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

_CHAT_SCOPE = "https://www.googleapis.com/auth/chat.bot"


def get_chat_service(credentials_path: Optional[Path] = None):
    """Build and return an authenticated Google Chat API v1 service client.

    Raises RuntimeError if the required packages are not installed.
    Raises google.auth.exceptions.DefaultCredentialsError if no credentials
    are found and ADC is not configured.
    """
    try:
        from googleapiclient.discovery import build  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "google-api-python-client is required for Google Chat integration. "
            "Install with: pip install google-api-python-client"
        ) from exc

    try:
        from google.oauth2 import service_account  # type: ignore
        from google.auth import default as _adc_default  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "google-auth is required for Google Chat integration. "
            "Install with: pip install google-auth"
        ) from exc

    # Resolve credentials
    env_path = os.getenv("GOOGLE_CHAT_CREDENTIALS", "")
    resolved = credentials_path or (Path(env_path) if env_path else None)

    if resolved and resolved.exists():
        creds = service_account.Credentials.from_service_account_file(
            str(resolved), scopes=[_CHAT_SCOPE]
        )
    else:
        # Fall back to ADC — works with gcloud auth application-default login
        creds, _ = _adc_default(scopes=[_CHAT_SCOPE])

    return build("chat", "v1", credentials=creds, cache_discovery=False)
