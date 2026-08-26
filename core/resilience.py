"""
core/resilience.py -- Centralized retry logic and startup health checks.

Retry policy for Fuel iX API calls: 1s -> 2s -> 4s, max 3 retries.
  Retries on HTTP 429, 500, 502, 503, 504 and network errors.

Retry policy for BigQuery calls: same backoff schedule.
  Retries on ServiceUnavailable and ResourceExhausted (quota) errors.

Phase 5B: run_startup_health_check() gates the session on Fuel iX reachability,
  BigQuery ADC validity, and knowledge index presence.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

import requests

_log = logging.getLogger(__name__)

# HTTP status codes that warrant a retry on Fuel iX calls
_FUELIX_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

# BQ exception class names that warrant a retry
_BQ_RETRYABLE_NAMES = frozenset({"ServiceUnavailable", "ResourceExhausted"})


def resilient_post(
    url: str,
    *,
    max_retries: int = 3,
    backoff_base: float = 1.0,
    **kwargs: Any,
) -> requests.Response:
    """POST with exponential backoff retry on transient Fuel iX errors.

    Retries on:
      - HTTP 429, 500, 502, 503, 504  (transient server errors)
      - requests.ConnectionError       (network drop)
      - requests.Timeout               (API unresponsive)

    Backoff schedule: backoff_base * (2 ** attempt) seconds.
    Defaults: 1s -> 2s -> 4s for max_retries=3.
    On the final attempt all errors propagate to the caller unchanged.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(url, **kwargs)
            if resp.status_code in _FUELIX_RETRYABLE_STATUS and attempt < max_retries:
                delay = backoff_base * (2 ** attempt)
                _log.warning(
                    "Fuel iX HTTP %d on attempt %d/%d — retrying in %.0fs",
                    resp.status_code, attempt + 1, max_retries, delay,
                )
                time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            if attempt == max_retries:
                raise
            delay = backoff_base * (2 ** attempt)
            _log.warning(
                "Fuel iX connection error on attempt %d/%d: %s — retrying in %.0fs",
                attempt + 1, max_retries, type(exc).__name__, delay,
            )
            time.sleep(delay)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("resilient_post: exhausted all retries with no response")


def resilient_bq_query(
    client: Any,
    sql: str,
    *,
    max_retries: int = 3,
    backoff_base: float = 1.0,
) -> list[dict]:
    """Execute a BigQuery query with retry on transient errors.

    Retries on google.api_core.exceptions.ServiceUnavailable and
    google.api_core.exceptions.ResourceExhausted (quota exceeded).

    Returns a list of row dicts on success.
    All other exceptions propagate immediately.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            return [dict(r) for r in client.query(sql).result()]
        except Exception as exc:
            exc_name = type(exc).__name__
            if exc_name in _BQ_RETRYABLE_NAMES and attempt < max_retries:
                delay = backoff_base * (2 ** attempt)
                _log.warning(
                    "BigQuery %s on attempt %d/%d — retrying in %.0fs",
                    exc_name, attempt + 1, max_retries, delay,
                )
                time.sleep(delay)
                last_exc = exc
                continue
            raise

    if last_exc is not None:
        raise last_exc
    raise RuntimeError("resilient_bq_query: exhausted all retries")


# ---------------------------------------------------------------------------
# Phase 5B -- Startup health checks
# ---------------------------------------------------------------------------

def _check_fuelix(api_key: str, base_url: str = "https://api.fuelix.ai") -> tuple[str, str]:
    """Check AI service reachability (Gemini via Vertex AI). Returns (status, plain-English message).

    Name kept for compatibility with existing callers; `api_key`/`base_url`
    are unused now that the AI call goes through core.ai_client (ADC-based
    auth, no API key).
    """
    from core.ai_client import check_ai_reachable  # noqa: PLC0415
    return check_ai_reachable()


def _check_bq(project: str) -> tuple[str, str]:
    """Validate BigQuery ADC credentials with a dry-run query. Returns (status, plain-English message)."""
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from google.cloud import bigquery  # type: ignore

        client = bigquery.Client(project=project)
        job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        client.query("SELECT 1", job_config=job_config)
        return "OK", ""
    except Exception as exc:
        msg = str(exc)[:140]
        if "credentials" in msg.lower() or "401" in msg or "403" in msg or "unauthenticated" in msg.lower():
            return "FAIL", "Your data access has expired. To renew it, run:  python refresh_adc_scopes.py"
        return "WARN", f"The data connection check was inconclusive ({type(exc).__name__}). Try restarting."


def _check_knowledge(artifacts_dir: Path) -> tuple[str, str]:
    """Verify the knowledge index exists and contains at least one campaign. Returns (status, plain-English message)."""
    try:
        import json
        idx_path = Path(artifacts_dir) / "semantic_knowledge_index.json"
        if not idx_path.exists():
            return "FAIL", (
                "No campaign data was found. To load it, run:\n"
                "    python -m knowledge_base.vibe_octo_knowledge --full-refresh"
            )
        data = json.loads(idx_path.read_text(encoding="utf-8"))
        campaigns = data.get("campaigns", [])
        if not campaigns:
            return "FAIL", (
                "Campaign data is empty. To reload it, run:\n"
                "    python -m knowledge_base.vibe_octo_knowledge --full-refresh"
            )
        return "OK", ""
    except Exception as exc:
        return "FAIL", (
            f"Campaign data could not be read ({type(exc).__name__}). Try running:\n"
            "    python -m knowledge_base.vibe_octo_knowledge --full-refresh"
        )


def run_startup_health_check(
    api_key: str,
    bq_project: str,
    artifacts_dir: Path,
    base_url: str = "https://api.fuelix.ai",
) -> bool:
    """Run all pre-session health checks and print a plain-English status summary.

    Returns True when all checks are OK or WARN (degraded but operable).
    Returns False when any check is FAIL (caller should block session start).
    """
    checks = [
        _check_fuelix(api_key, base_url),
        _check_bq(bq_project),
        _check_knowledge(artifacts_dir),
    ]

    failures = [(status, msg) for status, msg in checks if status == "FAIL"]

    if not failures:
        print("All connections are working — you're good to go!")
        print()
        return True

    count = len(failures)
    intro = "something needs attention" if count == 1 else f"{count} things need attention"
    print(f"Heads up — {intro} before you can get started.")
    print("Here is what to do, step by step:")
    print()
    for i, (_, msg) in enumerate(failures, 1):
        print(f"  Step {i} — {msg}")
        print()
    print("Once all steps are done, restart the tool and you should be good to go.")
    print()
    return False
