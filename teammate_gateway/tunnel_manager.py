"""teammate_gateway/tunnel_manager.py -- keeps a live IAP tunnel to the VM open
in the background, for as long as this container runs.

Why this exists: the VM's firewall only accepts inbound connections that
arrive through Google's own IAP tunneling service -- that's the one and only
door in, confirmed directly (see project history). Reaching it therefore
needs a Google identity with tunnel access already granted -- the shared
service account this app otherwise runs as does not have that access, and
granting it requires a level of project administration neither the app owner
nor this session has. The one identity that already works is the app owner's
own personal Google account (the same one used for every manual
`gcloud compute start-iap-tunnel` command run by hand throughout this
project) -- this module is that same manual step, automated: it holds a
refresh token for that account (never the account's password, and never a
long-lived access token -- those expire in an hour), mints a fresh access
token from it periodically via gcloud's own public installed-app OAuth client
(the same client `gcloud auth login` itself uses), and runs
`gcloud compute start-iap-tunnel` as a background subprocess authenticated
with that token, restarting it if it ever dies or before its token expires.

Tradeoff, stated plainly (the owner was walked through this before building
it): this ties the gateway's ability to reach the VM to the owner's own
access specifically. If that access is ever revoked or the owner leaves the
team, this stops working until re-pointed at a different credential -- it is
not tied to a durable service-account grant.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

import httpx

_log = logging.getLogger(__name__)

# A dedicated OAuth client used only for this tunnel's refresh token --
# switched away from gcloud CLI's own shared installed-app client after
# discovering that client's refresh tokens get silently rotated out from
# under us by the owner's routine day-to-day gcloud use, making any copy
# extracted for this container go stale unpredictably. This client is never
# touched by anything else, so its refresh token should stay stable.
# Unlike gcloud's public client, this one's secret is genuinely confidential
# (a "web" application type client) -- it must come from Secret Manager, not
# be hardcoded here. Falls back to gcloud's client only if unset, so this
# stays a drop-in replacement rather than a hard requirement.
_GCLOUD_CLIENT_ID = os.environ.get("TUNNEL_OAUTH_CLIENT_ID", "32555940559.apps.googleusercontent.com").strip()
_GCLOUD_CLIENT_SECRET = os.environ.get("TUNNEL_OAUTH_CLIENT_SECRET", "ZmssLNjJy2998hD4CTg2ejr2").strip()

_VM_NAME = os.environ.get("VM_NAME", "bq-test-vm2")
_VM_ZONE = os.environ.get("VM_ZONE", "northamerica-northeast1-a")
_GCP_PROJECT = os.environ.get("GCP_PROJECT", "cdo-hsm-adobe-fda-np-9fbb44")
TUNNEL_LOCAL_PORT = int(os.environ.get("TUNNEL_LOCAL_PORT", "8443"))

# Refresh well inside the access token's ~1 hour lifetime, and check the
# subprocess is still alive every minute in between so a dropped connection
# is noticed quickly rather than silently leaving the gateway unable to reach
# the VM until the next scheduled refresh.
_REFRESH_INTERVAL_SECONDS = 45 * 60
_HEALTH_CHECK_INTERVAL_SECONDS = 60

_tunnel_proc: "subprocess.Popen | None" = None
_lock = threading.Lock()


def _mint_access_token(refresh_token: str) -> str:
    resp = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": _GCLOUD_CLIENT_ID,
            "client_secret": _GCLOUD_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _stop_tunnel() -> None:
    global _tunnel_proc
    if _tunnel_proc is None:
        return
    _tunnel_proc.terminate()
    try:
        _tunnel_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        _tunnel_proc.kill()
    _tunnel_proc = None


def _start_tunnel(access_token: str) -> "subprocess.Popen":
    env = os.environ.copy()
    env["CLOUDSDK_AUTH_ACCESS_TOKEN"] = access_token
    return subprocess.Popen(
        [
            "gcloud", "compute", "start-iap-tunnel", _VM_NAME, "443",
            f"--local-host-port=localhost:{TUNNEL_LOCAL_PORT}",
            f"--zone={_VM_ZONE}", f"--project={_GCP_PROJECT}",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _tunnel_loop(refresh_token: str) -> None:
    global _tunnel_proc
    while True:
        try:
            token = _mint_access_token(refresh_token)
            with _lock:
                _stop_tunnel()
                _tunnel_proc = _start_tunnel(token)
            _log.info("IAP tunnel (re)started.")
        except Exception:
            _log.exception("Failed to (re)start the IAP tunnel -- will retry.")

        elapsed = 0
        while elapsed < _REFRESH_INTERVAL_SECONDS:
            time.sleep(_HEALTH_CHECK_INTERVAL_SECONDS)
            elapsed += _HEALTH_CHECK_INTERVAL_SECONDS
            with _lock:
                died = _tunnel_proc is not None and _tunnel_proc.poll() is not None
            if died:
                _log.warning("Tunnel process died -- restarting early.")
                break


def start_background_tunnel() -> None:
    """Call once at app startup. Reads the personal refresh token from
    GCLOUD_REFRESH_TOKEN (populated from Secret Manager) -- never logs it,
    never accepts it as a request parameter."""
    # .strip() guards against a trailing newline in the stored secret (e.g.
    # from piping a string into `gcloud secrets create ... --data-file=-`),
    # which would otherwise silently make every minted access token invalid.
    refresh_token = os.environ["GCLOUD_REFRESH_TOKEN"].strip()
    threading.Thread(target=_tunnel_loop, args=(refresh_token,), daemon=True).start()
