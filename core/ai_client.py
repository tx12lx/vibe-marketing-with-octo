"""core/ai_client.py -- shared AI-calling helper (Gemini via Vertex AI).

Replaces the Fuel iX-specific request building and response parsing that
used to be duplicated, with real variation, across call sites in agents/*.py
and vibe_orchestrator.py. Every caller now builds a prompt and gets plain
text back through one function instead of hand-rolling headers, an
OpenAI-style payload, and its own response-parsing logic.

Auth: relies on Application Default Credentials -- the attached service
account when running on a GCE VM, no separate API key needed.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx
from google import genai
from google.genai import errors, types

_log = logging.getLogger(__name__)

# Same retry policy as core/resilience.py's resilient_bq_query:
# exponential backoff, 1s -> 2s -> 4s, max 3 retries. Retries on rate limits
# (429) and server errors (5xx); anything else (including permission errors)
# propagates immediately -- retrying a 403 just wastes three round trips.
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.0

# GEMINI_LOCATION's fallback must stay a Canadian region -- data-residency
# requirement (confirmed with Alex Everitt) -- even if the env var is unset.
_PROJECT = os.getenv("GEMINI_PROJECT_ID", "cdo-hsm-adobe-fda-np-9fbb44")
_LOCATION = os.getenv("GEMINI_LOCATION", "northamerica-northeast1")
_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

_client: Optional[genai.Client] = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(vertexai=True, project=_PROJECT, location=_LOCATION)
    return _client


def ask_ai(
    prompt: str,
    *,
    system: Optional[str] = None,
    temperature: float = 0.0,
    max_tokens: int = 1024,
    thinking_budget: Optional[int] = None,
) -> str:
    """Ask the AI a question and get plain text back.

    Replaces the old pattern of building a Fuel iX chat-completions request
    by hand (headers, OpenAI-style payload) and then parsing
    choices[0].message.content out of the JSON response -- Gemini's own
    response.text already gives back clean, assembled text.

    thinking_budget: Gemini 2.5 models spend part of max_tokens on hidden
    "thinking" tokens before writing the visible response -- for straightforward
    formatting tasks (e.g. "reply with only this JSON shape") that hidden spend
    can silently truncate the real output. Pass thinking_budget=0 to disable it
    for calls that don't need deliberation; leave unset (default) for anything
    that benefits from it, like SQL generation or intent classification.
    """
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=max_tokens,
        thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget) if thinking_budget is not None else None,
    )

    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = _get_client().models.generate_content(
                model=_MODEL,
                contents=prompt,
                config=config,
            )
            return (response.text or "").strip()
        except errors.ServerError as exc:
            last_exc = exc
            retryable = True
        except errors.ClientError as exc:
            last_exc = exc
            retryable = exc.code == 429  # rate limited -- worth retrying
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_exc = exc
            retryable = True  # network drop / unresponsive -- worth retrying
        if not retryable or attempt == _MAX_RETRIES:
            raise last_exc
        delay = _BACKOFF_BASE * (2 ** attempt)
        _log.warning(
            "Gemini call failed on attempt %d/%d (%s) -- retrying in %.0fs",
            attempt + 1, _MAX_RETRIES, type(last_exc).__name__, delay,
        )
        time.sleep(delay)

    raise last_exc  # pragma: no cover -- loop always returns or raises above


_EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
_EMBEDDING_DIMENSIONALITY = 768


def embed_text(text: str) -> list[float]:
    """Turn a sentence into a set of numbers that captures its meaning.

    Used by the knowledge layer to find similar past campaigns, rules, or
    glossary terms by what they mean rather than by matching stray words.
    Same Gemini/Vertex AI client, project, and region as ask_ai() -- this is
    purely the "find related things" piece working alongside it, not a
    separate AI system. Truncated to 768 dimensions (Matryoshka-style output
    truncation, confirmed supported by this model) -- plenty for a corpus of
    this size, at a fraction of the storage/compute of the full 3072.
    """
    config = types.EmbedContentConfig(output_dimensionality=_EMBEDDING_DIMENSIONALITY)

    last_exc: Optional[Exception] = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = _get_client().models.embed_content(
                model=_EMBEDDING_MODEL,
                contents=[text],
                config=config,
            )
            return list(response.embeddings[0].values)
        except errors.ServerError as exc:
            last_exc = exc
            retryable = True
        except errors.ClientError as exc:
            last_exc = exc
            retryable = exc.code == 429
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            last_exc = exc
            retryable = True
        if not retryable or attempt == _MAX_RETRIES:
            raise last_exc
        delay = _BACKOFF_BASE * (2 ** attempt)
        _log.warning(
            "Gemini embedding call failed on attempt %d/%d (%s) -- retrying in %.0fs",
            attempt + 1, _MAX_RETRIES, type(last_exc).__name__, delay,
        )
        time.sleep(delay)

    raise last_exc  # pragma: no cover -- loop always returns or raises above


def check_ai_reachable() -> tuple[str, str]:
    """Startup health check -- mirrors core/resilience.py's old _check_fuelix.

    Returns (status, plain-English message), matching the existing
    (status, message) contract used by run_startup_health_check.
    """
    try:
        text = ask_ai("Reply with just the word: ok", max_tokens=10)
        if text:
            return "OK", ""
        return "WARN", "The AI service returned an empty response. Try again in a few minutes."
    except Exception as exc:  # noqa: BLE001 -- surfacing any failure as a plain-English health check result
        return "FAIL", f"We can't reach the AI service (Gemini): {exc}"
