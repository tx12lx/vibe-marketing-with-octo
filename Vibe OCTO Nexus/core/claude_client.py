from __future__ import annotations

import json
import os
import re
import time

import requests

_BASE_URL = "https://api.fuelix.ai"
_DEFAULT_MODEL = "claude-sonnet-4"
_MAX_TOKENS = 16384

_LEARN_SYSTEM = (
    "You are a campaign brief analyst for a telecom marketing team. "
    "Study campaign briefs and extract structured patterns in how targeting criteria are expressed. "
    "Be precise: capture exact thresholds and values when stated. "
    "When criteria are vague or contradictory, flag them with a low confidence score. "
    "Return only valid JSON."
)

_GENERATE_SYSTEM = (
    "You are the Vibe Briefing brief generation assistant for a telecom marketing team. "
    "You generate standardized campaign data brief templates based on portfolio knowledge "
    "and stakeholder requests. Your output is consumed directly by downstream tools — "
    "be precise, use only known patterns, and flag anything that needs stakeholder input. "
    "Return only valid JSON."
)

_FEEDBACK_SYSTEM = (
    "You are the Vibe Briefing feedback processor. "
    "You extract structured learnings from stakeholder feedback on campaign briefs, "
    "apply corrections, and return an updated brief with a knowledge extraction. "
    "Every correction you capture improves the portfolio knowledge base. "
    "Return only valid JSON."
)


class ClaudeClient:
    def __init__(self):
        self.api_key = os.getenv("FUELIX_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "FUELIX_API_KEY is not set. Add it to your .env file."
            )
        self.model = os.getenv("FUELIX_MODEL", _DEFAULT_MODEL)

    def learn_from_brief(
        self,
        brief_content: str,
        learn_prompt_template: str,
        metadata: dict,
    ) -> dict:
        user_text = learn_prompt_template.format(
            CAMPAIGN_ID=metadata.get("campaign_id", ""),
            PORTFOLIO=metadata.get("portfolio", ""),
            PURPOSE=metadata.get("purpose", ""),
            MEDIUM=metadata.get("medium", ""),
            CADENCE=metadata.get("cadence", ""),
            BRIEF_CONTENT=brief_content,
        )
        raw = self._call(_LEARN_SYSTEM, user_text)
        return self._extract_json(raw)

    def generate_brief(
        self,
        request: str,
        generate_prompt_template: str,
        portfolio: str,
        portfolio_description: str,
        glossary_terms_context: str,
        term_count: int,
        campaign_summaries: str,
    ) -> dict:
        user_text = generate_prompt_template.format(
            REQUEST=request,
            PORTFOLIO=portfolio,
            PORTFOLIO_DESCRIPTION=portfolio_description,
            TERM_COUNT=term_count,
            GLOSSARY_TERMS=glossary_terms_context,
            CAMPAIGN_SUMMARIES=campaign_summaries,
        )
        raw = self._call(_GENERATE_SYSTEM, user_text)
        return self._extract_json(raw)

    def process_feedback(
        self,
        brief_json: dict,
        feedback_text: str,
        feedback_prompt_template: str,
    ) -> dict:
        user_text = feedback_prompt_template.format(
            BRIEF_JSON=json.dumps(brief_json, indent=2, ensure_ascii=False),
            FEEDBACK_TEXT=feedback_text,
        )
        raw = self._call(_FEEDBACK_SYSTEM, user_text)
        return self._extract_json(raw)

    def translate_brief(
        self,
        brief_content: str,
        system_prompt: str,
        translate_prompt_template: str,
        bq_schema: str,
        glossary_json: str,
        metadata: dict,
    ) -> dict:
        user_text = translate_prompt_template.format(
            METADATA=json.dumps(metadata, indent=2),
            BRIEF_CONTENT=brief_content,
        )
        system = (
            f"{system_prompt}\n\n"
            f"BigQuery Schema (use ONLY these field names):\n{bq_schema}"
        )
        user = (
            f"Portfolio Glossary (primary reference for term mapping):\n{glossary_json}"
            f"\n\n{user_text}"
        )
        raw = self._call(system, user)
        return self._extract_json(raw)

    def _call(self, system: str, user: str, max_retries: int = 3) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": _MAX_TOKENS,
            "temperature": 0,
        }

        for attempt in range(max_retries):
            try:
                response = requests.post(
                    f"{_BASE_URL}/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=300,
                )
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"].strip()
            except Exception as exc:
                if attempt == max_retries - 1:
                    raise RuntimeError(
                        f"Fuel iX API call failed after {max_retries} attempts: {exc}"
                    ) from exc
                time.sleep(2 ** attempt)

    def _extract_json(self, text: str) -> dict:
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass
        match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
        if match:
            return json.loads(match.group(1))
        start = text.find("{")
        end = text.rfind("}") + 1
        if start != -1 and end > start:
            return json.loads(text[start:end])
        raise ValueError(
            f"No valid JSON in API response ({len(text)} chars). "
            f"First 400: {text[:400]!r}  ...  Last 200: {text[-200:]!r}"
        )
