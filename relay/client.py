"""relay/client.py -- runs on the VM, opens the outbound tunnel to relay/server.py.

Keeps a single persistent WebSocket connection to the deployed relay open,
authenticated as this machine's own attached service account (the same
ADC-based approach already used for BigQuery and Gemini -- no manual
credentials). For every request that arrives over the tunnel, calls the
already-running local app and relays the response back.

Runs as its own systemd service (vibe-octo-relay-client.service), separate
from vibe-octo-web.service.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import requests
import websockets
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

_RELAY_URL = os.environ["RELAY_URL"].rstrip("/")  # e.g. https://vibe-octo-relay-xxxx.a.run.app
_RELAY_WS_URL = _RELAY_URL.replace("https://", "wss://", 1) + "/tunnel"
_LOCAL_APP_URL = os.getenv("LOCAL_APP_URL", "http://localhost:443")

_BACKOFF_BASE = 1.0
_BACKOFF_CAP = 30.0

# Headers that must not be blindly forwarded from the envelope to the local call.
_DROP_HEADERS = {"host", "content-length"}


def _mint_id_token() -> str:
    return id_token.fetch_id_token(GoogleAuthRequest(), _RELAY_URL)


async def _handle_envelope(ws, envelope: dict) -> None:
    method = envelope.get("method", "GET")
    path = envelope.get("path", "/")
    query = envelope.get("query", "")
    headers = {k: v for k, v in envelope.get("headers", {}).items() if k.lower() not in _DROP_HEADERS}
    body = envelope.get("body", "").encode("utf-8")
    url = f"{_LOCAL_APP_URL}{path}"
    if query:
        url = f"{url}?{query}"

    try:
        resp = await asyncio.to_thread(
            requests.request, method, url, headers=headers, data=body, timeout=55,
        )
        response_envelope = {
            "id": envelope["id"],
            "status": resp.status_code,
            "content_type": resp.headers.get("content-type", "application/octet-stream"),
            "body": resp.text,
        }
    except Exception as exc:
        _log.warning("Local call to %s failed: %s", url, exc)
        response_envelope = {
            "id": envelope["id"],
            "status": 502,
            "content_type": "application/json",
            "body": '{"error": "The tool encountered a problem answering this request."}',
        }

    await ws.send(_dumps(response_envelope))


def _dumps(obj: dict) -> str:
    import json
    return json.dumps(obj)


def _loads(text: str) -> dict:
    import json
    return json.loads(text)


async def run_forever() -> None:
    attempt = 0
    while True:
        try:
            token = _mint_id_token()
            async with websockets.connect(
                _RELAY_WS_URL,
                additional_headers={"Authorization": f"Bearer {token}"},
                ping_interval=20,
                ping_timeout=20,
            ) as ws:
                _log.info("Tunnel connected to %s", _RELAY_WS_URL)
                attempt = 0
                async for raw in ws:
                    envelope = _loads(raw)
                    asyncio.create_task(_handle_envelope(ws, envelope))
        except Exception as exc:
            delay = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP)
            _log.warning("Tunnel connection lost (%s) -- reconnecting in %.0fs", exc, delay)
            attempt += 1
            time.sleep(delay)


if __name__ == "__main__":
    asyncio.run(run_forever())
