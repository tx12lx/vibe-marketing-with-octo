"""relay/server.py -- public front door that relays browser traffic to the VM.

Deployed on Cloud Run. Holds a single persistent WebSocket connection from
relay/client.py (running on the VM) and forwards every incoming HTTP request
to it, matching responses back by a correlation id. Knows nothing about
sizing questions, campaigns, or the AI -- pure transport, kept deliberately
separate from agents/, core/, and api/.

Cloud Run must be run with --min-instances=1 --max-instances=1 so exactly one
instance ever holds the tunnel connection.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Optional

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

_log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Vibe OCTO relay")

_REQUEST_TIMEOUT_SECONDS = 60.0

_tunnel: Optional[WebSocket] = None
_pending: dict[str, asyncio.Future] = {}


@app.websocket("/tunnel")
async def tunnel(ws: WebSocket) -> None:
    global _tunnel
    await ws.accept()
    if _tunnel is not None:
        _log.warning("A second tunnel connection arrived while one was already active -- closing the old one.")
        await _tunnel.close()
    _tunnel = ws
    _log.info("Tunnel connected.")
    try:
        while True:
            envelope = await ws.receive_json()
            request_id = envelope.get("id", "")
            future = _pending.pop(request_id, None)
            if future is not None and not future.done():
                future.set_result(envelope)
    except WebSocketDisconnect:
        _log.warning("Tunnel disconnected.")
    finally:
        if _tunnel is ws:
            _tunnel = None
        for future in _pending.values():
            if not future.done():
                future.set_exception(RuntimeError("tunnel disconnected"))
        _pending.clear()


@app.api_route("/", methods=["GET", "POST"])
async def relay_root(request: Request) -> Response:
    return await relay("", request)


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def relay(path: str, request: Request) -> Response:
    if _tunnel is None:
        return JSONResponse(
            status_code=503,
            content={"error": "The tool isn't reachable right now. Please try again shortly."},
        )

    request_id = uuid.uuid4().hex
    body = await request.body()
    envelope = {
        "id": request_id,
        "method": request.method,
        "path": f"/{path}",
        "query": str(request.url.query),
        "headers": dict(request.headers),
        "body": body.decode("utf-8", errors="replace"),
    }

    future: asyncio.Future = asyncio.get_event_loop().create_future()
    _pending[request_id] = future
    try:
        await _tunnel.send_json(envelope)
        response_envelope = await asyncio.wait_for(future, timeout=_REQUEST_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        _pending.pop(request_id, None)
        return JSONResponse(
            status_code=504,
            content={"error": "The tool took too long to respond. Please try again."},
        )
    except Exception:
        _pending.pop(request_id, None)
        return JSONResponse(
            status_code=503,
            content={"error": "The tool isn't reachable right now. Please try again shortly."},
        )

    return Response(
        content=response_envelope.get("body", ""),
        status_code=response_envelope.get("status", 200),
        media_type=response_envelope.get("content_type", "application/octet-stream"),
    )


@app.get("/healthz")
async def healthz() -> dict:
    return {"tunnel_connected": _tunnel is not None}
