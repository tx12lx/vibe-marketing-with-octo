"""core/json_extract.py -- shared helper for parsing a JSON object out of a raw
LLM text response.

Used by every agent that asks the model to "return only JSON" (agents/nexus_agent.py,
agents/feedback_agent.py) -- previously each had its own hand-rolled version with
subtly different, inconsistent fallback behavior. This is the one version both use.

Tries three things in order:
  1. The whole response is already a clean JSON object.
  2. The JSON is wrapped in a ```json ... ``` markdown fence.
  3. A JSON object is embedded somewhere in surrounding prose -- found by counting
     braces from the first '{' to its actual matching '}', not just the last '}' in
     the whole text (which would wrongly swallow trailing prose like "let me know
     if you need changes {smile}").

Raises ValueError if none of these produce valid JSON -- callers that want a soft
failure should catch that (as both existing call sites already do) rather than this
module returning None, so a missing value is never silently mistaken for "the model
legitimately returned nothing".
"""
from __future__ import annotations

import json
import re


def extract_json(text: str) -> dict:
    text = text.strip()
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    fence_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    start = text.find("{")
    if start != -1:
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break

    raise ValueError(f"No valid JSON in response. First 300 chars: {text[:300]!r}")
