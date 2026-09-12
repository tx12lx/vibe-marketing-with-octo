"""teammate_gateway/main.py -- lets a teammate use the web app through a
normal link and a real sign-in, with no terminal and no personal Google Cloud
access of their own.

What this is: a small, single-purpose doorway. It does exactly two jobs and
nothing else:

1. Sign-in gate: a teammate visiting this address is sent through a real
   "Sign in with Google" flow (see tunnel_manager.py's docstring for why this
   exists rather than IAP's own managed version -- this project's IAP
   consent-screen setup was never confirmed permission-free, and this
   approach was already proven to need no new permission at all). Only a
   verified @telus.com account is let through -- checked server-side against
   Google's own token-info endpoint, not just requested via the `hd` login
   hint (a hint alone is not a security check).

2. Forwarding: once signed in, every request is forwarded, unchanged, to the
   real app already running on the VM (over the background IAP tunnel
   tunnel_manager.py maintains), with the signed-in person's own verified
   email attached as the same header IAP itself would set
   (x-goog-authenticated-user-email) -- the VM app already reads exactly this
   header for its audit trail (api/web_app.py's _caller_identity()), so every
   correction or confirmation a teammate makes is attributed to them
   specifically, not to a generic placeholder.

Nothing else lives here: no business logic, no knowledge-layer access, no
BigQuery calls -- all of that stays exactly where it already runs, on the VM.
This file's only job is "verify who you are, then get out of the way."

The VM's own shared-password gate (api/web_app.py's WEB_APP_PASSWORD) is
satisfied transparently on every forwarded request using its already-known
secret, rather than making a teammate log in twice. A teammate's browser only
ever holds this gateway's own session cookie.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from urllib.parse import urlencode

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse

import tunnel_manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_log = logging.getLogger(__name__)

app = FastAPI(title="Vibe OCTO -- teammate access gateway")

_OAUTH_CLIENT_ID = os.environ["GATEWAY_OAUTH_CLIENT_ID"]
_OAUTH_CLIENT_SECRET = os.environ["GATEWAY_OAUTH_CLIENT_SECRET"]
_SESSION_SECRET = os.environ["GATEWAY_SESSION_SECRET"].encode()
_ALLOWED_DOMAIN = os.environ.get("GATEWAY_ALLOWED_DOMAIN", "telus.com")

# Named allowlist, e.g. "tian.xia@telus.com,weibo.wang@telus.com" -- when set,
# only these specific addresses may sign in, on top of (not instead of) the
# domain check above. Empty (the default) means "anyone on the domain."
_ALLOWED_EMAILS = frozenset(
    e.strip().lower() for e in os.environ.get("GATEWAY_ALLOWED_EMAILS", "").split(",") if e.strip()
)

# The VM app's own shared-password gate (api/web_app.py) -- satisfied here so
# a signed-in teammate is never asked to enter it separately. Mirrors
# api/web_app.py's _expected_auth_cookie_value() exactly: same cookie name,
# same HMAC construction, so the value computed here is valid there.
_VM_AUTH_COOKIE_NAME = "octo_auth"
_VM_WEB_APP_SECRET = os.environ["VM_WEB_APP_SECRET"].encode()
_VM_AUTH_COOKIE_VALUE = hmac.new(_VM_WEB_APP_SECRET, b"vibe-octo-authenticated", hashlib.sha256).hexdigest()

_GATEWAY_COOKIE_NAME = "vibe_octo_gateway_session"
_SESSION_TTL_SECONDS = 8 * 3600
_TUNNEL_TARGET = f"http://localhost:{tunnel_manager.TUNNEL_LOCAL_PORT}"

# Requests to these paths are handled by the dedicated routes below, never by
# the catch-all proxy -- listed so the proxy route can refuse to shadow them
# if FastAPI's routing order is ever changed.
_RESERVED_PATHS = frozenset({"health", "login", "oauth/callback"})


def _redirect_uri(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/oauth/callback"


def _sign_session(email: str) -> str:
    expiry = int(time.time()) + _SESSION_TTL_SECONDS
    payload = f"{email}|{expiry}"
    sig = hmac.new(_SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def _verify_session(cookie_value: str) -> "str | None":
    try:
        email, expiry_str, sig = cookie_value.rsplit("|", 2)
        expected = hmac.new(_SESSION_SECRET, f"{email}|{expiry_str}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        if int(expiry_str) < int(time.time()):
            return None
        return email
    except Exception:
        return None


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/login")
async def login(request: Request) -> RedirectResponse:
    params = {
        "client_id": _OAUTH_CLIENT_ID,
        "redirect_uri": _redirect_uri(request),
        "response_type": "code",
        "scope": "openid email",
        "hd": _ALLOWED_DOMAIN,
        "prompt": "select_account",
    }
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}")


@app.get("/oauth/callback")
async def oauth_callback(request: Request, code: str = "", error: str = "") -> Response:
    if error or not code:
        return Response(content=f"Sign-in failed: {error or 'no code returned'}", status_code=400)

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": _OAUTH_CLIENT_ID,
                "client_secret": _OAUTH_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": _redirect_uri(request),
            },
        )
    if token_resp.status_code != 200:
        _log.warning("OAuth token exchange failed: %s", token_resp.text[:300])
        return Response(content="Sign-in failed during token exchange.", status_code=400)
    id_token = token_resp.json().get("id_token", "")

    # tokeninfo verifies the token's signature/expiry server-side for us --
    # simpler and safer here than hand-rolling JWT verification for a
    # low-traffic internal tool.
    async with httpx.AsyncClient() as client:
        info_resp = await client.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": id_token})
    if info_resp.status_code != 200:
        return Response(content="Could not verify your sign-in with Google.", status_code=400)
    info = info_resp.json()

    if info.get("aud") != _OAUTH_CLIENT_ID:
        return Response(content="Token audience mismatch -- refusing to sign in.", status_code=403)

    email = info.get("email", "")
    email_verified = info.get("email_verified") in (True, "true")
    if not email_verified:
        return Response(content="Google has not verified this email address.", status_code=403)
    if not email.lower().endswith(f"@{_ALLOWED_DOMAIN}"):
        return Response(
            content=f"Access is restricted to @{_ALLOWED_DOMAIN} accounts. You signed in as {email}.",
            status_code=403,
        )
    if _ALLOWED_EMAILS and email.lower() not in _ALLOWED_EMAILS:
        return Response(
            content=f"Access is restricted to specific accounts. You signed in as {email}.",
            status_code=403,
        )

    resp = RedirectResponse("/")
    resp.set_cookie(
        _GATEWAY_COOKIE_NAME, _sign_session(email),
        httponly=True, secure=True, samesite="lax", max_age=_SESSION_TTL_SECONDS,
    )
    _log.info("Signed in: %s", email)
    return resp


_STRIP_REQUEST_HEADERS = frozenset({"host", "cookie", "content-length"})
_STRIP_RESPONSE_HEADERS = frozenset({"content-length", "transfer-encoding", "connection"})


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(request: Request, path: str):
    if path in _RESERVED_PATHS:
        return Response(status_code=404)

    cookie_value = request.cookies.get(_GATEWAY_COOKIE_NAME, "")
    email = _verify_session(cookie_value) if cookie_value else None
    if not email:
        return RedirectResponse("/login")

    body = await request.body()
    target_url = f"{_TUNNEL_TARGET}/{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _STRIP_REQUEST_HEADERS}
    headers["cookie"] = f"{_VM_AUTH_COOKIE_NAME}={_VM_AUTH_COOKIE_VALUE}"
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
    uvicorn.run(app, host="0.0.0.0", port=port)
