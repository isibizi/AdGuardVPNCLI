"""Monitoring: connected clients, bandwidth and the event log."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from app import events, stats
from app.config import SETTINGS
from app.routes.status import snapshot
from app.web import render

router = APIRouter()

WINDOWS = {
    "10m": "10 Minuten",
    "1h": "1 Stunde",
    "24h": "24 Stunden",
    "7d": "7 Tage",
    "30d": "30 Tage",
}


@router.get("/monitor")
def monitor_page(request: Request, kind: str = "", q: str = "", window: str = "10m"):
    if window not in WINDOWS:
        window = "10m"
    return render(
        request, "monitor.html",
        data=snapshot(),
        log=events.query(kind=kind, search=q),
        kind=kind,
        search=q,
        window=window,
        windows=WINDOWS,
        sources=stats.active_sources(),
        connection_log_enabled=SETTINGS.connection_log,
    )


@router.get("/api/series")
def api_series(peer_id: int = 0, window: str = "10m"):
    if window not in WINDOWS:
        window = "10m"
    points = stats.series(peer_id=peer_id, window=window)
    return {
        "window": window,
        "peer_id": peer_id,
        # Bytes per second, which is what a bandwidth chart should show -
        # raw per-bucket totals would change meaning with the zoom level.
        "points": [
            {
                "ts": point["ts"],
                "up": point["rx"] / point["seconds"],
                "down": point["tx"] / point["seconds"],
            }
            for point in points
        ],
    }


@router.get("/api/events")
def api_events(kind: str = "", q: str = "", limit: int = 100):
    return {"events": events.query(kind=kind, search=q, limit=min(limit, 1000))}


@router.get("/monitor/log.txt")
def download_log(kind: str = "", q: str = ""):
    lines = [
        f"{entry['ts']}\t{entry['level']}\t{entry['kind']}\t{entry['message']}"
        for entry in events.query(kind=kind, search=q, limit=5000)
    ]
    return PlainTextResponse(
        "\n".join(reversed(lines)),
        headers={"Content-Disposition": 'attachment; filename="bridge-events.txt"'},
    )
