"""core/ai_client.py -- shared AI-calling helper.

ask_ai() calls Claude Sonnet 5 through Fuel iX -- TELUS's internal LLM
gateway -- via its OpenAI-compatible /v1/chat/completions endpoint. Every
caller in agents/*.py and knowledge/sync_schema.py builds a prompt and gets
plain text back through this one function instead of hand-rolling Fuel iX
request building and response parsing itself (that duplication is what this
file replaced the first time Fuel iX was wired in).

This file previously also called Gemini/Vertex AI for embeddings
(embed_text()), to support a campaign-similarity-search feature. That
feature (knowledge/store.py's campaign_summaries table and
knowledge/retrieve.py's find_similar_campaigns()) was removed during the
2026-09-11 knowledge-layer cleanup as dead weight from a scope this tool no
longer has -- it never held a real record and was retrieved on every single
request for nothing. embed_text() was its only caller, so it went too, and
with it the google-genai dependency and every GEMINI_* env var. Fuel iX is
now the sole model provider for this app.

Auth, direct mode (FUELIX_RELAY_URL unset): FUELIX_API_KEY (bearer token),
sent straight to Fuel iX. Works from anywhere with normal internet access
(e.g. a developer's laptop).

Auth, relayed mode (FUELIX_RELAY_URL set): this app's production compute (a
VM whose network only reaches Google-owned destinations -- the same
restriction that already blocks it from GitHub) cannot reach api.fuelix.ai
directly, confirmed live 2026-09-11. It can reach any *.run.app URL, since
Cloud Run is itself Google infrastructure -- so requests go instead to a
small relay service (fuelix_relay/) deployed on Cloud Run, which holds the
real FUELIX_API_KEY (this process never needs it) and forwards the request
to Fuel iX unchanged. Calls to the relay are authenticated with a
Google-signed identity token for this VM's own attached service account,
fetched fresh per call from the metadata server -- Cloud Run's own IAM layer
(the relay is deployed without --allow-unauthenticated) verifies that token
before the request ever reaches the relay's code; nothing in this app needs
to check auth itself.

Verified live against Fuel iX's real endpoint (2026-09-11) before wiring
this in: FUELIX_MODEL defaults to claude-sonnet-5 -- current-generation
Sonnet, both newer and cheaper than claude-sonnet-4/4.5/4.6 (Fuel iX silently
serves a newer snapshot for those older aliases anyway, e.g. requesting
claude-sonnet-4 actually returns claude-sonnet-4-6). Claude 3.5/3.7 Sonnet
are listed in Fuel iX's /v1/models catalog but are NOT actually callable for
this org: claude-3-7-sonnet returns 404 (publisher model not found/not
accessible), claude-3-5-haiku returns 403 (not enabled for this org), and no
claude-3-5-sonnet variant is listed at all. Don't reintroduce a 3.x model
string here without re-checking the live catalog first -- it may still list
models that no longer resolve to anything callable.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token as google_id_token

_log = logging.getLogger(__name__)

# Same retry policy as core/resilience.py's resilient_bq_query:
# exponential backoff, 1s -> 2s -> 4s, max 3 retries. Retries on rate limits
# (429) and server errors (5xx); anything else (including permission errors)
# propagates immediately -- retrying a 403 just wastes three round trips.
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0


# ---------------------------------------------------------------------------
# Fuel iX (ask_ai) -- text generation via Claude Sonnet 5
# ---------------------------------------------------------------------------

_FUELIX_BASE = os.getenv("FUELIX_BASE_URL", "https://api.fuelix.ai")
_FUELIX_API_KEY = os.getenv("FUELIX_API_KEY", "")
_FUELIX_MODEL = os.getenv("FUELIX_MODEL", "claude-sonnet-5")

# Set only on compute that can't reach api.fuelix.ai directly -- see the module
# docstring's "Auth, relayed mode" section. Unset (the common case for local dev,
# where normal internet access works) means calls go straight to Fuel iX.
_FUELIX_RELAY_URL = os.getenv("FUELIX_RELAY_URL", "").rstrip("/")


def _fuelix_request_target() -> tuple[str, dict]:
    """(base_url, auth_header) for this call.

    Relayed mode: a fresh Google-signed identity token for this instance's own
    attached service account, audience-scoped to the relay's URL -- fetched from
    the metadata server, a local call with negligible overhead next to the AI
    request itself, so no caching is needed. Cloud Run verifies this token
    before the request reaches the relay; the relay itself never checks auth.
    Direct mode: the real Fuel iX API key as a bearer token, same as always.
    """
    if _FUELIX_RELAY_URL:
        token = google_id_token.fetch_id_token(GoogleAuthRequest(), _FUELIX_RELAY_URL)
        return _FUELIX_RELAY_URL, {"Authorization": f"Bearer {token}"}
    return _FUELIX_BASE, {"Authorization": f"Bearer {_FUELIX_API_KEY}"}


def ask_ai(
    prompt: str,
    *,
    system: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    thinking_budget: Optional[int] = None,
) -> str:
    """Ask the AI a question and get plain text back.

    thinking_budget: kept for call-site compatibility with every existing
    caller's signature (agents/*.py, knowledge/sync_schema.py already pass
    thinking_budget=0 or omit it). Claude Sonnet 5 has no fixed token budget
    for thinking -- that mechanism is removed on this model generation, and
    Fuel iX rejects the old {"type": "enabled", "budget_tokens": N} shape
    outright (400, confirmed live) -- so this only supports on/off:
      thinking_budget == 0  -> send {"type": "disabled"} (verified accepted)
      anything else (None, or any other value) -> omit the field entirely,
        which runs Sonnet 5's own default (adaptive) reasoning.
    Verified directly against Fuel iX with a reasoning-heavy prompt under a
    tight max_tokens: completion-token counts and output were identical
    across "omitted", "disabled", and "adaptive" -- unlike the Gemini path
    this replaced, there's no evidence hidden thinking tokens eat into the
    caller's own max_tokens budget through this endpoint.
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload: dict = {
        "model": _FUELIX_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if thinking_budget == 0:
        payload["thinking"] = {"type": "disabled"}

    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES + 1):
        retryable = False
        try:
            base_url, auth_header = _fuelix_request_target()
            resp = httpx.post(
                f"{base_url}/v1/chat/completions",
                headers={**auth_header, "Content-Type": "application/json"},
                json=payload,
                timeout=180,
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_exc = exc
            retryable = True
        else:
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"].strip()
            last_exc = RuntimeError(
                f"Fuel iX call failed ({resp.status_code}): {resp.text[:500]}"
            )
            retryable = resp.status_code == 429 or resp.status_code >= 500

        if not retryable or attempt == _MAX_RETRIES:
            raise last_exc
        delay = _BACKOFF_BASE * (2 ** attempt)
        _log.warning(
            "Fuel iX call failed on attempt %d/%d (%s) -- retrying in %.0fs",
            attempt + 1, _MAX_RETRIES, type(last_exc).__name__, delay,
        )
        time.sleep(delay)

    raise last_exc  # pragma: no cover -- loop always returns or raises above


def check_ai_reachable() -> tuple[str, str]:
    """Startup health check -- mirrors core/resilience.py's old _check_fuelix.

    Now genuinely checks Fuel iX (ask_ai's real backend again), not Gemini.
    Returns (status, plain-English message), matching the existing
    (status, message) contract used by run_startup_health_check.
    """
    try:
        text = ask_ai("Reply with just the word: ok", max_tokens=10)
        if text:
            return "OK", ""
        return "WARN", "The AI service returned an empty response. Try again in a few minutes."
    except Exception as exc:  # noqa: BLE001 -- surfacing any failure as a plain-English health check result
        return "FAIL", f"We can't reach the AI service (Fuel iX): {exc}"
