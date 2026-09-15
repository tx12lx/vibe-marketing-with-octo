"""teammate_gateway/main.py -- forwards a signed-in teammate's request through
to the real app running on the VM, with no terminal and no personal Google
Cloud access of their own.

What this is: a small, single-purpose relay. It does exactly one job now:

Forwarding: Google's own sign-in checkpoint (Identity-Aware Proxy) is switched
on directly in front of this address -- it already verifies who someone is
and checks them against its own access list before a request ever reaches
this code, and it sets a trustworthy header (x-goog-authenticated-user-email)
recording who that verified person is. This file used to run its own,
separate "sign in with Google" flow to do that same job (kept as a fallback
before the checkpoint's own setup was confirmed working) -- that homemade
sign-in step, its own allowed-emails list, and its own session cookie have
all been retired now that the checkpoint does this job before this code ever
runs. Every request that reaches this code is forwarded, unchanged, to the
real app already running on the VM (over the background IAP tunnel
tunnel_manager.py maintains), carrying that same verified-email header along
so the VM app's audit trail (api/web_app.py's _caller_identity()) attributes
every correction or confirmation to the actual person, not a placeholder.

Nothing else lives here: no business logic, no knowledge-layer access, no
BigQuery calls -- all of that stays exactly where it already runs, on the VM.
"""
from __future__ import annotations

import logging
import os

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

import tunnel_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_log = logging.getLogger(__name__)

app = FastAPI(title="Vibe OCTO -- teammate access gateway")

_TUNNEL_TARGET = f"http://localhost:{tunnel_manager.TUNNEL_LOCAL_PORT}"

_STRIP_REQUEST_HEADERS = frozenset({"host", "cookie", "content-length"})
_STRIP_RESPONSE_HEADERS = frozenset({"content-length", "transfer-encoding", "connection"})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


def _verified_email(request: Request) -> "str | None":
    """The real person's email, as already verified by Google's checkpoint in
    front of this address -- Cloud Run's own ingress guarantees this header
    can only have been set by that checkpoint, never by the original caller,
    once the checkpoint is switched on for this service (which it is)."""
    raw = request.headers.get("x-goog-authenticated-user-email", "")
    if not raw:
        return None
    return raw.split(":", 1)[-1] or None


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    if path == "health":
        return Response(status_code=404)

    email = _verified_email(request)
    if not email:
        # Shouldn't happen once the checkpoint is switched on -- it wouldn't have
        # let the request reach here without this header. Refuse rather than
        # guess who this is.
        return Response(content="Could not verify who you are. Please try again.", status_code=403)

    body = await request.body()
    target_url = f"{_TUNNEL_TARGET}/{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP_REQUEST_HEADERS}
    headers["x-goog-authenticated-user-email"] = f"accounts.google.com:{email}"

    client = httpx.AsyncClient(timeout=200.0)
    upstream_request = client.build_request(
        request.method, target_url, params=request.query_params, headers=headers, content=body,
    )
    upstream = await client.send(upstream_request, stream=True)
    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _STRIP_RESPONSE_HEADERS}

    async def _stream_and_close():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    # Streamed rather than buffered so the live, plain-English reasoning
    # display (Server-Sent Events on /query) still arrives progressively
    # through the gateway instead of appearing all at once at the end.
    return StreamingResponse(
        _stream_and_close(), status_code=upstream.status_code,
        headers=response_headers, media_type=upstream.headers.get("content-type"),
    )


@app.on_event("startup")
async def startup() -> None:
    tunnel_manager.start_background_tunnel()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")
