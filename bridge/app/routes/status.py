"""Dashboard and the JSON endpoint the pages poll for live state."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request

from app import adguard, state, stats, watchdog
from app.config import SETTINGS
from app.web import endpoint_or_hint, render

router = APIRouter()


def snapshot() -> dict:
    current = state.read()
    live = adguard.status_live()
    peers = stats.peer_overview()
    online = [p for p in peers if p["online"]]
    rate_up, rate_down = stats.current_rate(stats.TOTAL)

    uptime = 0
    if current.get("connected_since"):
        uptime = int(time.time()) - int(current["connected_since"])

    return {
        "vpn": {
            # The client's own answer wins over the stored one wherever it is
            # available; the stored value is only a fallback for the moments the
            # client cannot be asked.
            "connected": live.connected if live is not None else bool(current.get("vpn_connected")),
            "location": (live.location if live is not None and live.location else current.get("vpn_location", "")),
            "selected_location": watchdog.selected_location(),
            "exit_ip": current.get("exit_ip", ""),
            "vps_ip": current.get("vps_ip", ""),
            "login_required": live.auth_required if live is not None else bool(current.get("login_required")),
            "uptime": uptime,
            "reconnects": int(current.get("reconnects") or 0),
            "last_error": current.get("last_error", ""),
            "busy": current.get("busy", ""),
        },
        "killswitch": {
            "gate": current.get("gate", "closed"),
            # The gate is open only when traffic has been proven to flow.
            "traffic_allowed": current.get("gate") == "open",
        },
        "wireguard": {
            "backend": current.get("wg_backend", ""),
            "peers": len(peers),
            "online": len(online),
            "rate_up": rate_up,
            "rate_down": rate_down,
        },
        "peers": peers,
        "checked_at": current.get("last_check", 0),
    }


@router.get("/")
def dashboard(request: Request):
    endpoint, endpoint_warning = endpoint_or_hint()
    return render(
        request, "dashboard.html",
        data=snapshot(),
        endpoint=endpoint,
        endpoint_warning=endpoint_warning,
        bridge_address=str(SETTINGS.bridge_address),
    )


@router.get("/api/status")
def api_status():
    return snapshot()


@router.get("/api/diagnostics")
def api_diagnostics():
    """Raw output for the troubleshooting box - never hide what the CLI said."""
    status = adguard.status()
    return {
        "status_raw": status.raw,
        "config_raw": adguard.config_show().output,
        "license_raw": adguard.license_info().output,
    }
