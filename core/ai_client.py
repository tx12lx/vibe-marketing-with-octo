"""core/ai_client.py -- shared AI-calling helper (Gemini via Vertex AI).

Replaces the Fuel iX-specific request building and response parsing that
used to be duplicated, with real variation, across ten call sites in
agents/*.py, core/business_rules_registry.py, and vibe_orchestrator.py.
Every caller now builds a prompt and gets plain text back through one
function instead of hand-rolling headers, an OpenAI-style payload, and its
own response-parsing logic.

Auth: relies on Application Default Credentials -- the attached service
account when running on a GCE VM, no separate API key needed.
"""
from __future__ import annotations

import os
from typing import Optional

from google import genai
from google.genai import types

_PROJECT = os.getenv("GEMINI_PROJECT_ID", "cdo-hsm-adobe-fda-np-9fbb44")
_LOCATION = os.getenv("GEMINI_LOCATION", "us-central1")
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
) -> str:
    """Ask the AI a question and get plain text back.

    Replaces the old pattern of building a Fuel iX chat-completions request
    by hand (headers, OpenAI-style payload) and then parsing
    choices[0].message.content out of the JSON response -- Gemini's own
    response.text already gives back clean, assembled text.
    """
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=max_tokens,
    )
    response = _get_client().models.generate_content(
        model=_MODEL,
        contents=prompt,
        config=config,
    )
    return (response.text or "").strip()


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
