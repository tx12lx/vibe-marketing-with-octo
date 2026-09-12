"""fuelix_relay/main.py -- minimal Cloud Run proxy for reaching Fuel iX from a
network that can't reach it directly.

Why this exists: this app's VM has a network egress restriction that only
allows Google-owned destinations (that's already why it can't reach GitHub or
BigQuery from anywhere else) -- confirmed live that it cannot reach
api.fuelix.ai directly either. It CAN reach any *.run.app URL, because Cloud
Run is itself Google infrastructure. This service is that reachable middle
point: it holds the real FUELIX_API_KEY (the VM never sees it), and forwards
whatever chat-completion request it receives straight to Fuel iX, returning
the response unchanged. See core/ai_client.py's FUELIX_RELAY_URL for the
caller side of this.

This is a dumb, transparent pass-through on purpose -- no retry logic, no
response parsing, nothing duplicated from core/ai_client.py. Every real
decision (what to send, how to retry, how to parse the answer) stays in one
place: the caller. This file's only job is "be reachable from a restricted
network and hold the secret so the caller doesn't have to."

Access control: deployed WITHOUT --allow-unauthenticated. Cloud Run's own IAM
layer verifies every caller's Google-signed identity token before a request
ever reaches this code -- only identities granted roles/run.invoker on this
specific service can call it at all. This file does not check auth itself;
there is deliberately no API-key or shared-secret check here to duplicate
what Cloud Run's ingress already guarantees.
"""
from __future__ import annotations

import os

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

app = FastAPI(title="Vibe OCTO -- Fuel iX relay")

_FUELIX_BASE = os.getenv("FUELIX_BASE_URL", "https://api.fuelix.ai")
_FUELIX_API_KEY = os.getenv("FUELIX_API_KEY", "")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "configured": bool(_FUELIX_API_KEY)}


@app.post("/v1/chat/completions")
async def relay_chat_completions(request: Request) -> Response:
    body = await request.body()
    resp = httpx.post(
        f"{_FUELIX_BASE}/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {_FUELIX_API_KEY}",
            "Content-Type": "application/json",
        },
        content=body,
        timeout=180,
    )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
