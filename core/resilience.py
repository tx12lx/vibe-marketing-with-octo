"""
core/resilience.py -- Centralized retry logic and startup health checks.

Retry policy for BigQuery calls: 1s -> 2s -> 4s, max 3 retries.
  Retries on ServiceUnavailable and ResourceExhausted (quota) errors.

run_startup_health_check() gates the session on Gemini reachability,
  BigQuery ADC validity, and knowledge index presence.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

_log = logging.getLogger(__name__)

# BQ exception class names that warrant a retry
_BQ_RETRYABLE_NAMES = frozenset({"ServiceUnavailable", "ResourceExhausted"})

# This tool must only ever read from campaign data -- never write to it. This
# is enforced twice: the service account itself has no data-write role on
# that project (the real backstop), and this check, so a bug in AI-generated
# SQL is caught here too, not just relied on to never happen.
_WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|CALL)\b",
    re.IGNORECASE,
)


class ReadOnlyViolation(RuntimeError):
    """Raised when generated SQL isn't a plain read query -- refused before it ever reaches BigQuery."""


def assert_read_only_sql(sql: str) -> None:
    stripped = sql.strip().lstrip("(")
    if not re.match(r"^(SELECT|WITH)\b", stripped, re.IGNORECASE):
        raise ReadOnlyViolation(
            "Generated SQL must start with SELECT or WITH -- refusing to run anything else."
        )
    match = _WRITE_KEYWORDS.search(sql)
    if match:
        raise ReadOnlyViolation(
            f"Generated SQL contains a write/DDL keyword ('{match.group(0)}') -- refusing to run it."
        )


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
    assert_read_only_sql(sql)
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


def _check_knowledge() -> tuple[str, str]:
    """Report the knowledge layer's real, live status. Returns (status, plain-English message).

    Can the database be opened, and does it have synced tables in it.
    """
    from knowledge.store import check_health  # noqa: PLC0415
    return check_health()


def run_startup_health_check(
    api_key: str,
    bq_project: str,
    base_url: str = "https://api.fuelix.ai",
) -> bool:
    """Run all pre-session health checks and print a plain-English status summary.

    Returns True when all checks are OK or WARN (degraded but operable).
    Returns False when any check is FAIL (caller should block session start).
    """
    checks = [
        _check_fuelix(api_key, base_url),
        _check_bq(bq_project),
        _check_knowledge(),
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
